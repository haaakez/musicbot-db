"""Library timestamps and link identity, shared by the bot and dashboard."""
from urllib.parse import urlsplit, parse_qs, unquote


def setup_metadata(conn):
    columns = {row[1] for row in conn.execute('PRAGMA table_info(songs)')}
    if 'added_at' not in columns:
        conn.execute('ALTER TABLE songs ADD COLUMN added_at TEXT')
    # Existing rows have unknown addition dates; do not invent dates for them.
    conn.execute('''CREATE TRIGGER IF NOT EXISTS song_added_timestamp
        AFTER INSERT ON songs WHEN NEW.added_at IS NULL
        BEGIN UPDATE songs SET added_at=CURRENT_TIMESTAMP WHERE song_id=NEW.song_id; END''')


def link_identity(url):
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().removeprefix('www.')
    except ValueError:
        return ('invalid', url)
    parts = [unquote(part) for part in parsed.path.split('/') if part]
    if host in ('youtube.com', 'music.youtube.com', 'm.youtube.com', 'youtu.be'):
        video = (parts[0] if parts else None) if host == 'youtu.be' else parse_qs(parsed.query).get('v', [None])[0]
        if not video and parts and parts[0] in ('shorts', 'embed', 'live') and len(parts) > 1:
            video = parts[1]
        if video:
            return ('youtube', video)
        return ('youtube', parsed.path, parsed.query)
    if host in ('open.spotify.com', 'soundcloud.com', 'm.soundcloud.com', 'bandcamp.com') or host.endswith('.bandcamp.com'):
        return (host.removeprefix('m.'), parsed.path.rstrip('/'))
    # Preserve unknown parameters, which may identify a different recording.
    return (host, parsed.path.rstrip('/'), parsed.query)
