"""Community CRUD routes — create, edit, delete communities + manage sources."""

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, current_app, jsonify,
)
from core.db import get_db, get_setting, get_community_sources

bp = Blueprint("community", __name__)


@bp.route("/new", methods=["GET", "POST"])
def new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        if not name:
            flash("Community name is required.", "error")
            return render_template("community_edit.html", community=None, sources=[])
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
            return render_template("community_edit.html", community=None, sources=[])
        conn.close()
        return redirect(url_for("community.edit", community_id=community_id))
    return render_template("community_edit.html", community=None, sources=[])


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

    # Fetch sources with enriched metadata
    sources = _get_sources_with_meta(conn, community_id)

    conn.close()
    return render_template(
        "community_edit.html",
        community=dict(community),
        sources=sources,
    )


def _get_sources_with_meta(conn, community_id: int) -> list[dict]:
    """Return sources for a community enriched with display metadata."""
    rows = conn.execute("""
        SELECT cs.source_type, cs.source_id, cs.display_name, cs.added_at,
               ch.channel_name, ch.handle, ch.thumbnail_url,
               (SELECT subscriber_count FROM channel_snapshots s
                WHERE s.channel_id = cs.source_id
                ORDER BY s.snapshot_date DESC LIMIT 1) AS subscriber_count
        FROM community_sources cs
        LEFT JOIN channels ch ON cs.source_id = ch.channel_id
        WHERE cs.community_id = ?
        ORDER BY cs.source_type, COALESCE(ch.channel_name, cs.display_name, cs.source_id)
    """, (community_id,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["name"] = d["channel_name"] or d["display_name"] or d["source_id"]
        result.append(d)
    return result


@bp.route("/<int:community_id>/add-source", methods=["POST"])
def add_source(community_id):
    source_type = request.form.get("source_type", "youtube").strip()
    identifier = request.form.get("identifier", "").strip()
    if not identifier:
        flash("Please enter a channel handle, ID, or subreddit name.", "error")
        return redirect(url_for("community.edit", community_id=community_id))

    conn = get_db(current_app.config["DB_PATH"])

    if source_type == "youtube":
        _add_youtube_source(conn, community_id, identifier)
    elif source_type == "reddit":
        _add_reddit_source(conn, community_id, identifier)
    else:
        flash(f"Unknown source type: {source_type}", "error")

    conn.close()
    return redirect(url_for("community.edit", community_id=community_id))


def _add_youtube_source(conn, community_id: int, identifier: str):
    from core.youtube_api import build_youtube, resolve_channel_id
    api_key = get_setting(conn, "youtube_api_key")
    if not api_key:
        flash("YouTube API key not configured. Go to Settings first.", "error")
        return

    try:
        youtube = build_youtube(api_key)
        info = resolve_channel_id(youtube, identifier)
    except Exception as e:
        flash(f"YouTube API error: {e}", "error")
        return

    if not info:
        flash(f"Could not find YouTube channel: {identifier}", "error")
        return

    channel_id = info["id"]
    channel_name = info["title"]

    conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle) VALUES (?, ?, ?)",
        (channel_id, channel_name, info.get("handle", "")),
    )
    # Write both legacy table (for backward compat) and new table
    conn.execute(
        "INSERT OR IGNORE INTO community_channels (community_id, channel_id) VALUES (?, ?)",
        (community_id, channel_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO community_sources "
        "(community_id, source_type, source_id, display_name) VALUES (?, ?, ?, ?)",
        (community_id, "youtube", channel_id, channel_name),
    )
    conn.commit()
    flash(f"Added YouTube channel: {channel_name}", "success")


def _add_reddit_source(conn, community_id: int, identifier: str):
    from core.db import get_setting, get_all_settings
    settings = {
        "reddit_client_id": get_setting(conn, "reddit_client_id"),
        "reddit_client_secret": get_setting(conn, "reddit_client_secret"),
        "reddit_user_agent": get_setting(conn, "reddit_user_agent",
                                         "CommunityAnalyzer/1.0"),
    }
    if not settings["reddit_client_id"] or not settings["reddit_client_secret"]:
        flash("Reddit API credentials not configured. Go to Settings first.", "error")
        return

    # Normalise: strip 'r/' prefix for lookup, re-add for storage
    clean = identifier.lstrip("r/").strip()
    source_id = f"r/{clean}"

    try:
        from core.reddit_api import build_reddit, fetch_subreddit_info
        reddit = build_reddit(
            settings["reddit_client_id"],
            settings["reddit_client_secret"],
            settings["reddit_user_agent"],
        )
        info = fetch_subreddit_info(reddit, clean)
    except ImportError:
        flash("praw is not installed. Run: pip install praw", "error")
        return
    except Exception as e:
        flash(f"Reddit API error: {e}", "error")
        return

    if not info:
        flash(f"Could not find subreddit: {source_id}", "error")
        return

    display_name = info["channel_name"]
    conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle, description) "
        "VALUES (?, ?, ?, ?)",
        (source_id, display_name, source_id, info.get("description", "")),
    )
    conn.execute(
        "INSERT OR IGNORE INTO community_sources "
        "(community_id, source_type, source_id, display_name) VALUES (?, ?, ?, ?)",
        (community_id, "reddit", source_id, display_name),
    )
    conn.commit()
    flash(f"Added Reddit subreddit: {display_name}", "success")


