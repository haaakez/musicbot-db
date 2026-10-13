"""Clean platform links without losing an intentional playback start time."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode


def time_seconds(value):
    value = value.strip().lower()
    if re.fullmatch(r'\d+', value):
        return int(value)
    if re.fullmatch(r'\d+(?::\d{1,2}){1,2}', value):
        total = 0
        for part in value.split(':'):
            total = total * 60 + int(part)
        return total
    match = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', value)
    if match and any(match.groups()):
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        return hours*3600 + minutes*60 + seconds
    raise ValueError('Invalid link timecode.')


def clean_link(url):
    if not url:
        return url, 0
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or '').lower().removeprefix('www.')
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    value = next((query[key][0] for key in ('t', 'start', 'time_continue') if query.get(key)), None)
    if value is None:
        value = fragment.get('t', [None])[0]
    start = time_seconds(value) if value else 0
    if host in ('youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'):
        parts = [part for part in parsed.path.split('/') if part]
        video = query.get('v', [None])[0]
        if host == 'youtu.be' and parts:
            video = parts[0]
        elif not video and len(parts) > 1 and parts[0] in ('shorts', 'embed', 'live'):
            video = parts[1]
        if video:
            fields = {'v': video}
            if start: fields['t'] = str(start)
            return 'https://www.youtube.com/watch?' + urlencode(fields), start
        if parsed.path.rstrip('/') == '/playlist' and query.get('list'):
            return 'https://www.youtube.com/playlist?' + urlencode({'list':query['list'][0]}), 0
    known = host in ('open.spotify.com', 'soundcloud.com', 'm.soundcloud.com') or host.endswith('.bandcamp.com')
    if known:
        fields = {'t': str(start)} if start else {}
        return urlunsplit(('https', host.removeprefix('m.'), parsed.path, urlencode(fields), '')), start
    # Keep parameters needed by arbitrary HTTP streams; remove only known tracking fields.
    fields = {key: values for key,values in query.items() if not key.startswith('utm_') and key not in ('fbclid','gclid')}
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(fields,doseq=True), parsed.fragment)), start
