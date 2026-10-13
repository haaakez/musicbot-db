"""Run an allowlisted dashboard behind a temporary Cloudflare HTTPS tunnel."""
import importlib.util
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from discord_auth import DiscordAuth

ROOT = Path(__file__).resolve().parent


def stop(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main():
    os.chdir(ROOT)
    cloudflared = shutil.which('cloudflared')
    if not cloudflared:
        raise RuntimeError('cloudflared is missing. enter the updated nix-shell first.')
    if importlib.util.find_spec('gunicorn') is None:
        raise RuntimeError('gunicorn is missing. run pip install -r requirements.txt.')
    secret = os.environ.get('FLASK_SECRET_KEY', '')
    if len(secret) < 32 or secret.startswith('replace-with-'):
        raise RuntimeError('set a permanent random FLASK_SECRET_KEY in .env (at least 32 characters).')
    # Validate credentials and the allowlist before creating any public endpoint.
    # The actual random callback is supplied only to the dashboard child process.
    original_redirect = os.environ.get('DISCORD_OAUTH_REDIRECT_URI')
    os.environ['DISCORD_OAUTH_REDIRECT_URI'] = 'https://temporary.invalid/auth/discord/callback'
    try:
        auth = DiscordAuth()
        if not auth.enabled:
            raise RuntimeError('configure Discord OAuth credentials and allowed user IDs in .env first.')
    finally:
        if original_redirect is None:
            os.environ.pop('DISCORD_OAUTH_REDIRECT_URI', None)
        else:
            os.environ['DISCORD_OAUTH_REDIRECT_URI'] = original_redirect
    config_directory = Path.home() / '.cloudflared'
    if any((config_directory / name).exists() for name in ('config.yml', 'config.yaml')):
        raise RuntimeError('Quick Tunnels conflict with ~/.cloudflared/config.yml or config.yaml. use a named tunnel or temporarily move that configuration yourself.')
    # Reserve a local port; Gunicorn inherits this socket so another service cannot take it.
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    tunnel = server = None
    output = queue.Queue()
    def interrupted(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    try:
        tunnel = subprocess.Popen([cloudflared, 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{port}'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

        def read_output():
            for line in tunnel.stdout:
                output.put(line)

        threading.Thread(target=read_output, daemon=True).start()
        deadline = time.monotonic() + 90
        public_url = None
        while time.monotonic() < deadline:
            if tunnel.poll() is not None:
                raise RuntimeError('Cloudflare tunnel exited before assigning an address.')
            try:
                line = output.get(timeout=1)
            except queue.Empty:
                continue
            print(line.rstrip(), flush=True)
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
            if match:
                public_url = match[0]
                break
        if not public_url:
            raise RuntimeError('Cloudflare did not provide an address within 90 seconds.')
        callback = public_url + '/auth/discord/callback'
        environment = os.environ.copy()
        environment['DISCORD_OAUTH_REDIRECT_URI'] = callback
        environment['FLASK_COOKIE_SECURE'] = 'true'
        server = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--workers', '1', '--threads', '4',
            '--timeout', '60', '--bind', f'fd://{listener.fileno()}', 'webapp:app'],
            cwd=ROOT, env=environment, pass_fds=(listener.fileno(),), start_new_session=True)
        listener.close()
        print(f'\nshare this dashboard address: {public_url}', flush=True)
        print(f'add this exact redirect in Discord Developer Portal > OAuth2 > Redirects:\n{callback}', flush=True)
        print('save the redirect there, then your allowed friends can sign in.', flush=True)
        print('keep this terminal open. ctrl+c stops the temporary dashboard and tunnel.', flush=True)
        print('the next run gets a different address. .env stays on your machine.', flush=True)
        while tunnel.poll() is None and server.poll() is None:
            try:
                line = output.get(timeout=1)
                print(line.rstrip(), flush=True)
            except queue.Empty:
                pass
        raise RuntimeError('Dashboard or tunnel exited. temporary sharing has stopped.')
    finally:
        listener.close()
        stop(server)
        stop(tunnel)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\ntemporary sharing stopped.')
    except Exception as error:
        print(f'temporary sharing failed: {error}', file=sys.stderr)
        sys.exit(1)
