from flask import Flask, g, redirect, render_template, request, session, url_for, abort, jsonify
import sqlite3
import os
import json
import urllib.request
import difflib
import re
import math
import secrets
import hmac
import time
from datetime import datetime, timedelta, timezone
from collections import Counter
from urllib.parse import urlsplit
from i18n import RUSSIAN, ENGLISH, translate
from pathlib import Path
from identities import setup_users
from preferences import setup_settings, read_settings, write_settings, read_backup_settings
from radio import eligible
from command_help import COMMANDS

from library_metadata import setup_metadata, link_identity

DB_NAME = 'music_bot.db'
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'password')
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN') or os.environ.get('DISCORD_TOKEN')
if not DISCORD_BOT_TOKEN and Path('token.txt').is_file():
    DISCORD_BOT_TOKEN = Path('token.txt').read_text(encoding='utf-8').strip()


def t(text, **values):
    return translate(session.get('language', 'en'), text, **values)


def login_required(func):
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            if wants_json():
                return jsonify(error=t('Your session expired. Reload and sign in again.')), 401
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


def ensure_db_schema():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS songs (
                    song_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    url TEXT UNIQUE,
                    play_count INTEGER DEFAULT 0,
                    last_played TIMESTAMP
                )''')
    c.execute('PRAGMA table_info(songs)')
    song_columns = [row[1] for row in c.fetchall()]
    if 'artwork_url' not in song_columns:
        c.execute('ALTER TABLE songs ADD COLUMN artwork_url TEXT')
    if 'play_count' not in song_columns:
        c.execute('ALTER TABLE songs ADD COLUMN play_count INTEGER DEFAULT 0')
    if 'last_played' not in song_columns:
        c.execute('ALTER TABLE songs ADD COLUMN last_played TIMESTAMP')

    c.execute('''CREATE TABLE IF NOT EXISTS ratings (
                    rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    song_id INTEGER,
                    username TEXT,
                    score INTEGER,
                    user_id INTEGER,
                    FOREIGN KEY(song_id) REFERENCES songs(song_id)
                )''')
    c.execute('PRAGMA table_info(ratings)')
    rating_columns = [row[1] for row in c.fetchall()]
    if 'username' not in rating_columns:
        c.execute('ALTER TABLE ratings ADD COLUMN username TEXT')
    if 'user_id' not in rating_columns:
        c.execute('ALTER TABLE ratings ADD COLUMN user_id INTEGER')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_ratings_song_user ON ratings(song_id, user_id)')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_thresholds (
                    user_id INTEGER PRIMARY KEY,
                    min_rating REAL DEFAULT 0.0
                )''')
    
    setup_users(conn)
    setup_metadata(conn)
    setup_settings(conn)
    conn.commit()
    conn.close()


def normalize_username(username):
    return username.strip() or 'Anonymous'


def get_ratings_for_song_ids(song_ids):
    if not song_ids:
        return {}
    placeholders = ','.join('?' for _ in song_ids)
    rows = query_db(f'SELECT song_id, username, user_id, score FROM ratings WHERE song_id IN ({placeholders}) ORDER BY song_id, username COLLATE NOCASE', tuple(song_ids))
    grouped = {}
    for row in rows:
        song_id = row['song_id']
        grouped.setdefault(song_id, []).append({
            'username': row['username'] or (f'User {row["user_id"]}' if row['user_id'] else 'Anonymous'),
            'score': row['score']
        })
    return grouped


def get_rating_summary(song_ids):
    if not song_ids:
        return {}
    placeholders = ','.join('?' for _ in song_ids)
    rows = query_db(
        f'SELECT song_id, AVG(CASE WHEN score >= 0 THEN score END) AS avg_score, COUNT(CASE WHEN score >= 0 THEN 1 END) AS count, COUNT(*) AS rated_count FROM ratings WHERE song_id IN ({placeholders}) GROUP BY song_id',
        tuple(song_ids)
    )
    return {row['song_id']: {'avg_score': row['avg_score'], 'count': row['count'], 'rated_count': row['rated_count']} for row in rows}


