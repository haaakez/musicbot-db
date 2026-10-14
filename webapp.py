from flask import Flask, g, redirect, render_template, request, session, url_for, abort, jsonify
import sqlite3
import os
import json
import urllib.request
import urllib.error
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
from identities import setup_users, remember_user
from preferences import setup_settings, read_settings, write_settings, read_backup_settings
from radio import eligible
from command_help import COMMANDS
from bulk_import import parse_import
from discord_auth import DiscordAuth
from track_links import clean_link

from library_metadata import setup_metadata, link_identity, duplicate_matches

from playback_store import setup_playback

from playback_recovery import setup_recovery

DB_NAME = 'music_bot.db'
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2_100_000
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))

discord_auth = DiscordAuth()
login_days = int(os.environ.get('DASHBOARD_LOGIN_DAYS', '30'))
if not 1 <= login_days <= 90:
    raise RuntimeError('DASHBOARD_LOGIN_DAYS must be between 1 and 90.')
if discord_auth.enabled:
    secret = os.environ.get('FLASK_SECRET_KEY', '')
    if len(secret) < 32 or secret.startswith('replace-with-'):
        raise RuntimeError('Set a permanent random FLASK_SECRET_KEY of at least 32 characters for Discord login.')
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('FLASK_COOKIE_SECURE', str(discord_auth.redirect_uri.startswith('https://'))).lower() in ('true', '1', 'on'),
    PERMANENT_SESSION_LIFETIME=timedelta(days=login_days),
    SESSION_REFRESH_EACH_REQUEST=False,
)

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'password')
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN') or os.environ.get('DISCORD_TOKEN')
if not DISCORD_BOT_TOKEN and Path('token.txt').is_file():
    DISCORD_BOT_TOKEN = Path('token.txt').read_text(encoding='utf-8').strip()


def t(text, **values):
    return translate(session.get('language', 'ru'), text, **values)


def login_required(func):
    def wrapper(*args, **kwargs):
        authenticated = session.get('logged_in') and session.get('login_expires', 0) > time.time()
        if discord_auth.enabled:
            authenticated = authenticated and session.get('auth_method') == 'discord' and session.get('discord_user_id') in discord_auth.allowed_ids
        if not authenticated:
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
    setup_playback(conn)
    setup_recovery(conn)
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
    return {row['user_id']: int(row['min_rating']) for row in rows}


def get_username_for_user_id(user_id):
    row = query_db('SELECT username FROM users WHERE user_id = ?', (user_id,), one=True)
    return row['username'] if row else None


def get_progress():
    total = query_db('SELECT COUNT(*) AS n FROM songs', one=True)['n']
    counts = query_db('SELECT user_id, COUNT(*) AS n FROM ratings GROUP BY user_id')
    return total, {str(row['user_id']): row['n'] for row in counts}


def listening_preview(scores, listeners, names, thresholds, ignore_unrated):
    blockers = []
    for uid in listeners:
        threshold = thresholds.get(uid, 0)
        score = scores.get(uid)
        if score == -1 and threshold > -1:
            blockers.append(t('{name}: veto', name=names[uid]))
        elif score is None and not ignore_unrated:
            blockers.append(t('{name}: unrated', name=names[uid]))
        elif score is not None and score < threshold:
            blockers.append(t('{name}: {score} below {threshold}', name=names[uid], score=score, threshold=f'{threshold:g}'))
    return {'allowed': eligible(scores, listeners, thresholds, ignore_unrated), 'blockers': blockers}


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
        default_direction = 'asc' if current in ('title', 'song_id') else 'desc'
        direction = request.args.get('direction', default_direction)
        args['sort'] = column
        args['direction'] = ('desc' if direction == 'asc' else 'asc') if current == column else ('asc' if column in ('title', 'song_id') else 'desc')
        args['page'] = 1
        return url_for('index', **args)
    return {'csrf_token': csrf_token, 'page_url': page_url, 'sort_url': sort_url,
            't': t, 'language': session.get('language', 'ru'),
            'discord_login_enabled': discord_auth.enabled, 'login_days': login_days,
            'login_name': session.get('discord_username'),
            'rating_owner': session.get('discord_user_id')}


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
    language = request.args.get('lang', 'ru')
    if language not in ('en', 'ru'):
        abort(400)
    session['language'] = language
    target = request.args.get('next', url_for('index'))
    try:
        parsed = urlsplit(target)
    except ValueError:
        target = url_for('index')
        parsed = urlsplit(target)
    allowed = parsed.path in ('/', '/login', '/add', '/about', '/import') or re.fullmatch(r'/edit/[0-9]+', parsed.path)
    if parsed.scheme or parsed.netloc or not allowed or '\\' in target or any(ord(c) < 32 for c in target):
        target = url_for('index')
    return redirect(target)


