"""Process-lifetime leases using SQLite write transactions."""

import sqlite3
from pathlib import Path


def acquire_sqlite_lease(path: Path, timeout: float) -> sqlite3.Connection:
    """Acquire a write lease on its own database until the caller closes it."""
    connection = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except BaseException as exc:
        connection.close()
        if isinstance(exc, sqlite3.OperationalError) and exc.sqlite_errorcode == sqlite3.SQLITE_BUSY:
            raise BlockingIOError(f"SQLite lease is held: {path}") from exc
        raise
    return connection
