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
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations for columns added after initial release."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(video_summaries)")}
    if "last_comment_published_at" not in existing:
        conn.execute(
            "ALTER TABLE video_summaries ADD COLUMN last_comment_published_at TEXT"
        )
        # Backfill from the comments table so existing rows don't get re-processed
        conn.execute("""
            UPDATE video_summaries
            SET last_comment_published_at = (
                SELECT MAX(published_at) FROM comments
                WHERE comments.video_id = video_summaries.video_id
            )
            WHERE last_comment_published_at IS NULL
        """)

    # executive_reports: report_json column
    if "executive_reports" in {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }:
        er_cols = {row[1] for row in conn.execute("PRAGMA table_info(executive_reports)")}
        if "report_json" not in er_cols:
            conn.execute("ALTER TABLE executive_reports ADD COLUMN report_json TEXT")

    runs_cols = {row[1] for row in conn.execute("PRAGMA table_info(gossip_runs)")}
    if "progress_log" not in runs_cols:
        conn.execute(
            "ALTER TABLE gossip_runs ADD COLUMN progress_log TEXT DEFAULT ''"
        )
    if "quota_units" not in runs_cols:
        conn.execute(
            "ALTER TABLE gossip_runs ADD COLUMN quota_units INTEGER DEFAULT 0"
        )

    # Multi-source migration: new columns on comments + videos
    comments_cols = {row[1] for row in conn.execute("PRAGMA table_info(comments)")}
    if "source_type" not in comments_cols:
        conn.execute("ALTER TABLE comments ADD COLUMN source_type TEXT DEFAULT 'youtube'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comments_source_type ON comments(source_type)"
        )
    if "engagement_normalized" not in comments_cols:
        conn.execute("ALTER TABLE comments ADD COLUMN engagement_normalized REAL")

    videos_cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "source_type" not in videos_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN source_type TEXT DEFAULT 'youtube'")

    # commenter_scores: new columns
    cs_cols = {row[1] for row in conn.execute("PRAGMA table_info(commenter_scores)")}
    if "reply_ratio" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN reply_ratio REAL")
    if "vocab_richness_score" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN vocab_richness_score REAL")
    if "llm_tone_score" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN llm_tone_score REAL")
    if "llm_politeness_score" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN llm_politeness_score REAL")
    if "llm_constructiveness_score" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN llm_constructiveness_score REAL")
    if "llm_depth_score" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN llm_depth_score REAL")
    if "llm_tone_reason" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN llm_tone_reason TEXT")
    if "llm_tone_backend" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN llm_tone_backend TEXT")
    if "llm_tone_model" not in cs_cols:
        conn.execute("ALTER TABLE commenter_scores ADD COLUMN llm_tone_model TEXT")

    # gossip_items: evidence_quality_score column
    gi_cols = {row[1] for row in conn.execute("PRAGMA table_info(gossip_items)")}
    if "evidence_quality_score" not in gi_cols:
        conn.execute("ALTER TABLE gossip_items ADD COLUMN evidence_quality_score REAL")

    # Migrate community_channels → community_sources (one-time, only if sources is empty)
    sources_empty = conn.execute(
        "SELECT COUNT(*) FROM community_sources"
    ).fetchone()[0] == 0
    old_channels_exist = conn.execute(
        "SELECT COUNT(*) FROM community_channels"
    ).fetchone()[0] > 0
    if sources_empty and old_channels_exist:
        conn.execute("""
            INSERT OR IGNORE INTO community_sources
                (community_id, source_type, source_id, display_name)
            SELECT cc.community_id, 'youtube', cc.channel_id,
                   COALESCE(ch.channel_name, cc.channel_id)
            FROM community_channels cc
            LEFT JOIN channels ch ON cc.channel_id = ch.channel_id
        """)


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


def get_community_sources(conn: sqlite3.Connection,
                          community_id: int) -> list[dict]:
    """Return all sources for a community as [{source_type, source_id, display_name}]."""
    rows = conn.execute(
        "SELECT source_type, source_id, display_name, config_json "
        "FROM community_sources WHERE community_id = ? ORDER BY added_at",
        (community_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_community_channel_ids(conn: sqlite3.Connection, community_id: int) -> list[str]:
    """Return all source_ids for a community (all platforms combined).

    Queries community_sources first; falls back to legacy community_channels
    table if community_sources is empty (pre-migration databases).
    """
    rows = conn.execute(
        "SELECT source_id FROM community_sources WHERE community_id = ?",
        (community_id,),
    ).fetchall()
    if rows:
        return [r["source_id"] for r in rows]
    # Fallback for pre-migration databases
    rows = conn.execute(
        "SELECT channel_id FROM community_channels WHERE community_id = ?",
        (community_id,),
    ).fetchall()
    return [r["channel_id"] for r in rows]
