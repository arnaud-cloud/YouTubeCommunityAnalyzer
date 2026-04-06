"""Gossip pipeline routes — trigger, status polling, report viewing."""

import threading

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from markupsafe import Markup
from core.db import get_db
from core.gossip_pipeline import run_gossip_pipeline, run_collect_only, run_local_steps, run_force_summarize
from core.gossip_report import generate_report_html

bp = Blueprint("gossip", __name__)


@bp.route("/<int:community_id>")
def runs(community_id):
    conn = get_db(current_app.config["DB_PATH"])

    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.home"))

    # Currently running (non-pending) run
    active_run = conn.execute("""
        SELECT * FROM gossip_runs
        WHERE community_id = ? AND status NOT IN ('complete', 'failed', 'pending')
        ORDER BY id ASC LIMIT 1
    """, (community_id,)).fetchone()

    # Queued (pending) runs
    queued_runs = conn.execute("""
        SELECT * FROM gossip_runs
        WHERE community_id = ? AND status = 'pending'
        ORDER BY id ASC
    """, (community_id,)).fetchall()

    # Run history
    history = conn.execute("""
        SELECT gr.*, ar.llm_backend
        FROM gossip_runs gr
        LEFT JOIN analysis_results ar ON gr.analysis_id = ar.id
        WHERE gr.community_id = ?
        ORDER BY gr.id DESC
        LIMIT 20
    """, (community_id,)).fetchall()

    conn.close()
    return render_template(
        "gossip_runs.html",
        community=dict(community),
        active_run=dict(active_run) if active_run else None,
        queued_runs=[dict(r) for r in queued_runs],
        history=[dict(r) for r in history],
    )