def parse_average_filter(value):
    if not value or value == 'all':
        return None
    if value.startswith('>='):
        try:
            return ('>=', float(value[2:]))
        except ValueError:
            return None
    if value.startswith('='):
        try:
            return ('=', float(value[1:]))
        except ValueError:
            return None
    return None


def is_song_banned(song_id, song_ratings):
    return any(r['score'] == -1 for r in song_ratings.get(song_id, []))


def fetch_discord_username(user_id):
    if not DISCORD_BOT_TOKEN:
        return None
    url = f'https://discord.com/api/v10/users/{user_id}'
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bot {DISCORD_BOT_TOKEN}')
    req.add_header('User-Agent', 'MusicBotDB/1.0')
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            username = data.get('username')
            discriminator = data.get('discriminator')
            if username and discriminator and discriminator != '0':
                return f'{username}#{discriminator}'
            return username
    except Exception:
        return None


def get_known_raters():
    # New bot ratings also enter this registry; old databases are backfilled here.
    setup_users(get_db())
    get_db().commit()
    rows = query_db('SELECT user_id, username, checked_at FROM users ORDER BY username COLLATE NOCASE, user_id')
    raters = []
    for row in rows:
        user_id, username = row['user_id'], row['username']
        if not username and time.time() - row['checked_at'] > 3600:
            username = fetch_discord_username(user_id)
            execute_db('UPDATE users SET username = COALESCE(?, username), checked_at = ? WHERE user_id = ?',
                       (username, time.time(), user_id))
        display = username or t('Unknown user ({id})', id=user_id)
        raters.append({'display': display, 'value': f'id:{user_id}', 'user_id': user_id})
    return sorted(raters, key=lambda r: (r['display'].casefold(), r['user_id']))


def get_user_thresholds():
    rows = query_db('SELECT user_id, min_rating FROM user_thresholds')
    return {row['user_id']: row['min_rating'] for row in rows}


def get_username_for_user_id(user_id):
    row = query_db('SELECT username FROM users WHERE user_id = ?', (user_id,), one=True)
    return row['username'] if row else None


def get_progress():
    total = query_db('SELECT COUNT(*) AS n FROM songs', one=True)['n']
    counts = query_db('SELECT user_id, COUNT(*) AS n FROM ratings GROUP BY user_id')
    return total, {str(row['user_id']): row['n'] for row in counts}


def listening_preview(scores, listeners, names, thresholds, ignore_unrated):
    threshold = max((thresholds.get(uid, 0) for uid in listeners), default=0)
    blockers = []
    for uid in listeners:
        score = scores.get(uid)
        if score == -1:
            blockers.append(t('{name}: veto', name=names[uid]))
        elif score is None and not ignore_unrated:
            blockers.append(t('{name}: unrated', name=names[uid]))
        elif score is not None and score < threshold:
            blockers.append(t('{name}: {score} below {threshold}', name=names[uid], score=score, threshold=f'{threshold:g}'))
    return {'allowed': eligible(scores, listeners, threshold, ignore_unrated), 'blockers': blockers}


def get_ratings_matrix(song_ids, raters):
    if not song_ids or not raters:
        return {}
    placeholders = ','.join('?' for _ in song_ids)
    rows = query_db(
        f'SELECT song_id, user_id, score FROM ratings WHERE song_id IN ({placeholders})',
        tuple(song_ids)
    )
    matrix = {song_id: {} for song_id in song_ids}
    for row in rows:
        matrix.setdefault(row['song_id'], {})[row['user_id']] = row['score']
    return matrix


def parse_rater_value(value):
    if not value or value == '':
        return None, None
    if value.startswith('id:'):
        try:
            user_id = int(value.split(':', 1)[1])
            return (user_id, None) if 0 < user_id <= 9223372036854775807 else (None, None)
        except ValueError:
            return None, None
    if value.startswith('name:'):
        return None, value.split(':', 1)[1]
    return None, value


