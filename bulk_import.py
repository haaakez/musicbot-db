"""Parse import text and prepare an explicit, duplicate-aware review."""
import csv
import io
import json
import re
from urllib.parse import urlsplit
from library_metadata import link_identity
from track_links import clean_link


def parse_import(text, existing):
    if len(text.encode('utf-8')) > 2_000_000:
        raise ValueError('Import is limited to 2 MB.')
    text = text.lstrip('\ufeff').strip()
    lines = text.splitlines()
    if not text.startswith('[') and len(lines) > 5001:
        raise ValueError('Import is limited to 5000 entries.')
    first = next((line for line in lines if line.strip()), '')
    rows = []
    if text.startswith('['):
        items = json.loads(text)
        if not isinstance(items, list) or len(items) > 5000:
            raise ValueError('Import is limited to 5000 entries.')
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Each JSON entry must be an object.')
            title, url = item.get('title', ''), item.get('url', item.get('link', ''))
            ratings = item.get('ratings', {})
            if not isinstance(title, str) or not isinstance(url, str) or not url.strip():
                raise ValueError('JSON entries require a link and an optional text title.')
            if not isinstance(ratings, dict):
                raise ValueError('Ratings must map Discord user IDs to scores.')
            for user_id, score in ratings.items():
                if not re.fullmatch(r'[1-9][0-9]{16,19}', user_id) or int(user_id) > 9223372036854775807:
                    raise ValueError('Use valid Discord user IDs for ratings.')
                if type(score) is not int or score not in (-1, 0, 1, 2, 3):
                    raise ValueError('Ratings must be integers from -1 to 3.')
            rows.append((title.strip(), url.strip(), ratings))
    elif first.lower().strip().startswith(('title,', 'title\t', 'url,', 'url\t')):
        reader = csv.DictReader(io.StringIO(text), delimiter='\t' if '\t' in first else ',')
        for row in reader:
            row = {(key or '').strip().lower(): value for key, value in row.items()}
            rows.append(((row.get('title') or '').strip(), (row.get('url') or '').strip(), {}))
    else:
        for line in lines:
            line = line.strip()
            if line:
                rows.append(('', line, {}) if line.startswith(('http://', 'https://')) else (line, '', {}))
    known_links = {link_identity(song['url']) for song in existing if song['url']}
    normalize = lambda name: ' '.join(re.findall(r'\w+', (name or '').casefold()))
    known_names = {normalize(song['title']) for song in existing if song['title']}
    preview = []
    for title, url, ratings in rows:
        error = None
        if not title and not url:
            continue
        if len(title) > 1000 or len(url) > 2048:
            error = 'Entry is too long.'
        try:
            if url and (urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).hostname):
                error = 'Use an http or https link.'
        except ValueError:
            error = 'Invalid link.'
        if url and not error:
            try:
                url, _ = clean_link(url)
            except ValueError:
                error = 'Invalid link timecode.'
        duplicate = bool(url and link_identity(url) in known_links or title and normalize(title) in known_names)
        preview.append({'title': title or None, 'url': url or None, 'error': error, 'duplicate': duplicate, 'ratings': ratings})
        if url:
            known_links.add(link_identity(url))
        if title:
            known_names.add(normalize(title))
    return preview
