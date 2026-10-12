from flask import Flask, g, redirect, render_template, request, session, url_for, abort
import sqlite3
import os
import json
import urllib.request
import difflib
import re
import math
import secrets
import hmac

DB_NAME = 'music_bot.db'
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'password')
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN') or os.environ.get('DISCORD_TOKEN')


def login_required(func):
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
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
        f'SELECT song_id, AVG(CASE WHEN score >= 0 THEN score END) AS avg_score, COUNT(CASE WHEN score >= 0 THEN 1 END) AS count FROM ratings WHERE song_id IN ({placeholders}) GROUP BY song_id',
        tuple(song_ids)
    )
    return {row['song_id']: {'avg_score': row['avg_score'], 'count': row['count']} for row in rows}


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
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            username = data.get('username')
            discriminator = data.get('discriminator')
            if username and discriminator:
                return f'{username}#{discriminator}'
            return username
    except Exception:
        return None


def get_known_raters():
    rows = query_db('SELECT user_id, MAX(username) AS username FROM ratings WHERE user_id IS NOT NULL GROUP BY user_id ORDER BY username COLLATE NOCASE')
    raters = []
    for row in rows:
        user_id = row['user_id']
        username = row['username']
        if not username or not username.strip():
            username = fetch_discord_username(user_id)
            if username:
                execute_db('UPDATE ratings SET username = ? WHERE user_id = ? AND (username IS NULL OR username = "")', (username, user_id))
        if not username or not username.strip():
            username = f'User {user_id}'
        raters.append({'display': username, 'value': f'id:{user_id}', 'user_id': user_id})
    return raters


def get_user_thresholds():
    rows = query_db('SELECT user_id, min_rating FROM user_thresholds')
    return {row['user_id']: row['min_rating'] for row in rows}


def get_username_for_user_id(user_id):
    row = query_db('SELECT username FROM ratings WHERE user_id = ? AND username IS NOT NULL LIMIT 1', (user_id,), one=True)
    db_username = row['username'] if row else None
    return db_username or fetch_discord_username(user_id)


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
            return int(value.split(':', 1)[1]), None
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
        args = request.args.to_dict()
        args['page'] = page
        return url_for('index', **args)
    return {'csrf_token': csrf_token, 'page_url': page_url}


@app.before_request
def protect_forms():
    if request.method == 'POST':
        expected = session.get('csrf_token', '')
        supplied = request.form.get('csrf_token', '')
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, 'This form expired. Reload the page and try again.')


