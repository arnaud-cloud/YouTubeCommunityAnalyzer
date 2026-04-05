"""
Database helpers for the YouTube Community Analyzer.
"""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "community_analyzer.db"


def get_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open (or create) the database and ensure schema is applied."""
    db_path = str(db_path or DEFAULT_DB)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Read a single setting value."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Write a single setting value."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Return all settings as a dict."""
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_community_channel_ids(conn: sqlite3.Connection, community_id: int) -> list[str]:
    """Return the list of channel_ids for a community."""
    rows = conn.execute(
        "SELECT channel_id FROM community_channels WHERE community_id = ?",
        (community_id,),
    ).fetchall()
    return [r["channel_id"] for r in rows]