ensure_db_schema()


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        if not os.path.exists(DB_NAME):
            ensure_db_schema()
        db = g._database = sqlite3.connect(DB_NAME)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('PRAGMA busy_timeout = 30000')
    return db


def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


def execute_db(query, args=()):
    conn = get_db()
    cur = conn.execute(query, args)
    conn.commit()
    cur.close()


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()



def normalize_search(text):
    return ' '.join(''.join(c for c in text.casefold() if c.isalnum() or c.isspace()).split())


def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


@app.context_processor
def template_helpers():
    def page_url(page):
        args = request.args.to_dict(flat=False)
        args['page'] = page
        return url_for('index', **args)
    def sort_url(column):
        args = request.args.to_dict(flat=False)
        current = request.args.get('sort', 'relevance' if request.args.get('q') else 'title')
        default_direction = 'asc' if current == 'title' else 'desc'
        direction = request.args.get('direction', default_direction)
        args['sort'] = column
        args['direction'] = ('desc' if direction == 'asc' else 'asc') if current == column else ('asc' if column == 'title' else 'desc')
        args['page'] = 1
        return url_for('index', **args)
    return {'csrf_token': csrf_token, 'page_url': page_url, 'sort_url': sort_url,
            't': t, 'language': session.get('language', 'en')}


@app.before_request
def protect_forms():
    if request.method == 'POST':
        expected = session.get('csrf_token', '')
        supplied = request.form.get('csrf_token', '')
        if not expected or not hmac.compare_digest(expected, supplied):
            if wants_json():
                return jsonify(error=t('This form expired. Reload the page and try again.')), 400
            abort(400, t('This form expired. Reload the page and try again.'))


def wants_json():
    return request.headers.get('Accept') == 'application/json'


def back_to_library():
    # Only construct a local library URL; never trust a submitted redirect URL.
    return redirect(url_for('index') + '?' + request.form.get('filters', ''))


