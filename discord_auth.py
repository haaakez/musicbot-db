"""Discord authorization-code login using only the identify scope."""
import json
import os
import re
import urllib.request
from urllib.parse import urlencode, urlsplit


class DiscordAuth:
    def __init__(self):
        self.client_id = os.environ.get('DISCORD_OAUTH_CLIENT_ID', '').strip()
        self.client_secret = os.environ.get('DISCORD_OAUTH_CLIENT_SECRET', '').strip()
        self.redirect_uri = os.environ.get('DISCORD_OAUTH_REDIRECT_URI', '').strip()
        self.enabled = bool(self.client_id or self.client_secret or self.redirect_uri)
        raw = os.environ.get('DISCORD_ALLOWED_USER_IDS', '')
        ids = [value for value in re.split(r'[,\s]+', raw.strip()) if value]
        if any(not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**63 for value in ids):
            raise RuntimeError('DISCORD_ALLOWED_USER_IDS must contain comma-separated Discord user IDs.')
        self.allowed_ids = {str(int(value)) for value in ids}
        if self.enabled:
            if not all((self.client_id, self.client_secret, self.redirect_uri, self.allowed_ids)):
                raise RuntimeError('Discord login requires client ID, client secret, redirect URI, and allowed user IDs.')
            parsed = urlsplit(self.redirect_uri)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.query or parsed.fragment
                    or parsed.username or parsed.password):
                raise RuntimeError('Use an absolute Discord callback URL without credentials, query, or fragment.')
            if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
                raise RuntimeError('Discord login requires HTTPS, except for localhost development.')

    def authorization_url(self, state):
        return 'https://discord.com/oauth2/authorize?' + urlencode({
            'client_id': self.client_id, 'redirect_uri': self.redirect_uri,
            'response_type': 'code', 'scope': 'identify', 'state': state,
        })

    def identify(self, code):
        data = urlencode({'client_id': self.client_id, 'client_secret': self.client_secret,
                          'grant_type': 'authorization_code', 'code': code,
                          'redirect_uri': self.redirect_uri}).encode()
        request = urllib.request.Request('https://discord.com/api/v10/oauth2/token', data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'musicbot-dashboard'})
        with urllib.request.urlopen(request, timeout=10) as response:
            token = json.load(response)['access_token']
        request = urllib.request.Request('https://discord.com/api/v10/users/@me',
            headers={'Authorization': f'Bearer {token}', 'User-Agent': 'musicbot-dashboard'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
