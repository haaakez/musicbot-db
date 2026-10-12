"""Server settings shared by the bot and dashboard."""
import sqlite3

DEFAULTS = {'ignore_unrated': True, 'embed_ratings': False, 'voice_status': False}


def setup_settings(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY, ignore_unrated INTEGER NOT NULL DEFAULT 1
    )''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(guild_settings)').fetchall()}
    for name in ('embed_ratings', 'voice_status'):
        if name not in columns:
            conn.execute(f'ALTER TABLE guild_settings ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
    if 'guild_name' not in columns:
        conn.execute('ALTER TABLE guild_settings ADD COLUMN guild_name TEXT')


def read_settings(db_name, guild_id):
    with sqlite3.connect(db_name, timeout=30) as conn:
        row = conn.execute('SELECT ignore_unrated, embed_ratings, voice_status FROM guild_settings WHERE guild_id=?',
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
