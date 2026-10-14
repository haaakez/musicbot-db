"""Playback history and short-lived dashboard control requests."""
import asyncio
import json
import sqlite3
import time
from contextlib import closing


def setup_playback(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS playback_history (
        id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, song_id INTEGER,
        title TEXT NOT NULL, url TEXT, source TEXT, requester TEXT, encoded TEXT,
        started_at REAL NOT NULL, ended_at REAL, result TEXT NOT NULL DEFAULT 'playing')''')
    conn.execute('CREATE INDEX IF NOT EXISTS history_guild_date ON playback_history(guild_id, id DESC)')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(playback_history)')}
    if 'counted' not in columns:
        conn.execute('ALTER TABLE playback_history ADD COLUMN counted INTEGER NOT NULL DEFAULT 0')
        conn.execute('UPDATE playback_history SET counted=1')
    conn.execute('''CREATE TABLE IF NOT EXISTS playback_requests (
        id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, action TEXT NOT NULL,
        expected TEXT NOT NULL, created_at REAL NOT NULL, result TEXT NOT NULL DEFAULT 'pending', error TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS import_previews (
        token TEXT PRIMARY KEY, owner TEXT NOT NULL, created_at REAL NOT NULL, payload TEXT NOT NULL)''')


def record_start(database, guild_id, track, song_id):
    with closing(sqlite3.connect(database, timeout=30)) as conn, conn:
        resume_id = getattr(track.extras, 'recovery_history_id', None)
        old = conn.execute('SELECT id FROM playback_history WHERE id=? AND guild_id=? AND encoded=?',
                           (resume_id,guild_id,track.encoded)).fetchone() if resume_id else None
        if old:
            conn.execute("UPDATE playback_history SET result='playing',ended_at=NULL WHERE id=?", (old[0],))
            return old[0]
        cursor = conn.execute('''INSERT INTO playback_history(guild_id,song_id,title,url,source,requester,encoded,started_at,counted)
            VALUES(?,?,?,?,?,?,?,?,0)''', (guild_id, song_id, track.title, track.uri, track.source,
            getattr(track.extras, 'requester_name', 'radio'), track.encoded, time.time()))
        history_id = cursor.lastrowid
        conn.execute('''DELETE FROM playback_history WHERE guild_id=? AND id NOT IN
            (SELECT id FROM playback_history WHERE guild_id=? ORDER BY id DESC LIMIT 500)''', (guild_id, guild_id))
        return history_id


def count_play(database, history_id):
    with closing(sqlite3.connect(database, timeout=30)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT song_id,counted FROM playback_history WHERE id=?', (history_id,)).fetchone()
        if not row or row[1]:
            return None
        conn.execute('UPDATE playback_history SET counted=1 WHERE id=?', (history_id,))
        if row[0] is not None:
            conn.execute('UPDATE songs SET play_count=COALESCE(play_count,0)+1,last_played=CURRENT_TIMESTAMP WHERE song_id=?', (row[0],))
            result = conn.execute('SELECT play_count FROM songs WHERE song_id=?', (row[0],)).fetchone()
            return result[0] if result else None


def record_end(database, guild_id, track, result):
    with closing(sqlite3.connect(database, timeout=30)) as conn, conn:
        conn.execute('''UPDATE playback_history SET ended_at=?, result=? WHERE id=(
            SELECT id FROM playback_history WHERE guild_id=? AND encoded=? AND result='playing'
            ORDER BY id DESC LIMIT 1)''', (time.time(), result, guild_id, track.encoded))


def take_requests(database):
    with closing(sqlite3.connect(database, timeout=30)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE playback_requests SET result='error',error='Request expired.' WHERE result IN ('pending','running') AND created_at<?", (time.time()-20,))
        rows = conn.execute("SELECT id,guild_id,action,expected FROM playback_requests WHERE result='pending' ORDER BY id LIMIT 10").fetchall()
        for row in rows:
            conn.execute("UPDATE playback_requests SET result='running' WHERE id=?", (row[0],))
        conn.execute('DELETE FROM playback_requests WHERE created_at<?', (time.time()-86400,))
        return rows


def finish_request(database, request_id, error):
    with closing(sqlite3.connect(database, timeout=30)) as conn, conn:
        conn.execute('UPDATE playback_requests SET result=?,error=? WHERE id=?',
                     ('error' if error else 'done', str(error)[:300] if error else None, request_id))


async def maintain_controls(database, execute):
    while True:
        try:
            rows = await asyncio.to_thread(take_requests, database)
            for request_id, guild_id, action, expected in rows:
                error = None
                try:
                    await asyncio.wait_for(execute(guild_id, action, expected), timeout=10)
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                await asyncio.to_thread(finish_request, database, request_id, error)
        except Exception as exc:
            print(f'dashboard controls failed: {exc}')
        await asyncio.sleep(.5)