def establish_login(method, remember, user=None):
    language = session.get('language', 'ru')
    session.clear()
    session['language'] = language
    session['logged_in'] = True
    session['auth_method'] = method
    session.permanent = remember
    session['login_expires'] = time.time() + (login_days * 86400 if remember else 12 * 3600)
    session['csrf_token'] = secrets.token_hex(32)
    if user:
        session['discord_user_id'] = str(user['id'])
        session['discord_username'] = user['username']
        session['viewer'] = str(user['id'])


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if discord_auth.enabled:
            abort(403)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode()) and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            establish_login('password', request.form.get('remember') == 'on')
            return redirect(url_for('index'))
        error = t('Invalid username or password.')
    return render_template('login.html', error=error)


@app.route('/auth/discord')
def discord_login():
    if not discord_auth.enabled:
        abort(404)
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    session['oauth_started'] = time.time()
    session['oauth_remember'] = request.args.get('remember') == 'on'
    return redirect(discord_auth.authorization_url(state))


@app.route('/auth/discord/callback')
def discord_callback():
    if not discord_auth.enabled:
        abort(404)
    expected = session.pop('oauth_state', '')
    started = session.pop('oauth_started', 0)
    remember = session.pop('oauth_remember', False)
    supplied = request.args.get('state', '')
    if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()) or time.time()-started > 600:
        return render_template('login.html', error=t('Login expired. Please try again.')), 400
    if request.args.get('error') or not request.args.get('code'):
        return render_template('login.html', error=t('Discord login was cancelled.')), 400
    try:
        user = discord_auth.identify(request.args['code'])
        if str(user['id']) not in discord_auth.allowed_ids:
            return render_template('login.html', error=t('Your Discord account is not allowed to access this dashboard.')), 403
        remember_user(DB_NAME, int(user['id']), user['username'])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return render_template('login.html', error=t('Discord login failed. Please try again.')), 502
    establish_login('discord', remember, user)
    return redirect(url_for('index'))


@app.route('/logout')
def logout():
    language = session.get('language', 'ru')
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
    hide_mine = request.args.get('hide_mine') == 'on'
    hide_full = request.args.get('hide_full') == 'on'
    rating_owner = int(session['discord_user_id']) if session.get('discord_user_id') else None
    view = request.args.get('view', 'all')
    if view not in ('all', 'everyone', 'missing', 'review', 'similar', 'new', 'duplicates'):
        view = 'all'
    sort = request.args.get('sort', 'relevance' if search_query else 'title')
    if sort not in ('title', 'song_id', 'average', 'play_count', 'last_played', 'coverage', 'relevance', 'added_at'):
        sort = 'title'
    direction = request.args.get('direction', 'desc' if sort in ('average', 'play_count', 'last_played', 'coverage', 'relevance', 'added_at') else 'asc')
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
    duplicates = duplicate_matches(songs, include_names=view == 'duplicates') if view in ('duplicates', 'similar') else {}
    recent_since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    for song in songs:
        if view == 'duplicates' and song['song_id'] not in duplicates:
            continue
        if view == 'similar' and (not song['url'] or link_counts[link_identity(song['url'])] < 2):
            continue
        if view == 'new' and (not song['added_at'] or song['added_at'] < recent_since):
            continue
        sid = song['song_id']
        scores = ratings_matrix.get(sid, {})
        if hide_mine and rating_owner is not None and rating_owner in scores:
            continue
        if hide_full and names and all(uid in scores for uid in names):
            continue
        summary = rating_summary.get(sid, {'avg_score': None, 'count': 0, 'rated_count': 0})
        avg = summary['avg_score']
        if search_query:
            raw = search_query.casefold()
            id_query = search_query.removeprefix('#')
            url = (song['url'] or '').casefold()
            if raw.startswith(('http://', 'https://')):
                if not song['url'] or link_identity(search_query) != link_identity(song['url']):
                    continue
                score = 2
            elif search_query.startswith('#') and id_query.isdecimal():
                if int(id_query) != sid:
                    continue
                score = 2
            else:
                query, title = normalize_search(search_query), normalize_search(song['title'] or '')
                score = difflib.SequenceMatcher(None, query, title).ratio()
                if query and query in title:
                    score = max(score, .7)
                if query == title:
                    score = 1
                if raw in url:
                    score = max(score, .95)
                if id_query.isdecimal() and int(id_query) == sid:
                    score = 2
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
        if sort == 'average':
            average = rating_summary.get(sid, {}).get('avg_score')
            return average if average is not None else 0
        if sort == 'coverage':
            return rating_summary.get(sid, {}).get('rated_count', 0)
        if sort == 'relevance':
            return relevance.get(sid, 0)
        return song[sort] if song[sort] is not None else (0 if sort == 'play_count' else '')
    filtered.sort(key=lambda song: ((song['title'] or '').casefold(), song['song_id']))
    filtered.sort(key=sort_key, reverse=direction == 'desc')
    if sort == 'average':
        filtered.sort(key=lambda song: rating_summary.get(song['song_id'], {}).get('avg_score') is None)
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
        'progress': progress, 'libraryTotal': library_total, 'ratingOwner': session.get('discord_user_id'),
        'messages': RUSSIAN if session.get('language', 'ru') == 'ru' else ENGLISH,
    }
    return render_template('index.html', songs=songs, total=total, page=page, pages=pages,
        search_query=search_query, avg_filter=avg_filter, banned_filter=banned_filter, not_rated_by=not_rated_by,
        view=view, viewer=viewer, sort=sort, direction=direction, known_raters=known_raters,
        hide_mine=hide_mine, hide_full=hide_full,
        user_thresholds=user_thresholds, rating_summary=rating_summary, ratings_matrix=ratings_matrix,
        library_total=library_total, progress=progress, listeners=listeners, ignore_unrated=ignore_unrated,
        threshold=threshold, previews=previews, dashboard_data=dashboard_data,
        bot_guilds=bot_guilds, selected_guild=selected_guild, bot_settings=bot_settings,
        backup_settings=read_backup_settings(DB_NAME), duplicates=duplicates)


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


