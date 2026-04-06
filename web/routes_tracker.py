"""Tracker dashboard routes — channel metrics visualization + collection trigger."""

import json
import threading

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from core.db import get_db, get_setting, get_community_channel_ids
from core.youtube_api import build_youtube
from core.tracker import collect_community

bp = Blueprint("tracker", __name__)


@bp.route("/<int:community_id>")
def dashboard(community_id):
    conn = get_db(current_app.config["DB_PATH"])

    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.home"))

    channels = conn.execute("""
        SELECT ch.channel_id, ch.channel_name, ch.handle, ch.thumbnail_url
        FROM community_channels cc
        JOIN channels ch ON cc.channel_id = ch.channel_id
        WHERE cc.community_id = ?
        ORDER BY ch.channel_name
    """, (community_id,)).fetchall()

    all_communities = conn.execute(
        "SELECT id, name FROM communities ORDER BY name"
    ).fetchall()

    conn.close()
    return render_template(
        "tracker_dashboard.html",
        community=dict(community),
        channels=[dict(c) for c in channels],
        all_communities=[dict(c) for c in all_communities],
    )


@bp.route("/<int:community_id>/data")
def dashboard_data(community_id):
    """JSON API: all chart data for a community's tracker dashboard."""
    conn = get_db(current_app.config["DB_PATH"])
    channel_ids = get_community_channel_ids(conn, community_id)

    if not channel_ids:
        conn.close()
        return jsonify({"channels": [], "snapshots": [], "videos": [], "video_snapshots": []})

    placeholders = ",".join("?" * len(channel_ids))

    # Channel info
    channels = [dict(r) for r in conn.execute(
        f"SELECT channel_id, channel_name, handle, thumbnail_url FROM channels WHERE channel_id IN ({placeholders})",
        channel_ids,
    ).fetchall()]

    # Channel snapshots (time series)
    snapshots = [dict(r) for r in conn.execute(
        f"""SELECT channel_id, snapshot_date, subscriber_count, view_count, video_count
            FROM channel_snapshots
            WHERE channel_id IN ({placeholders})
            ORDER BY snapshot_date""",
        channel_ids,
    ).fetchall()]

    # Latest video catalog for each channel (top 50 by views)
    videos = []
    for cid in channel_ids:
        rows = conn.execute("""
            SELECT v.video_id, v.channel_id, v.title, v.published_at,
                   v.duration, v.thumbnail_url,
                   vs.view_count, vs.like_count, vs.comment_count
            FROM videos v
            LEFT JOIN video_snapshots vs ON v.video_id = vs.video_id
                AND vs.snapshot_date = (
                    SELECT MAX(vs2.snapshot_date) FROM video_snapshots vs2
                    WHERE vs2.video_id = v.video_id
                )
            WHERE v.channel_id = ?
            ORDER BY vs.view_count DESC
            LIMIT 50
        """, (cid,)).fetchall()
        videos.extend([dict(r) for r in rows])

    # Video snapshots time series (for velocity chart - recent videos only)
    video_snapshots = [dict(r) for r in conn.execute(
        f"""SELECT vs.video_id, vs.channel_id, vs.snapshot_date,
                   vs.view_count, vs.like_count, vs.comment_count
            FROM video_snapshots vs
            WHERE vs.channel_id IN ({placeholders})
            ORDER BY vs.snapshot_date""",
        channel_ids,
    ).fetchall()]

    conn.close()
    return jsonify({
        "channels": channels,
        "snapshots": snapshots,
        "videos": videos,
        "video_snapshots": video_snapshots,
    })


@bp.route("/<int:community_id>/collect", methods=["POST"])
def collect_now(community_id):
    """Trigger immediate tracker collection for this community."""
    conn = get_db(current_app.config["DB_PATH"])
    api_key = get_setting(conn, "youtube_api_key")
    if not api_key:
        conn.close()
        flash("YouTube API key not configured. Go to Settings.", "error")
        return redirect(url_for("tracker.dashboard", community_id=community_id))

    db_path = current_app.config["DB_PATH"]
    conn.close()

    def _run():
        c = get_db(db_path)
        try:
            yt = build_youtube(get_setting(c, "youtube_api_key"))
            collect_community(c, yt, community_id)
        finally:
            c.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    flash("Tracker collection started in background.", "success")
    return redirect(url_for("tracker.dashboard", community_id=community_id))
