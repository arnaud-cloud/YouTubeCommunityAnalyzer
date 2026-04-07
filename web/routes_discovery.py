"""
Community discovery routes — keyword search across platforms + LLM suggestions.
"""

from __future__ import annotations

import json
import logging

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, current_app, jsonify,
)
from core.db import get_db, get_all_settings, get_setting

log = logging.getLogger(__name__)

bp = Blueprint("discovery", __name__)


@bp.route("/")
def discovery_page():
    conn = get_db(current_app.config["DB_PATH"])
    communities = [dict(r) for r in conn.execute(
        "SELECT id, name FROM communities ORDER BY name"
    ).fetchall()]
    conn.close()
    community_id = request.args.get("community_id", "")
    return render_template(
        "discovery.html",
        communities=communities,
        prefill_community_id=community_id,
    )


@bp.route("/search", methods=["POST"])
def search():
    """Run discovery search. Returns JSON list of DiscoveryResult dicts."""
    keywords = request.json.get("keywords", "").strip() if request.is_json else \
               request.form.get("keywords", "").strip()
    if not keywords:
        return jsonify({"error": "keywords required"}), 400

    conn = get_db(current_app.config["DB_PATH"])
    settings = get_all_settings(conn)
    conn.close()

    try:
        from core.community_discovery import discover_sources
        results = discover_sources(keywords, settings)
        return jsonify({"results": [r.__dict__ for r in results]})
    except Exception as e:
        log.error(f"Discovery search failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@bp.route("/create", methods=["POST"])
def create_community():
    """Create a new community from selected discovery results."""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    sources = data.get("sources", [])  # list of {source_type, source_id, display_name}

    if not name:
        return jsonify({"error": "community name required"}), 400

    conn = get_db(current_app.config["DB_PATH"])
    try:
        conn.execute(
            "INSERT INTO communities (name) VALUES (?)", (name,)
        )
        conn.commit()
        community_id = conn.execute(
            "SELECT id FROM communities WHERE name = ?", (name,)
        ).fetchone()["id"]
        _add_sources_to_community(conn, community_id, sources)
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

    conn.close()
    return jsonify({
        "ok": True,
        "community_id": community_id,
        "redirect": url_for("community.edit", community_id=community_id),
    })


@bp.route("/add/<int:community_id>", methods=["POST"])
def add_to_community(community_id):
    """Add selected discovery results to an existing community."""
    data = request.get_json() or {}
    sources = data.get("sources", [])

    conn = get_db(current_app.config["DB_PATH"])
    community = conn.execute(
        "SELECT id, name FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        return jsonify({"error": "community not found"}), 404

    try:
        _add_sources_to_community(conn, community_id, sources)
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

    conn.close()
    return jsonify({
        "ok": True,
        "redirect": url_for("community.edit", community_id=community_id),
    })


def _add_sources_to_community(conn, community_id: int, sources: list[dict]):
    """Insert sources into community_sources (and community_channels for YouTube)."""
    for s in sources:
        source_type = s.get("source_type", "youtube")
        source_id = s.get("source_id", "")
        display_name = s.get("display_name", source_id)
        if not source_id:
            continue

        # Ensure channels row exists
        conn.execute(
            "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle) "
            "VALUES (?, ?, ?)",
            (source_id, display_name, source_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO community_sources "
            "(community_id, source_type, source_id, display_name) VALUES (?, ?, ?, ?)",
            (community_id, source_type, source_id, display_name),
        )
        if source_type == "youtube":
            conn.execute(
                "INSERT OR IGNORE INTO community_channels (community_id, channel_id) "
                "VALUES (?, ?)",
                (community_id, source_id),
            )
