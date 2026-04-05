"""Main routes — home page."""

from flask import Blueprint, render_template, current_app
from core.db import get_db

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
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
        # Latest tracker snapshot date
        row = conn.execute("""
            SELECT MAX(cs.snapshot_date) AS last_snapshot
            FROM channel_snapshots cs
            JOIN community_channels cc ON cs.channel_id = cc.channel_id
            WHERE cc.community_id = ?
        """, (d["id"],)).fetchone()
        d["last_snapshot"] = row["last_snapshot"] if row else None

        # Latest gossip run
        row = conn.execute("""
            SELECT status, started_at FROM gossip_runs
            WHERE community_id = ?
            ORDER BY id DESC LIMIT 1
        """, (d["id"],)).fetchone()
        d["last_gossip_status"] = row["status"] if row else None
        d["last_gossip_date"] = row["started_at"] if row else None

        # Total subscribers across channels
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

        enriched.append(d)

    conn.close()
    return render_template("home.html", communities=enriched)