@app.route('/language')
def change_language():
    language = request.args.get('lang', 'en')
    if language not in ('en', 'ru'):
        abort(400)
    session['language'] = language
    target = request.args.get('next', url_for('index'))
    try:
        parsed = urlsplit(target)
    except ValueError:
        target = url_for('index')
        parsed = urlsplit(target)
    allowed = parsed.path in ('/', '/login', '/add') or re.fullmatch(r'/edit/[0-9]+', parsed.path)
    if parsed.scheme or parsed.netloc or not allowed or '\\' in target or any(ord(c) < 32 for c in target):
        target = url_for('index')
    return redirect(target)


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = t('Invalid username or password.')
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    language = session.get('language', 'en')
    session.clear()
    session['language'] = language
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    search_query = request.args.get('q', '').strip()
    avg_filter = request.args.get('avg_filter', 'all')
    banned_filter = request.args.get('banned_filter', 'all')
    not_rated_by = request.args.get('not_rated_by', 'all')
    view = request.args.get('view', 'all')
    if view not in ('all', 'everyone', 'missing', 'review', 'similar', 'new'):
        view = 'all'
    sort = request.args.get('sort', 'relevance' if search_query else 'title')
    if sort not in ('title', 'play_count', 'last_played', 'coverage', 'relevance', 'added_at'):
        sort = 'title'
    direction = request.args.get('direction', 'desc' if sort in ('play_count', 'last_played', 'coverage', 'relevance', 'added_at') else 'asc')
    direction = 'desc' if direction == 'desc' else 'asc'
    known_raters = get_known_raters()
    bot_guilds = query_db('SELECT guild_id, guild_name FROM guild_settings ORDER BY guild_name COLLATE NOCASE, guild_id')
    selected_guild = request.args.get('settings_guild', '')
    guild_ids = {str(row['guild_id']) for row in bot_guilds}
    if selected_guild not in guild_ids:
        selected_guild = str(bot_guilds[0]['guild_id']) if bot_guilds else None
    bot_settings = read_settings(DB_NAME, int(selected_guild)) if selected_guild else None
    names = {r['user_id']: r['display'] for r in known_raters}
    user_thresholds = get_user_thresholds()
    viewer = request.args.get('viewer', session.get('viewer', ''))
    if str(viewer).isdecimal() and int(viewer) in names:
        viewer = int(viewer)
        session['viewer'] = str(viewer)
    else:
        viewer = None
        if 'viewer' in request.args:
            session.pop('viewer', None)
    listeners = list(dict.fromkeys(int(value) for value in request.args.getlist('listener')
                                  if value.isdecimal() and int(value) in names))
    ignore_unrated = request.args.get('ignore_unrated', 'on') == 'on'
    threshold = max((user_thresholds.get(uid, 0) for uid in listeners), default=0)
    songs = query_db('SELECT song_id, title, url, play_count, last_played, artwork_url, added_at FROM songs')
    song_ids = [row['song_id'] for row in songs]
    rating_summary = get_rating_summary(song_ids)
    ratings_matrix = get_ratings_matrix(song_ids, known_raters)
    library_total, progress = get_progress()
    avg_rule = parse_average_filter(avg_filter)
    missing_id = not_rated_by.removeprefix('id:') if not_rated_by.startswith('id:') else ''
    missing_id = int(missing_id) if missing_id.isdecimal() else None
    relevance = {}
    filtered = []
    link_counts = Counter(link_identity(song['url']) for song in songs if song['url'])
    recent_since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    for song in songs:
        if view == 'similar' and (not song['url'] or link_counts[link_identity(song['url'])] < 2):
            continue
        if view == 'new' and (not song['added_at'] or song['added_at'] < recent_since):
            continue
        sid = song['song_id']
        scores = ratings_matrix.get(sid, {})
        summary = rating_summary.get(sid, {'avg_score': None, 'count': 0, 'rated_count': 0})
        avg = summary['avg_score']
        if search_query:
            query, title = normalize_search(search_query), normalize_search(song['title'] or '')
            score = difflib.SequenceMatcher(None, query, title).ratio()
            if query and query in title:
                score = max(score, .7)
            if query == title:
                score = 1
            if not query or score < .25:
                continue
            relevance[sid] = score
        if avg_rule and (avg is None or (avg_rule[0] == '>=' and avg < avg_rule[1]) or (avg_rule[0] == '=' and avg != avg_rule[1])):
            continue
        banned = -1 in scores.values()
        if banned_filter == 'banned' and not banned or banned_filter == 'not_banned' and banned:
            continue
        if missing_id is not None and missing_id in scores:
            continue
        if view == 'everyone' and (not names or any(scores.get(uid, -1) < 2 for uid in names)):
            continue
        if view == 'missing' and (viewer is None or viewer in scores):
            continue
        disagreement = len(scores) > 1 and max(scores.values()) - min(scores.values()) >= 2
        if view == 'review' and song['url'] and len(scores) >= len(names) and not disagreement:
            continue
        filtered.append(song)
    def sort_key(song):
        sid = song['song_id']
        if sort == 'title':
            return (song['title'] or '').casefold()
        if sort == 'coverage':
            return rating_summary.get(sid, {}).get('rated_count', 0)
        if sort == 'relevance':
            return relevance.get(sid, 0)
        return song[sort] if song[sort] is not None else (0 if sort == 'play_count' else '')
    filtered.sort(key=lambda song: ((song['title'] or '').casefold(), song['song_id']))
    filtered.sort(key=sort_key, reverse=direction == 'desc')
    if sort == 'last_played':
        filtered.sort(key=lambda song: song['last_played'] is None)  # Unknown dates always last.
    total = len(filtered)
    pages = max(1, math.ceil(total / 50))
    try:
        page = max(1, min(pages, int(request.args.get('page', 1))))
    except ValueError:
        page = 1
    songs = filtered[(page - 1) * 50:page * 50]
    previews = {song['song_id']: listening_preview(ratings_matrix.get(song['song_id'], {}), listeners,
                                                names, user_thresholds, ignore_unrated) for song in songs}
    dashboard_data = {
        'users': [{'id': str(r['user_id']), 'name': r['display'], 'threshold': user_thresholds.get(r['user_id'], 0)} for r in known_raters],
        'ratings': {str(song['song_id']): {str(uid): score for uid, score in ratings_matrix.get(song['song_id'], {}).items()} for song in songs},
        'progress': progress, 'libraryTotal': library_total,
        'messages': RUSSIAN if session.get('language') == 'ru' else ENGLISH,
    }
    return render_template('index.html', songs=songs, total=total, page=page, pages=pages,
        search_query=search_query, avg_filter=avg_filter, banned_filter=banned_filter, not_rated_by=not_rated_by,
        view=view, viewer=viewer, sort=sort, direction=direction, known_raters=known_raters,
        user_thresholds=user_thresholds, rating_summary=rating_summary, ratings_matrix=ratings_matrix,
        library_total=library_total, progress=progress, listeners=listeners, ignore_unrated=ignore_unrated,
        threshold=threshold, previews=previews, dashboard_data=dashboard_data,
        bot_guilds=bot_guilds, selected_guild=selected_guild, bot_settings=bot_settings,
        backup_settings=read_backup_settings(DB_NAME))