def back_to_library():
    # Only construct a local library URL; never trust a submitted redirect URL.
    return redirect(url_for('index') + '?' + request.form.get('filters', ''))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    search_query = request.args.get('q', '').strip()
    avg_filter = request.args.get('avg_filter', 'all')
    banned_filter = request.args.get('banned_filter', 'all')
    not_rated_by = request.args.get('not_rated_by', 'all')

    if search_query:
        norm_q = normalize_search(search_query)
        all_rows = query_db('SELECT song_id, title, url, play_count, last_played FROM songs')
        scored = []
        for row in all_rows:
            title = row['title'] or ''
            title_norm = normalize_search(title)
            if not title_norm:
                continue

            ratio = difflib.SequenceMatcher(None, norm_q, title_norm).ratio()
            score = ratio

            if title_norm == norm_q:
                score = max(score, 0.99)
            elif title_norm.startswith(norm_q):
                score = max(score, 0.85)
            elif norm_q in title_norm:
                score = max(score, 0.7)

            play_count = row['play_count'] or 0
            score += min(play_count, 100) * 0.001

            if score >= 0.25:
                scored.append((score, row))

        scored.sort(key=lambda x: (-x[0], - (x[1]['play_count'] or 0), x[1]['title'] or ''))
        songs = [r for _, r in scored]
    else:
        songs = query_db('SELECT song_id, title, url, play_count, last_played FROM songs ORDER BY title COLLATE NOCASE')

    song_ids = [song['song_id'] for song in songs]
    rating_summary = get_rating_summary(song_ids)
    song_ratings = get_ratings_for_song_ids(song_ids)
    known_raters = get_known_raters()
    user_thresholds = get_user_thresholds()
    ratings_matrix = get_ratings_matrix(song_ids, known_raters)

    avg_filter_parsed = parse_average_filter(avg_filter)
    selected_rater_id = None
    if not_rated_by and not_rated_by.startswith('id:'):
        try:
            selected_rater_id = int(not_rated_by.split(':', 1)[1])
        except ValueError:
            selected_rater_id = None

    filtered_songs = []
    for song in songs:
        song_id = song['song_id']
        summary = rating_summary.get(song_id, {'avg_score': None, 'count': 0})
        avg_score = summary['avg_score']

        if avg_filter_parsed:
            op, threshold = avg_filter_parsed
            if avg_score is None:
                continue
            if op == '>=' and avg_score < threshold:
                continue
            if op == '=' and avg_score != threshold:
                continue

        if banned_filter == 'banned' and not is_song_banned(song_id, song_ratings):
            continue
        if banned_filter == 'not_banned' and is_song_banned(song_id, song_ratings):
            continue

        if selected_rater_id is not None:
            if ratings_matrix.get(song_id, {}).get(selected_rater_id) is not None:
                continue

        filtered_songs.append(song)

    songs = filtered_songs
    song_ids = [song['song_id'] for song in songs]
    ratings_matrix = get_ratings_matrix(song_ids, known_raters)

    total = len(songs)
    pages = max(1, math.ceil(total / 50))
    try:
        page = max(1, min(pages, int(request.args.get('page', 1))))
    except ValueError:
        page = 1
    songs = songs[(page - 1) * 50:page * 50]
    return render_template(
        'index.html',
        total=total, page=page, pages=pages,
        songs=songs,
        search_query=search_query,
        avg_filter=avg_filter,
        banned_filter=banned_filter,
        not_rated_by=not_rated_by,
        rating_summary=rating_summary,
        song_ratings=song_ratings,
        known_raters=known_raters,
        user_thresholds=user_thresholds,
        ratings_matrix=ratings_matrix
    )


@app.route('/update_threshold/<int:user_id>', methods=['POST'])
@login_required
def update_threshold(user_id):
    try:
        min_rating = float(request.form.get('min_rating', '0.0'))
    except ValueError:
        abort(400, 'Invalid threshold')
    if not math.isfinite(min_rating) or not 0 <= min_rating <= 3:
        abort(400, 'Threshold must be between 0 and 3')
    execute_db('INSERT OR REPLACE INTO user_thresholds (user_id, min_rating) VALUES (?, ?)', (user_id, min_rating))
    return back_to_library()


@app.route('/rate/<int:song_id>', methods=['POST'])
@login_required
def rate_song(song_id):
    rater_value = request.form.get('rater', '').strip()
    user_id, username = parse_rater_value(rater_value)

    if user_id is not None and not username:
        username = get_username_for_user_id(user_id)

    if user_id is None:
        abort(400, 'Choose a Discord user')
    if request.form.get('score') == 'unrated':
        execute_db('DELETE FROM ratings WHERE song_id = ? AND user_id = ?', (song_id, user_id))
        return back_to_library()
    try:
        score = int(request.form.get('score', ''))
    except ValueError:
        abort(400, 'Invalid rating')

    if score not in [-1, 0, 1, 2, 3]:
        abort(400, 'Invalid rating')

    execute_db(
        'INSERT OR REPLACE INTO ratings (song_id, user_id, username, score) VALUES (?, ?, ?, ?)',
        (song_id, user_id, username, score)
    )
    if user_id is not None and username:
        execute_db('UPDATE ratings SET username = ? WHERE user_id = ? AND username IS NULL', (username, user_id))
    return back_to_library()


@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_song():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        url = request.form.get('url', '').strip() or None
        if title:
            execute_db('INSERT INTO songs (title, url, play_count, last_played) VALUES (?, ?, 0, NULL)', (title, url))
            return redirect(url_for('index'))
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
        play_count = int(request.form.get('play_count', song['play_count'] or 0))
        execute_db('UPDATE songs SET title = ?, url = ?, play_count = ? WHERE song_id = ?', (title, url, play_count, song_id))
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