@bp.route("/<int:community_id>/remove-source", methods=["POST"])
def remove_source(community_id):
    source_type = request.form.get("source_type", "youtube")
    source_id = request.form.get("source_id", "")
    conn = get_db(current_app.config["DB_PATH"])
    conn.execute(
        "DELETE FROM community_sources "
        "WHERE community_id = ? AND source_type = ? AND source_id = ?",
        (community_id, source_type, source_id),
    )
    # Also remove from legacy table if YouTube
    if source_type == "youtube":
        conn.execute(
            "DELETE FROM community_channels WHERE community_id = ? AND channel_id = ?",
            (community_id, source_id),
        )
    conn.commit()
    conn.close()
    flash("Source removed from community.", "success")
    return redirect(url_for("community.edit", community_id=community_id))


# Keep old add-channel route for bookmarks / external scripts
@bp.route("/<int:community_id>/add-channel", methods=["POST"])
def add_channel(community_id):
    return add_source(community_id)


# Keep old remove-channel route for bookmarks
@bp.route("/<int:community_id>/remove-channel/<channel_id>", methods=["POST"])
def remove_channel(community_id, channel_id):
    conn = get_db(current_app.config["DB_PATH"])
    conn.execute(
        "DELETE FROM community_sources "
        "WHERE community_id = ? AND source_type = 'youtube' AND source_id = ?",
        (community_id, channel_id),
    )
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
    conn.execute("DELETE FROM community_sources WHERE community_id = ?", (community_id,))
    conn.execute("DELETE FROM community_channels WHERE community_id = ?", (community_id,))
    conn.execute("DELETE FROM communities WHERE id = ?", (community_id,))
    conn.commit()
    conn.close()
    flash("Community deleted.", "success")
    return redirect(url_for("main.home"))


@bp.route("/manage")
def manage():
    """Matrix view: all sources × all communities."""
    conn = get_db(current_app.config["DB_PATH"])

    communities = [dict(r) for r in conn.execute(
        "SELECT id, name FROM communities ORDER BY name"
    ).fetchall()]

    # Show all known channels/subreddits with their metadata
    channels = [dict(r) for r in conn.execute("""
        SELECT ch.channel_id, ch.channel_name, ch.handle, ch.thumbnail_url,
               COALESCE(
                   (SELECT source_type FROM community_sources cs
                    WHERE cs.source_id = ch.channel_id LIMIT 1),
                   'youtube'
               ) AS source_type,
               (SELECT subscriber_count FROM channel_snapshots cs
                WHERE cs.channel_id = ch.channel_id
                ORDER BY cs.snapshot_date DESC LIMIT 1) AS subscriber_count
        FROM channels ch
        ORDER BY ch.channel_name
    """).fetchall()]

    # Build set of (community_id, source_id) memberships from community_sources
    memberships = set()
    for row in conn.execute(
        "SELECT community_id, source_id FROM community_sources"
    ):
        memberships.add((row["community_id"], row["source_id"]))
    # Also include legacy community_channels not yet in community_sources
    for row in conn.execute(
        "SELECT community_id, channel_id FROM community_channels"
    ):
        memberships.add((row["community_id"], row["channel_id"]))

    conn.close()
    return render_template(
        "community_manage.html",
        communities=communities,
        channels=channels,
        memberships=memberships,
    )


@bp.route("/assign", methods=["POST"])
def assign():
    """AJAX: add a source to a community."""
    data = request.get_json()
    community_id = data.get("community_id")
    channel_id = data.get("channel_id")
    # Determine source_type from channel_id prefix
    source_type = "reddit" if str(channel_id).startswith("r/") else "youtube"
    conn = get_db(current_app.config["DB_PATH"])
    display_name = (
        conn.execute(
            "SELECT channel_name FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone() or {"channel_name": channel_id}
    )["channel_name"]
    conn.execute(
        "INSERT OR IGNORE INTO community_sources "
        "(community_id, source_type, source_id, display_name) VALUES (?, ?, ?, ?)",
        (community_id, source_type, channel_id, display_name),
    )
    if source_type == "youtube":
        conn.execute(
            "INSERT OR IGNORE INTO community_channels (community_id, channel_id) VALUES (?, ?)",
            (community_id, channel_id),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@bp.route("/unassign", methods=["POST"])
def unassign():
    """AJAX: remove a source from a community."""
    data = request.get_json()
    community_id = data.get("community_id")
    channel_id = data.get("channel_id")
    source_type = "reddit" if str(channel_id).startswith("r/") else "youtube"
    conn = get_db(current_app.config["DB_PATH"])
    conn.execute(
        "DELETE FROM community_sources "
        "WHERE community_id = ? AND source_type = ? AND source_id = ?",
        (community_id, source_type, channel_id),
    )
    if source_type == "youtube":
        conn.execute(
            "DELETE FROM community_channels WHERE community_id = ? AND channel_id = ?",
            (community_id, channel_id),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})
