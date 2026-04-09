"""Main routes — landing page, community hub, DB stats."""

import os

from flask import Blueprint, render_template, current_app, jsonify, redirect, url_for
from core.db import get_db, get_community_channel_ids

bp = Blueprint("main", __name__)


def _format_bytes(n):
    """Human-readable file size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _db_total_size(db_path):
    """Get total DB file size in bytes."""
    try:
        return os.path.getsize(db_path)
    except OSError:
        return 0


def _db_community_sizes(conn, communities):
    """Estimate DB usage per community based on comment text size."""
    sizes = {}
    for com in communities:
        cid = com["id"]
        channel_ids = get_community_channel_ids(conn, cid)
        if not channel_ids:
            sizes[cid] = 0
            continue
        ph = ",".join("?" * len(channel_ids))
        row = conn.execute(
            f"SELECT COALESCE(SUM(LENGTH(text)), 0) AS text_bytes FROM comments WHERE channel_id IN ({ph})",
            channel_ids,
        ).fetchone()
        # Comment text is the dominant storage; multiply by ~1.5 for overhead (indexes, other tables)
        sizes[cid] = int(row["text_bytes"] * 1.5) if row else 0
    return sizes


@bp.route("/")
def landing():
    return render_template("landing.html")


@bp.route("/app")
def hub():
    conn = get_db(current_app.config["DB_PATH"])
    communities = conn.execute("""
        SELECT c.id, c.name, c.description, c.created_at,
               COUNT(cc.channel_id) AS channel_count
        FROM communities c
        LEFT JOIN community_channels cc ON c.id = cc.community_id
        GROUP BY c.id
        ORDER BY c.name
    """).fetchall()

    # Enrich with latest tracker and gossip dates
    enriched = []
    for com in communities:
        d = dict(com)
        row = conn.execute("""
            SELECT MAX(cs.snapshot_date) AS last_snapshot
            FROM channel_snapshots cs
            JOIN community_channels cc ON cs.channel_id = cc.channel_id
            WHERE cc.community_id = ?
        """, (d["id"],)).fetchone()
        d["last_snapshot"] = row["last_snapshot"] if row else None

        row = conn.execute("""
            SELECT status, started_at FROM gossip_runs
            WHERE community_id = ?
            ORDER BY id DESC LIMIT 1
        """, (d["id"],)).fetchone()
        d["last_gossip_status"] = row["status"] if row else None
        d["last_gossip_date"] = row["started_at"] if row else None

        row = conn.execute("""
            SELECT SUM(cs.subscriber_count) AS total_subs
            FROM channel_snapshots cs
            JOIN community_channels cc ON cs.channel_id = cc.channel_id
            WHERE cc.community_id = ?
            AND cs.snapshot_date = (
                SELECT MAX(cs2.snapshot_date) FROM channel_snapshots cs2
                WHERE cs2.channel_id = cs.channel_id
            )
        """, (d["id"],)).fetchone()
        d["total_subs"] = row["total_subs"] or 0 if row else 0

        # Pipeline status mini-dots
        from .routes_gossip import _get_pipeline_status
        d["pipeline_status"] = _get_pipeline_status(conn, d["id"])

        enriched.append(d)

    # DB sizes
    db_path = current_app.config["DB_PATH"]
    total_bytes = _db_total_size(db_path)
    community_sizes = _db_community_sizes(conn, enriched)
    for d in enriched:
        d["db_size_display"] = _format_bytes(community_sizes.get(d["id"], 0))

    conn.close()
    return render_template("hub.html",
                           communities=enriched,
                           db_size_bytes=total_bytes,
                           db_size_display=_format_bytes(total_bytes))


# Keep old home route as redirect for backward compat
@bp.route("/home")
def home():
    return redirect(url_for("main.hub"))


@bp.route("/api/db-stats")
def db_stats():
    db_path = current_app.config["DB_PATH"]
    total_bytes = _db_total_size(db_path)
    conn = get_db(db_path)
    communities = conn.execute("SELECT id, name FROM communities ORDER BY name").fetchall()
    community_sizes = _db_community_sizes(conn, communities)
    conn.close()
    return jsonify({
        "total_bytes": total_bytes,
        "total_display": _format_bytes(total_bytes),
        "communities": [
            {"id": c["id"], "name": c["name"],
             "estimated_bytes": community_sizes.get(c["id"], 0),
             "display": _format_bytes(community_sizes.get(c["id"], 0))}
            for c in communities
        ],
    })
