"""Small local HTTP-policy cache, not a second job queue.

Jobs/results/retries stay in PostgreSQL. This disposable cache preserves robots
policies and request spacing across short-lived crawler runs on the same host.
"""

import sqlite3
import threading
import time
from pathlib import Path


class HttpCache:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        with self._connection:
            self._connection.executescript('''
                CREATE TABLE IF NOT EXISTS robots (
                    origin TEXT PRIMARY KEY, policy TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    host TEXT PRIMARY KEY, requested REAL NOT NULL);
            ''')
            self._connection.execute('DELETE FROM robots WHERE expires <= ?', (time.time(),))
            self._connection.execute('DELETE FROM requests WHERE requested < ?', (time.time() - 86400,))

    def close(self):
        with self._lock:
            self._connection.close()

    def robots(self, origin):
        with self._lock:
            row = self._connection.execute(
                'SELECT policy, expires FROM robots WHERE origin = ? AND expires > ?',
                (origin, time.time()),
            ).fetchone()
            return row

    def save_robots(self, origin, policy, ttl):
        with self._lock, self._connection:
            self._connection.execute(
                'INSERT OR REPLACE INTO robots VALUES (?, ?, ?)',
                (origin, policy, time.time() + ttl),
            )

    def recent_requests(self):
        with self._lock:
            return dict(self._connection.execute('SELECT host, requested FROM requests'))

    def requested(self, host):
        with self._lock, self._connection:
            self._connection.execute('INSERT OR REPLACE INTO requests VALUES (?, ?)', (host, time.time()))