@app.route('/playback_control', methods=['POST'])
@login_required
def playback_control_route():
    guild_id = request.form.get('guild_id', '')
    action = request.form.get('action', '')
    if action not in ('toggle_pause', 'skip', 'replay'):
        return rating_error('Unknown playback control.', 400)
    runtime = query_db('SELECT updated_at,payload FROM bot_runtime WHERE guild_id=?', (guild_id,), one=True)
    if not runtime or time.time()-runtime['updated_at'] > 20:
        return rating_error('Bot offline or status outdated', 409)
    status = json.loads(runtime['payload'])
    expected = request.form.get('expected', '')
    if not status.get('voice') or not expected or expected != status.get('encoded'):
        return rating_error('The track changed. Refresh and try again.', 409)
    conn = get_db()
    with conn:
        cursor = conn.execute('INSERT INTO playback_requests(guild_id,action,expected,created_at) VALUES(?,?,?,?)',
                             (guild_id,action,expected,time.time()))
    return jsonify(id=cursor.lastrowid)


@app.route('/playback_request/<int:request_id>')
@login_required
def playback_request_route(request_id):
    row = query_db('SELECT result,error FROM playback_requests WHERE id=?', (request_id,), one=True)
    return jsonify(dict(row)) if row else (jsonify(error='Request expired.'), 404)


@app.route('/history')
@login_required
def history_route():
    guild_id = request.args.get('guild_id', '')
    rows = query_db('SELECT id,title,url,source,requester,started_at,result FROM playback_history WHERE guild_id=? ORDER BY id DESC LIMIT 50', (guild_id,))
    return jsonify(history=[dict(row) for row in rows])


