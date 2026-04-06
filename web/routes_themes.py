"""Gossip Theme Tracker routes — browse and detail pages."""

from __future__ import annotations

import json
import threading
from datetime import datetime

from flask import (Blueprint, current_app, flash, jsonify,
                   redirect, render_template, request, url_for)

from core.db import get_db
from core.gossip_themes import (
    compute_themes, get_theme_detail, sparkline_svg,
    activity_chart_svg, _GOSSIP_TYPE_COLORS, DEFAULT_TYPE_COLOR,
)


def _safe_json(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default

bp = Blueprint("themes", __name__)

# In-memory job tracker: community_id -> {status, message, started_at}
_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()

GOSSIP_TYPES = ["drama", "relationship", "collaboration",
                "reputation", "irl_vs_persona", "trend"]


def _run_bg(db_path: str, community_id: int, use_llm: bool):
    with _jobs_lock:
        _jobs[community_id] = {
            "status": "running",
            "message": "Computing themes...",
            "started_at": datetime.now().isoformat(),
        }
    try:
        conn = get_db(db_path)

        def _cb(msg):
            with _jobs_lock:
                _jobs[community_id]["message"] = msg

        n = compute_themes(conn, community_id, use_llm=use_llm,
                           progress_callback=_cb)
        conn.close()
        with _jobs_lock:
            _jobs[community_id] = {
                "status": "complete",
                "message": f"{n} theme{'s' if n != 1 else ''} computed",
            }
    except Exception as e:
        with _jobs_lock:
            _jobs[community_id] = {"status": "failed", "message": str(e)}


def _is_running(community_id: int) -> bool:
    with _jobs_lock:
        return _jobs.get(community_id, {}).get("status") == "running"


@bp.route("/<int:community_id>")
def browse(community_id):
    conn = get_db(current_app.config["DB_PATH"])

    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.home"))

    # Themes sorted by most recently active
    theme_rows = conn.execute(
        "SELECT * FROM themes WHERE community_id = ? "
        "ORDER BY CASE WHEN last_seen_at IS NULL THEN 1 ELSE 0 END, "
        "last_seen_at DESC, total_evidence DESC",
        (community_id,),
    ).fetchall()
    conn.close()

    # Build theme dicts with pre-computed sparklines
    themes = []
    for row in theme_rows:
        t = dict(row)
        t["subjects"] = _safe_json(t["subjects"], [])
        t["activity"] = _safe_json(t["activity_json"], {})
        t["sparkline"] = sparkline_svg(t["activity"])
        t["type_color"] = _GOSSIP_TYPE_COLORS.get(
            t.get("gossip_type") or "", DEFAULT_TYPE_COLOR
        )
        themes.append(t)

    # Active filter from query params
    filter_type = request.args.get("type", "")
    filter_subject = request.args.get("subject", "").strip().lower()

    with _jobs_lock:
        job = dict(_jobs.get(community_id, {}))

    return render_template(
        "themes_browse.html",
        community=dict(community),
        themes=themes,
        gossip_types=GOSSIP_TYPES,
        filter_type=filter_type,
        filter_subject=filter_subject,
        job=job,
    )


@bp.route("/<int:community_id>/recompute", methods=["POST"])
def recompute(community_id):
    if _is_running(community_id):
        flash("Theme computation is already running.", "error")
        return redirect(url_for("themes.browse", community_id=community_id))

    use_llm = request.form.get("use_llm") == "1"
    db_path = current_app.config["DB_PATH"]
    t = threading.Thread(
        target=_run_bg, args=(db_path, community_id, use_llm), daemon=True
    )
    t.start()
    flash("Theme computation started.", "success")
    return redirect(url_for("themes.browse", community_id=community_id))


@bp.route("/<int:community_id>/recompute/status")
def recompute_status(community_id):
    with _jobs_lock:
        job = dict(_jobs.get(community_id, {"status": "idle", "message": ""}))
    return jsonify(job)


@bp.route("/detail/<int:theme_id>")
def detail(theme_id):
    conn = get_db(current_app.config["DB_PATH"])
    theme = get_theme_detail(conn, theme_id)
    if not theme:
        conn.close()
        flash("Theme not found.", "error")
        return redirect(url_for("main.home"))

    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (theme["community_id"],)
    ).fetchone()
    conn.close()

    chart_svg = activity_chart_svg(theme["activity"], "Activity Over Time")

    return render_template(
        "theme_detail.html",
        theme=theme,
        community=dict(community) if community else {},
        chart_svg=chart_svg,
        gossip_types=GOSSIP_TYPES,
        type_color=theme["type_color"],
    )
