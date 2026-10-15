"""Single-owner SQLite state under Home Assistant's configured storage directory."""
import fcntl
import os
import sqlite3
from pathlib import Path
from .engine import Store, fail

class LocalStore(Store):
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            fail('storage_path_invalid')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / 'planner.sqlite3'
        if path.is_symlink():
            fail('storage_path_invalid')
        self.lock_fd = os.open(self.root / 'planner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        connection = None
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            super().__init__(connection)
        except Exception:
            if connection is not None:
                connection.close()
            os.close(self.lock_fd)
            raise

    def close(self):
        super().close()
        os.close(self.lock_fd)
