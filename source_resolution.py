"""Prefer SoundCloud matches while keeping explicit source requests available."""
import difflib
import logging
import re
from urllib.parse import urlsplit

import wavelink

log = logging.getLogger(__name__)
SEARCH_PREFIX = re.compile(r'^(ytsearch|ytmsearch|scsearch|spsearch|amsearch|dzsearch):')
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be', 'm.youtube.com'}


def normalized(title):
    title = re.sub(r'\b(official|audio|video|lyrics|hd|hq)\b', '', title.casefold())
    return ' '.join(re.findall(r'\w+', title))


def best_match(tracks, title, reference=None):
    wanted = normalized(title)
    matches = []
    for track in list(tracks or [])[:10]:
        candidate = normalized(track.title)
        # Do not substitute covers, remixes or live versions unless requested.
        if any(word in candidate.split() and word not in wanted.split()
               for word in ('cover', 'remix', 'live', 'instrumental', 'karaoke')):
            continue
        if reference and reference.length and track.length:
            if abs(reference.length - track.length) > max(10000, reference.length * .05):
                continue
        score = max(difflib.SequenceMatcher(None, wanted, candidate).ratio(),
                    difflib.SequenceMatcher(None, wanted, normalized(f'{track.author} {track.title}')).ratio())
        if score >= .72:
            matches.append((score, track))
    return max(matches, key=lambda item: item[0])[1] if matches else None


async def resolve_tracks(query, node, title=None):
    async def search(value):
        return await wavelink.Playable.search(value, source=None, node=node)

    if SEARCH_PREFIX.match(query):
        return await search(query)
    host = (urlsplit(query).hostname or '').lower()
    if host and host not in YOUTUBE_HOSTS:
        return await search(query)

    reference = None
    if host:
        # Metadata helps match a saved YouTube link to the same recording.
        try:
            originals = await search(query)
            if isinstance(originals, wavelink.Playlist):
                return originals
            reference = originals[0] if originals else None
        except Exception:
            log.warning('YouTube metadata lookup failed; trying the saved title.', exc_info=True)
        title = title or (reference.title if reference else None)
    else:
        title = title or query

    if title:
        try:
            match = best_match(await search(f'scsearch:{title}'), title, reference)
            if match:
                return [match]
        except Exception:
            log.warning('SoundCloud search failed; falling back to YouTube.', exc_info=True)
    if reference:
        return [reference]
    return await search(query if host else f'ytmsearch:{query}')
