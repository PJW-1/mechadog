"""Local DB maintenance helpers. Runtime readers never create a missing database."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def open_readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def backup_database(path):
    """Use SQLite's backup API so WAL contents are included; never overwrite a backup."""
    path = Path(path)
    if not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = path.with_name(f"{path.name}.{stamp}.bak")
    with dest.open("xb"):
        pass
    try:
        src = open_readonly(path)
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest


def has_rows(conn, tables):
    """tables must be code-owned identifiers, never input from an import file."""
    return any(conn.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone() for t in tables)
