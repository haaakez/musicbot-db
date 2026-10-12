"""Consistent SQLite snapshots, usable while the bot and dashboard are running."""
import argparse
from contextlib import closing
import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import time

log = logging.getLogger(__name__)


def backup_database(database='music_bot.db', directory='backups', keep=28):
    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    if keep < 1:
        raise ValueError('keep must be at least 1')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    final = directory / f'{database.stem}-{timestamp}.sqlite3'
    descriptor, temporary = tempfile.mkstemp(prefix='.backup-', suffix='.tmp', dir=directory)
    os.close(descriptor)
    deadline = time.monotonic() + 120

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('database backup exceeded two minutes')

    try:
        # The backup API includes committed WAL data without copying live sidecars.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=30)) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination, pages=256, progress=progress, sleep=.1)
                if destination.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise RuntimeError('backup failed SQLite integrity check')
        with open(temporary, 'rb') as snapshot:
            os.fsync(snapshot.fileno())
        os.replace(temporary, final)
        # Prune only this database's completed snapshots after a successful backup.
        snapshots = sorted(directory.glob(f'{database.stem}-*.sqlite3'), reverse=True)
        for old in snapshots[keep:]:
            old.unlink()
        log.info('Database backup saved: %s', final)
        return final
    finally:
        Path(temporary).unlink(missing_ok=True)


async def maintain_backups(database):
    interval = max(1, float(os.environ.get('DB_BACKUP_HOURS', '6'))) * 3600
    directory = os.environ.get('DB_BACKUP_DIR', 'backups')
    keep = max(1, int(os.environ.get('DB_BACKUP_KEEP', '28')))
    while True:
        try:
            path = await asyncio.to_thread(backup_database, database, directory, keep)
            print(f'database backup saved: {path}')
        except Exception:
            log.exception('Database backup failed; playback will continue')
        await asyncio.sleep(interval)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='music_bot.db')
    parser.add_argument('--directory', default=os.environ.get('DB_BACKUP_DIR', 'backups'))
    parser.add_argument('--keep', type=int, default=int(os.environ.get('DB_BACKUP_KEEP', '28')))
    args = parser.parse_args()
    print(backup_database(args.database, args.directory, args.keep))
