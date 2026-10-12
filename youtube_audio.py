"""Cache YouTube audio locally when its Lavalink stream resolver cannot play it."""
import asyncio
import copy
import hashlib
import logging
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit

import wavelink

log = logging.getLogger(__name__)
PROVIDER_VERSION = '2.0.1'
PROVIDER_REPO = 'https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git'


class YoutubeAudio:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.provider = self.root / 'provider' / 'server'
        self.tracks = self.root / 'tracks'
        self.enabled = os.environ.get('YOUTUBE_CACHE_ENABLED', 'true').lower() in ('1', 'true', 'on')
        self.ready = False
        self.lock = asyncio.Lock()
        self.limit = max(64, int(os.environ.get('YOUTUBE_CACHE_MB', '1024'))) * 1024 * 1024

    async def initialize(self):
        if not self.enabled:
            return
        async with self.lock:
            if not self.ready:
                await asyncio.to_thread(self._setup)
                self.ready = True

    def _setup(self):
        for executable in ('node', 'npm', 'git', 'deno'):
            if not shutil.which(executable):
                raise RuntimeError(f'{executable} is required for YouTube audio. run nix-shell first.')
        import yt_dlp  # Dependency errors should happen before connecting to Discord.
        import yt_dlp_ejs
        from yt_dlp.plugins import load_all_plugins
        load_all_plugins()
        self.tracks.mkdir(parents=True, exist_ok=True)
        checkout = self.provider.parent
        if not (checkout / '.git').exists():
            subprocess.run(['git', '-c', 'advice.detachedHead=false', 'clone', '--quiet', '--depth', '1',
                            '--branch', PROVIDER_VERSION, PROVIDER_REPO, str(checkout)], check=True, timeout=120)
        if not (self.provider / 'build' / 'generate_once.js').is_file():
            subprocess.run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=self.provider, check=True, timeout=180)
            subprocess.run(['npm', 'exec', '--', 'tsc'], cwd=self.provider, check=True, timeout=120)
        log.info('YouTube audio cache ready at %s', self.tracks)

    @staticmethod
    def is_youtube(track):
        return urlsplit(track.uri or '').hostname in ('youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be')

    def _cached(self, key):
        for extension in ('.webm', '.m4a', '.mp4', '.opus', '.ogg', '.mp3'):
            path = self.tracks / (key + extension)
            if path.is_file() and path.stat().st_size:
                path.touch()
                return path
        return None

    def _download(self, url):
        import yt_dlp
        key = hashlib.sha256(url.encode()).hexdigest()
        existing = self._cached(key)
        if existing:
            return existing
        options = {
            'format': 'bestaudio/best', 'outtmpl': str(self.tracks / (key + '.%(ext)s')),
            'noplaylist': True, 'quiet': True, 'noprogress': True,
            'socket_timeout': 20, 'retries': 1, 'fragment_retries': 1,
            'max_filesize': self.limit,
            'js_runtimes': {'deno': {}},
            'extractor_args': {
                'youtube': {'player_client': ['mweb']},
                'youtubepot-bgutilscript': {'server_home': [str(self.provider)]},
            },
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.extract_info(url, download=True)
        except Exception:
            for partial in self.tracks.glob(key + '.*'):
                if partial.suffix in ('.part', '.ytdl'):
                    partial.unlink(missing_ok=True)
            raise
        result = self._cached(key)
        if result is None:
            raise RuntimeError('YouTube download produced no audio file')
        return result

    def prune(self, protected):
        protected = {Path(path).resolve() for path in protected if path}
        files = [path for path in self.tracks.iterdir() if path.is_file() and path.suffix in
                 ('.webm', '.m4a', '.mp4', '.opus', '.ogg', '.mp3')]
        total = sum(path.stat().st_size for path in files)
        for path in sorted(files, key=lambda item: item.stat().st_mtime):
            if total <= self.limit:
                break
            if path.resolve() in protected:
                continue
            size = path.stat().st_size
            path.unlink()
            total -= size

    async def prepare(self, track, node, protected=()):
        if not self.enabled or not self.is_youtube(track) or track.is_stream:
            return track
        await self.initialize()
        async with self.lock:
            path = await asyncio.to_thread(self._download, track.uri)
            await asyncio.to_thread(self.prune, [*protected, str(path)])
            loaded = await wavelink.Playable.search(str(path), source=None, node=node)
            if not loaded:
                raise RuntimeError('Lavalink cannot load cached audio. enable sources.local and restart Lavalink.')
            # Keep the local encoded source, while the UI retains the library recording's metadata.
            data = copy.deepcopy(loaded[0].raw_data)
            for key in ('title', 'author', 'uri', 'artworkUrl', 'isrc'):
                if key in track.raw_data['info']:
                    data['info'][key] = track.raw_data['info'][key]
            data['userData'] = dict(track.extras)
            data['userData']['cache_path'] = str(path)
            return wavelink.Playable(data)
