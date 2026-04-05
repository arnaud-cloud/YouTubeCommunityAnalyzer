"""Community CRUD routes — create, edit, delete communities + manage channels."""

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, current_app, jsonify,
)
from core.db import get_db, get_setting
from core.youtube_api import build_youtube, resolve_channel_id

bp = Blueprint("community", __name__)


@bp.route("/new", methods=["GET", "POST"])
def new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        if not name:
            flash("Community name is required.", "error")
            return render_template("community_edit.html", community=None, channels=[])
        conn = get_db(current_app.config["DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO communities (name, description) VALUES (?, ?)",
                (name, description),
            )
            conn.commit()
            community_id = conn.execute(
                "SELECT id FROM communities WHERE name = ?", (name,)
            ).fetchone()["id"]
        except Exception as e:
            flash(f"Error creating community: {e}", "error")
            conn.close()
            return render_template("community_edit.html", community=None, channels=[])
        conn.close()
        return redirect(url_for("community.edit", community_id=community_id))
    return render_template("community_edit.html", community=None, channels=[])


@bp.route("/<int:community_id>/edit", methods=["GET", "POST"])
def edit(community_id):
    conn = get_db(current_app.config["DB_PATH"])

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        if name:
            conn.execute(
                "UPDATE communities SET name = ?, description = ? WHERE id = ?",
                (name, description, community_id),
            )
            conn.commit()
            flash("Community updated.", "success")

    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.home"))

    channels = conn.execute("""
        SELECT ch.channel_id, ch.channel_name, ch.handle, ch.thumbnail_url,
               cc.added_at,
               (SELECT subscriber_count FROM channel_snapshots cs
                WHERE cs.channel_id = ch.channel_id
                ORDER BY cs.snapshot_date DESC LIMIT 1) AS subscriber_count
        FROM community_channels cc
        JOIN channels ch ON cc.channel_id = ch.channel_id
        WHERE cc.community_id = ?
        ORDER BY ch.channel_name
    """, (community_id,)).fetchall()

    conn.close()
    return render_template(
        "community_edit.html",
        community=dict(community),
        channels=[dict(c) for c in channels],
    )


@bp.route("/<int:community_id>/add-channel", methods=["POST"])
def add_channel(community_id):
    identifier = request.form.get("identifier", "").strip()
    if not identifier:
        flash("Please enter a channel handle or ID.", "error")
        return redirect(url_for("community.edit", community_id=community_id))

    conn = get_db(current_app.config["DB_PATH"])
    api_key = get_setting(conn, "youtube_api_key")
    if not api_key:
        flash("YouTube API key not configured. Go to Settings first.", "error")
        conn.close()
        return redirect(url_for("community.edit", community_id=community_id))

    try:
        youtube = build_youtube(api_key)
        info = resolve_channel_id(youtube, identifier)
    except Exception as e:
        flash(f"API error: {e}", "error")
        conn.close()
        return redirect(url_for("community.edit", community_id=community_id))

    if not info:
        flash(f"Could not find channel: {identifier}", "error")
        conn.close()
        return redirect(url_for("community.edit", community_id=community_id))

    # Upsert channel
    conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle) VALUES (?, ?, ?)",
        (info["id"], info["title"], info.get("handle", "")),
    )
    # Link to community
    conn.execute(
        "INSERT OR IGNORE INTO community_channels (community_id, channel_id) VALUES (?, ?)",
        (community_id, info["id"]),
    )
    conn.commit()
    conn.close()
    flash(f"Added: {info['title']}", "success")
    return redirect(url_for("community.edit", community_id=community_id))


@bp.route("/<int:community_id>/remove-channel/<channel_id>", methods=["POST"])
def remove_channel(community_id, channel_id):
    conn = get_db(current_app.config["DB_PATH"])
    conn.execute(
        "DELETE FROM community_channels WHERE community_id = ? AND channel_id = ?",
        (community_id, channel_id),
    )
    conn.commit()
    conn.close()
    flash("Channel removed from community.", "success")
    return redirect(url_for("community.edit", community_id=community_id))


@bp.route("/<int:community_id>/delete", methods=["POST"])
def delete(community_id):
    conn = get_db(current_app.config["DB_PATH"])
    conn.execute("DELETE FROM community_channels WHERE community_id = ?", (community_id,))
    conn.execute("DELETE FROM communities WHERE id = ?", (community_id,))
    conn.commit()
    conn.close()
    flash("Community deleted.", "success")
    return redirect(url_for("main.home"))