@app.route('/bot_settings', methods=['GET', 'POST'])
@login_required
def bot_settings_route():
    guild_id = request.values.get('guild_id', '')
    if not guild_id.isdecimal() or len(guild_id) > 19 or not 0 < int(guild_id) <= 9223372036854775807:
        return rating_error('Server not found.', 404)
    guild_id = int(guild_id)
    if not query_db('SELECT guild_id FROM guild_settings WHERE guild_id=?', (guild_id,), one=True):
        return rating_error('Server not found.', 404)
    if request.method == 'POST':
        values = {}
        for name in ('embed_ratings', 'voice_status', 'ignore_unrated', 'radio_always'):
            value = request.form.get(name)
            if value not in ('on', 'off'):
                return rating_error('Choose on or off for each setting.', 400)
            values[name] = value == 'on'
        write_settings(DB_NAME, guild_id, **values)
        if not wants_json():
            return back_to_library()
    return jsonify(guild_id=str(guild_id), settings=read_settings(DB_NAME, guild_id))


@app.route('/about')
@login_required
def about():
    return render_template('about.html', commands=COMMANDS)


@app.route('/runtime_status')
@login_required
def runtime_status_route():
    guild_id = request.args.get('guild_id', '')
    row = query_db('SELECT updated_at, payload FROM bot_runtime WHERE guild_id=?', (guild_id,), one=True)
    response = jsonify(stale=not row or time.time() - row['updated_at'] > 20,
                       status=json.loads(row['payload']) if row else None,
                       backup=read_backup_settings(DB_NAME))
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/backup_settings', methods=['POST'])
@login_required
def backup_settings_route():
    try:
        hours = float(request.form.get('interval_hours', ''))
    except ValueError:
        return rating_error('Backup interval must be between 1 and 168 hours.', 400)
    if not math.isfinite(hours) or not 1 <= hours <= 168:
        return rating_error('Backup interval must be between 1 and 168 hours.', 400)
    execute_db('UPDATE backup_settings SET interval_hours=? WHERE id=1', (hours,))
    if wants_json():
        return jsonify(settings=read_backup_settings(DB_NAME))
    return back_to_library()


@app.route('/refresh_usernames', methods=['POST'])
@login_required
def refresh_usernames():
    users = query_db('SELECT user_id FROM users')
    updated = 0
    for row in users:
        name = fetch_discord_username(row['user_id'])
        if name:
            execute_db('UPDATE users SET username=?, checked_at=? WHERE user_id=?',
                       (name, time.time(), row['user_id']))
            execute_db('UPDATE ratings SET username=? WHERE user_id=?', (name, row['user_id']))
            updated += 1
    if wants_json():
        return jsonify(updated=updated, total=len(users))
    return back_to_library()