@app.route('/import', methods=['GET', 'POST'])
@login_required
def import_songs():
    token = None
    preview = None
    if request.method == 'POST':
        owner = session.setdefault('import_owner', secrets.token_urlsafe(24))
        if request.form.get('confirm'):
            token = request.form.get('token', '')
            row = query_db('SELECT payload,created_at FROM import_previews WHERE token=? AND owner=?', (token,owner), one=True)
            if not row or time.time()-row['created_at'] > 3600:
                abort(400, t('Import preview expired. Preview the file again.'))
            preview = json.loads(row['payload'])
            selected = {int(x) for x in request.form.getlist('entry') if x.isdecimal()}
            conn = get_db()
            imported = 0
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                # Recheck duplicates since the preview; never overwrite existing ratings.
                current = conn.execute('SELECT title,url FROM songs').fetchall()
                links = {link_identity(song['url']) for song in current if song['url']}
                names = {normalize_search(song['title']) for song in current if song['title']}
                for index in sorted(selected):
                    if index >= len(preview):
                        continue
                    item = preview[index]
                    if item['error']:
                        continue
                    title,url = item['title'],item['url']
                    if url and link_identity(url) in links or not url and title and normalize_search(title) in names:
                        continue
                    conn.execute('INSERT INTO songs(title,url) VALUES(?,?)', (title,url))
                    imported += 1
                    if url: links.add(link_identity(url))
                    if title: names.add(normalize_search(title))
                conn.execute('DELETE FROM import_previews WHERE token=?', (token,))
            return render_template('import.html', preview=None, token=None, imported=imported)
        text = request.form.get('entries', '')
        file = request.files.get('file')
        if file and file.filename:
            try:
                text = file.read(2_000_001).decode('utf-8-sig')
            except UnicodeDecodeError:
                abort(400, t('Use a UTF-8 text or CSV file.'))
        try:
            preview = parse_import(text, query_db('SELECT title,url FROM songs'))
        except (ValueError, TypeError) as error:
            abort(400, str(error))
        if not preview:
            abort(400, t('No entries found.'))
        token = secrets.token_urlsafe(24)
        execute_db('DELETE FROM import_previews WHERE created_at<?', (time.time()-3600,))
        execute_db('INSERT INTO import_previews VALUES(?,?,?,?)', (token,owner,time.time(),json.dumps(preview)))
    return render_template('import.html', preview=preview, token=token, imported=None)


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
    if str(user_id) != session.get('discord_user_id'):
        return rating_error('You can edit only your own ratings.', 403)
    try:
        min_rating = int(request.form.get('min_rating', '0'))
    except ValueError:
        return rating_error('Invalid threshold', 400)
    if min_rating not in (-1, 0, 1, 2, 3):
        return rating_error('Minimum must be -1, 0, 1, 2 or 3.', 400)
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
    if str(user_id) != session.get('discord_user_id'):
        return rating_error('You can edit only your own ratings.', 403)
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


def valid_song_url(url):
    if not url:
        return True
    try:
        return len(url) <= 2048 and urlsplit(url).scheme in ('http', 'https') and bool(urlsplit(url).hostname)
    except ValueError:
        return False


@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_song():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        url = request.form.get('url', '').strip() or None
        if not url and title.startswith(('http://', 'https://')):
            url, title = title, ''
        if url:
            try:
                url, _ = clean_link(url)
            except ValueError:
                abort(400, t('Invalid link timecode.'))
        if title or url:
            if not valid_song_url(url):
                return render_template('song_form.html', song={'title':title, 'url':url, 'play_count':0}, action='Add', error=t('Use an http or https link.')), 400
            if url and any(link_identity(url) == link_identity(row['url']) for row in query_db('SELECT url FROM songs WHERE url IS NOT NULL')):
                return render_template('song_form.html', song={'title': title, 'url': url, 'play_count': 0}, action='Add', error=t('This recording link is already in the library.')), 409
            execute_db('INSERT INTO songs (title, url, play_count, last_played) VALUES (?, ?, 0, NULL)', (title or None, url))
            return redirect(url_for('index'))
        abort(400, t('Enter a title or link.'))
    return render_template('song_form.html', song=None, action='Add', error=None)


@app.route('/edit/<int:song_id>', methods=['GET', 'POST'])
@login_required
def edit_song(song_id):
    song = query_db('SELECT song_id, title, url, play_count FROM songs WHERE song_id = ?', (song_id,), one=True)
    if song is None:
        return redirect(url_for('index'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        url = request.form.get('url', '').strip() or None
        if not url and title.startswith(('http://', 'https://')):
            url, title = title, ''
        if url:
            try:
                url, _ = clean_link(url)
            except ValueError:
                abort(400, t('Invalid link timecode.'))
        if not title and not url:
            abort(400, t('Enter a title or link.'))
        if not valid_song_url(url):
            return render_template('song_form.html', song={'title':title, 'url':url, 'play_count':song['play_count']}, action='Edit', error=t('Use an http or https link.')), 400
        try:
            play_count = int(request.form.get('play_count', song['play_count'] or 0))
            if play_count < 0: raise ValueError()
        except ValueError:
            return render_template('song_form.html', song=song, action='Edit', error=t('Play count must be a nonnegative integer.')), 400
        if url and any(link_identity(url) == link_identity(row['url']) for row in query_db('SELECT url FROM songs WHERE song_id!=? AND url IS NOT NULL', (song_id,))):
            return render_template('song_form.html', song={'title':title, 'url':url, 'play_count':play_count}, action='Edit', error=t('This recording link is already in the library.')), 409
        changed_recording = link_identity(url) != link_identity(song['url'])
        if changed_recording and url and title == (song['title'] or ''):
            title = ''
        execute_db('UPDATE songs SET title=?,url=?,play_count=?,artwork_url=CASE WHEN ? THEN NULL ELSE artwork_url END WHERE song_id=?',
                   (title or None,url,play_count,changed_recording,song_id))
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
