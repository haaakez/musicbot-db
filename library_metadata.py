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


def duplicate_matches(songs, include_names=True):
    """Return review candidates without merging records or their ratings."""
    import difflib
    import re
    from collections import defaultdict
    links, titles, tokens = defaultdict(list), defaultdict(list), defaultdict(set)
    matches = defaultdict(dict)

    def mark(left, right, reason):
        if left != right:
            matches[left].setdefault(right, set()).add(reason)
            matches[right].setdefault(left, set()).add(reason)

    def title_key(value):
        value = re.sub(r'\b(official|audio|video|lyrics|hd|hq)\b', '', (value or '').casefold())
        return ' '.join(re.findall(r'\w+', value))

    normalized = {}
    for song in songs:
        sid = song['song_id']
        if song['url']:
            links[link_identity(song['url'])].append(sid)
        name = title_key(song['title'])
        if include_names and name and name != 'unknown title' and song['title'] != song['url']:
            normalized[sid] = name
            titles[name].append(sid)
            for token in set(name.split()):
                if len(token) >= 3:
                    tokens[token].add(sid)
    for groups, reason in ((links, 'Same link'), (titles, 'Similar name')):
        for ids in groups.values():
            for i, left in enumerate(ids):
                for right in ids[i + 1:]:
                    mark(left, right, reason)
    # Compare shared-word candidates, avoiding a full library-wide fuzzy scan.
    checked = set()
    for ids in tokens.values():
        ordered = sorted(ids)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                pair = (left, right)
                if pair in checked:
                    continue
                checked.add(pair)
                a, b = normalized[left], normalized[right]
                if min(len(a), len(b)) / max(len(a), len(b)) < .85:
                    continue
                if a != b and difflib.SequenceMatcher(None, a, b).ratio() >= .92:
                    mark(left, right, 'Similar name')
    return {sid: [{'id': peer, 'reasons': sorted(reasons)} for peer, reasons in sorted(peers.items())]
            for sid, peers in matches.items()}