@app.route('/update_threshold/<int:user_id>', methods=['POST'])
@login_required
def update_threshold(user_id):
    try:
        min_rating = float(request.form.get('min_rating', '0.0'))
    except ValueError:
        return rating_error('Invalid threshold', 400)
    if not math.isfinite(min_rating) or not 0 <= min_rating <= 3:
        return rating_error('Threshold must be between 0 and 3', 400)
    execute_db('INSERT OR REPLACE INTO user_thresholds (user_id, min_rating) VALUES (?, ?)', (user_id, min_rating))
    if wants_json():
        return jsonify(user_id=str(user_id), min_rating=min_rating)
    return back_to_library()


@app.route('/rate/<int:song_id>', methods=['POST'])
@login_required
def rate_song(song_id):
    user_id, _ = parse_rater_value(request.form.get('rater', '').strip())
    if user_id is None or not query_db('SELECT user_id FROM users WHERE user_id=?', (user_id,), one=True):
        return rating_error('Choose a known Discord user.', 400)
    value = request.form.get('score', '')
    try:
        score = None if value == 'unrated' else int(value)
    except ValueError:
        return rating_error('Invalid rating.', 400)
    if score is not None and score not in (-1, 0, 1, 2, 3):
        return rating_error('Invalid rating.', 400)
    conn = get_db()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT song_id FROM songs WHERE song_id=?', (song_id,)).fetchone():
            return rating_error('This song was deleted. Reload the library.', 404)
        old = conn.execute('SELECT score FROM ratings WHERE song_id=? AND user_id=?', (song_id, user_id)).fetchone()
        previous = old['score'] if old else None
        expected = request.form.get('expected')
        if expected is not None and expected != ('unrated' if previous is None else str(previous)):
            return rating_error('This rating changed elsewhere. Reload before editing it.', 409)
        if score is None:
            conn.execute('DELETE FROM ratings WHERE song_id=? AND user_id=?', (song_id, user_id))
        else:
            conn.execute("INSERT INTO ratings (song_id, user_id, username, score) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(song_id, user_id) DO UPDATE SET score=excluded.score, username=excluded.username",
                (song_id, user_id, get_username_for_user_id(user_id), score))
    if wants_json():
        summary = get_rating_summary([song_id]).get(song_id, {'avg_score': None, 'count': 0, 'rated_count': 0})
        total, progress = get_progress()
        return jsonify(score=score, previous=previous, summary=summary, progress=progress, library_total=total)
    return back_to_library()


def rating_error(message, status):
    if wants_json():
        return jsonify(error=t(message)), status
    abort(status, t(message))


@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_song():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        url = request.form.get('url', '').strip() or None
        if title or url:
            execute_db('INSERT INTO songs (title, url, play_count, last_played) VALUES (?, ?, 0, NULL)', (title or None, url))
            return redirect(url_for('index'))
        abort(400, t('Enter a title or link.'))
    return render_template('song_form.html', song=None, action='Add')


@app.route('/edit/<int:song_id>', methods=['GET', 'POST'])
@login_required
def edit_song(song_id):
    song = query_db('SELECT song_id, title, url, play_count FROM songs WHERE song_id = ?', (song_id,), one=True)
    if song is None:
        return redirect(url_for('index'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        url = request.form.get('url', '').strip() or None
        if not title and not url:
            abort(400, t('Enter a title or link.'))
        play_count = int(request.form.get('play_count', song['play_count'] or 0))
        execute_db('UPDATE songs SET title = ?, url = ?, play_count = ? WHERE song_id = ?', (title or None, url, play_count, song_id))
        return redirect(url_for('index'))

    return render_template('song_form.html', song=song, action='Edit')


@app.route('/delete/<int:song_id>', methods=['POST'])
@login_required
def delete_song(song_id):
    conn = get_db()
    with conn:
        conn.execute('DELETE FROM ratings WHERE song_id = ?', (song_id,))
        conn.execute('DELETE FROM songs WHERE song_id = ?', (song_id,))
    return redirect(url_for('index'))


if __name__ == '__main__':
    port = int(os.environ.get('FLASK_PORT', 5000))
    app.run(host=os.environ.get('FLASK_HOST', '127.0.0.1'), port=port, debug=False)
