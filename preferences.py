"""Server settings shared by the bot and dashboard."""
import sqlite3
import os

DEFAULTS = {'ignore_unrated': True, 'embed_ratings': False, 'voice_status': False, 'radio_always': False}


def setup_settings(conn):
    # Preserve legacy cutoffs when moving to integer-only personal minima.
    conn.execute('''UPDATE user_thresholds SET min_rating=CASE
        WHEN min_rating IS NULL THEN 0 WHEN min_rating < -1 THEN -1 WHEN min_rating > 3 THEN 3
        ELSE CAST(min_rating AS INTEGER) + (min_rating > CAST(min_rating AS INTEGER)) END
        WHERE min_rating IS NULL OR min_rating < -1 OR min_rating > 3
           OR min_rating != CAST(min_rating AS INTEGER)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY, ignore_unrated INTEGER NOT NULL DEFAULT 1
    )''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(guild_settings)').fetchall()}
    for name in ('embed_ratings', 'voice_status', 'radio_always'):
        if name not in columns:
            conn.execute(f'ALTER TABLE guild_settings ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
    conn.execute("""CREATE TABLE IF NOT EXISTS backup_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), interval_hours REAL NOT NULL DEFAULT 6,
        last_success REAL, last_error TEXT)""")
    conn.execute('INSERT OR IGNORE INTO backup_settings(id, interval_hours) VALUES(1, ?)',
                 (max(1, float(os.environ.get('DB_BACKUP_HOURS', '6'))),))
    conn.execute("""CREATE TABLE IF NOT EXISTS bot_runtime (
        guild_id INTEGER PRIMARY KEY, updated_at REAL NOT NULL, payload TEXT NOT NULL)""")
    if 'guild_name' not in columns:
        conn.execute('ALTER TABLE guild_settings ADD COLUMN guild_name TEXT')


def read_settings(db_name, guild_id):
    with sqlite3.connect(db_name, timeout=30) as conn:
        row = conn.execute('SELECT ignore_unrated, embed_ratings, voice_status, radio_always FROM guild_settings WHERE guild_id=?',
                           (guild_id,)).fetchone()
    return dict(zip(DEFAULTS, map(bool, row))) if row else DEFAULTS.copy()


def write_settings(db_name, guild_id, **values):
    if not values or set(values) - DEFAULTS.keys():
        raise ValueError('unknown server setting')
    with sqlite3.connect(db_name, timeout=30) as conn:
        conn.execute('INSERT OR IGNORE INTO guild_settings(guild_id) VALUES(?)', (guild_id,))
        for name, value in values.items():
            conn.execute(f'UPDATE guild_settings SET {name}=? WHERE guild_id=?', (int(bool(value)), guild_id))


def register_guild(db_name, guild_id, name):
    with sqlite3.connect(db_name, timeout=30) as conn:
        conn.execute('''INSERT INTO guild_settings(guild_id, guild_name) VALUES(?,?)
            ON CONFLICT(guild_id) DO UPDATE SET guild_name=excluded.guild_name''', (guild_id, name))


def read_backup_settings(db_name):
    with sqlite3.connect(db_name, timeout=30) as conn:
        row = conn.execute('SELECT interval_hours, last_success, last_error FROM backup_settings WHERE id=1').fetchone()
    return dict(zip(('interval_hours', 'last_success', 'last_error'), row))