@bp.route("/<int:community_id>/run", methods=["POST"])
def start_run(community_id):
    conn = get_db(current_app.config["DB_PATH"])

    # Check no run already in progress
    active = conn.execute("""
        SELECT id FROM gossip_runs
        WHERE community_id = ? AND status NOT IN ('complete', 'failed')
    """, (community_id,)).fetchone()
    if active:
        conn.close()
        flash("A gossip pipeline is already running for this community.", "error")
        return redirect(url_for("gossip.runs", community_id=community_id))

    # Create run record
    cur = conn.execute(
        "INSERT INTO gossip_runs (community_id, status) VALUES (?, 'pending')",
        (community_id,),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()

    db_path = current_app.config["DB_PATH"]
    t = threading.Thread(
        target=run_gossip_pipeline,
        args=(db_path, community_id, run_id),
        daemon=True,
    )
    t.start()

    flash("Gossip pipeline started.", "success")
    return redirect(url_for("gossip.runs", community_id=community_id))


@bp.route("/<int:community_id>/run-local", methods=["POST"])
def start_local(community_id):
    conn = get_db(current_app.config["DB_PATH"])

    # Allow queuing ONE run behind a collect-only run; block everything else
    active_runs = conn.execute(
        "SELECT id, current_step, status FROM gossip_runs "
        "WHERE community_id = ? AND status NOT IN ('complete','failed') "
        "ORDER BY id ASC",
        (community_id,),
    ).fetchall()
    collecting_only = (
        len(active_runs) == 1
        and active_runs[0]["current_step"] in ("", "pending", "collecting")
    )
    if active_runs and not collecting_only:
        conn.close()
        flash("A pipeline is already running or queued for this community.", "error")
        return redirect(url_for("gossip.runs", community_id=community_id))
    active = active_runs[0] if active_runs else None

    cur = conn.execute(
        "INSERT INTO gossip_runs (community_id, status) VALUES (?, 'pending')",
        (community_id,),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()

    db_path = current_app.config["DB_PATH"]
    t = threading.Thread(
        target=run_local_steps, args=(db_path, community_id, run_id), daemon=True
    )
    t.start()
    if active:
        flash("Local pipeline queued — will start after current collection finishes.", "success")
    else:
        flash("Local pipeline started.", "success")
    return redirect(url_for("gossip.runs", community_id=community_id))


@bp.route("/<int:community_id>/collect", methods=["POST"])
def start_collect(community_id):
    conn = get_db(current_app.config["DB_PATH"])
    active = conn.execute(
        "SELECT id FROM gossip_runs WHERE community_id = ? AND status NOT IN ('complete','failed')",
        (community_id,),
    ).fetchone()
    if active:
        conn.close()
        flash("A pipeline is already running for this community.", "error")
        return redirect(url_for("gossip.runs", community_id=community_id))

    cur = conn.execute(
        "INSERT INTO gossip_runs (community_id, status) VALUES (?, 'pending')",
        (community_id,),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()

    db_path = current_app.config["DB_PATH"]
    t = threading.Thread(
        target=run_collect_only, args=(db_path, community_id, run_id), daemon=True
    )
    t.start()
    flash("Comment collection started.", "success")
    return redirect(url_for("gossip.runs", community_id=community_id))


@bp.route("/<int:community_id>/summarize-force", methods=["POST"])
def start_force_summarize(community_id):
    conn = get_db(current_app.config["DB_PATH"])
    active = conn.execute(
        "SELECT id FROM gossip_runs WHERE community_id = ? AND status NOT IN ('complete','failed')",
        (community_id,),
    ).fetchone()
    if active:
        conn.close()
        flash("A pipeline is already running for this community.", "error")
        return redirect(url_for("gossip.runs", community_id=community_id))

    cur = conn.execute(
        "INSERT INTO gossip_runs (community_id, status) VALUES (?, 'pending')",
        (community_id,),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()

    db_path = current_app.config["DB_PATH"]
    t = threading.Thread(
        target=run_force_summarize, args=(db_path, community_id, run_id), daemon=True
    )
    t.start()
    flash("Force re-summarize started — all videos will be reprocessed.", "success")
    return redirect(url_for("gossip.runs", community_id=community_id))


@bp.route("/run/<int:run_id>/status")
def run_status(run_id):
    conn = get_db(current_app.config["DB_PATH"])
    row = conn.execute(
        "SELECT id, status, current_step, progress_detail, progress_log, "
        "quota_units, started_at, completed_at, analysis_id, error_message "
        "FROM gossip_runs WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(dict(row))


@bp.route("/run/<int:run_id>/retry-report", methods=["POST"])
def retry_report(run_id):
    conn = get_db(current_app.config["DB_PATH"])
    run = conn.execute(
        "SELECT * FROM gossip_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if not run:
        conn.close()
        flash("Run not found.", "error")
        return redirect(url_for("main.home"))

    analysis_id = run["analysis_id"]
    if not analysis_id:
        # Find the most recent analysis for this community
        row = conn.execute(
            "SELECT id FROM analysis_results WHERE community_id = ? ORDER BY id DESC LIMIT 1",
            (run["community_id"],),
        ).fetchone()
        if not row:
            conn.close()
            flash("No analysis found for this community.", "error")
            return redirect(url_for("gossip.runs", community_id=run["community_id"]))
        analysis_id = row["id"]
        conn.execute(
            "UPDATE gossip_runs SET analysis_id = ? WHERE id = ?", (analysis_id, run_id)
        )
        conn.commit()

    conn.close()
    return redirect(url_for("gossip.report", analysis_id=analysis_id))


@bp.route("/report/<int:analysis_id>")
def report(analysis_id):
    conn = get_db(current_app.config["DB_PATH"])

    # Get community_id for back-navigation
    row = conn.execute(
        "SELECT community_id FROM analysis_results WHERE id = ?", (analysis_id,)
    ).fetchone()
    community_id = row["community_id"] if row else None

    try:
        html = generate_report_html(conn, analysis_id)
    except ValueError as e:
        conn.close()
        flash(str(e), "error")
        return redirect(url_for("main.home"))

    conn.close()
    return render_template(
        "gossip_report.html",
        report_html=Markup(html),
        analysis_id=analysis_id,
        community_id=community_id,
    )
