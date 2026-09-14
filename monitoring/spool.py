"""Atomic local queue: small JPEG blobs and metadata share one SQLite transaction."""
import json
import shutil
import threading
import time
from pathlib import Path

from .common import canonical, database


class Spool:
    def __init__(self, directory, max_bytes=512 * 1024**2, max_age=86400,
                 reserve_bytes=1024**3):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "queue.db"
        self.max_bytes, self.max_age, self.reserve_bytes = max_bytes, max_age, reserve_bytes
        self.lock = threading.RLock()
        with database(self.path) as db:
            db.execute("PRAGMA auto_vacuum=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS queue (
                  id INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                  kind TEXT NOT NULL, event TEXT NOT NULL, photo BLOB,
                  created REAL NOT NULL, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO counters VALUES ('dropped', 0);
            """)

    def _drop(self, db, ids):
        if ids:
            db.executemany("DELETE FROM queue WHERE id=?", [(i,) for i in ids])
            db.execute("UPDATE counters SET value=value+? WHERE key='dropped'", (len(ids),))

    def prune(self, now=None):
        now = time.time() if now is None else now
        with self.lock, database(self.path) as db:
            # A large backward clock correction makes age unknowable. Conservatively drop
            # future enqueues; the hard disk quota remains independent of wall-clock time.
            ids = [r[0] for r in db.execute(
                "SELECT id FROM queue WHERE created<? OR created>?",
                (now - self.max_age, now + 300))]
            self._drop(db, ids)
            rows = list(db.execute("SELECT id,size FROM queue ORDER BY id"))
            total = sum(r[1] for r in rows)
            deficit = max(0, self.reserve_bytes - shutil.disk_usage(self.directory).free)
            ids = []
            for row in rows:
                if total <= self.max_bytes and deficit <= 0:
                    break
                ids.append(row[0])
                total -= row[1]
                deficit -= row[1]
            self._drop(db, ids)

    def enqueue(self, event, photo=None, now=None):
        encoded = canonical(event)
        size = len(encoded.encode()) + len(photo or b"")
        with self.lock:
            self.prune(now)
            with database(self.path) as db:
                if size > self.max_bytes or size > 4 * 1024**2 + 65536:
                    db.execute("UPDATE counters SET value=value+1 WHERE key='dropped'")
                    return False
                # Evict before writing: never temporarily exceed the configured quota.
                total = db.execute("SELECT COALESCE(SUM(size),0) FROM queue").fetchone()[0]
                free = shutil.disk_usage(self.directory).free
                rows = iter(db.execute("SELECT id,size FROM queue ORDER BY id").fetchall())
                dropped = []
                while total + size > self.max_bytes or free - size < self.reserve_bytes:
                    row = next(rows, None)
                    if row is None:
                        self._drop(db, dropped)
                        db.execute("UPDATE counters SET value=value+1 WHERE key='dropped'")
                        return False
                    dropped.append(row[0])
                    total -= row[1]
                    free += row[1]
                self._drop(db, dropped)
                db.execute("INSERT OR IGNORE INTO queue(event_id,kind,event,photo,created,size) "
                           "VALUES (?,?,?,?,?,?)", (event["event_id"], event["kind"], encoded,
                                                    photo, time.time() if now is None else now, size))
            self.prune(now)
            return True

    def batch(self, kind, limit=32):
        with self.lock, database(self.path) as db:
            return [(json.loads(r["event"]), r["photo"]) for r in db.execute(
                "SELECT event,photo FROM queue WHERE kind=? ORDER BY id LIMIT ?", (kind, limit))]

    def acknowledge(self, event_ids):
        with self.lock, database(self.path) as db:
            db.executemany("DELETE FROM queue WHERE event_id=?", [(i,) for i in event_ids])

    def status(self):
        with self.lock, database(self.path) as db:
            row = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0),MIN(created) FROM queue").fetchone()
            return dict(queued=row[0], bytes=row[1],
                        oldest_age_seconds=max(0, time.time() - row[2]) if row[2] else 0,
                        dropped=db.execute("SELECT value FROM counters WHERE key='dropped'").fetchone()[0])
