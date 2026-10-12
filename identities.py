"""Keep Discord names independently of ratings, including after a rating is cleared."""
import sqlite3
import time


def setup_users(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        checked_at REAL NOT NULL DEFAULT 0
    )''')
    conn.execute('''INSERT OR IGNORE INTO users (user_id, username)
        SELECT user_id, MAX(NULLIF(TRIM(username), '')) FROM ratings
        WHERE user_id IS NOT NULL GROUP BY user_id''')
    conn.execute('''INSERT OR IGNORE INTO users (user_id)
        SELECT user_id FROM user_thresholds''')


def remember_user(db_name, user_id, username):
    with sqlite3.connect(db_name, timeout=30) as conn:
        conn.execute('''INSERT INTO users (user_id, username, checked_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
            checked_at=excluded.checked_at''', (user_id, username, time.time()))
        conn.execute('UPDATE ratings SET username = ? WHERE user_id = ?', (username, user_id))
