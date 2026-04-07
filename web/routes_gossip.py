"""Gossip pipeline routes — trigger, status polling, report viewing."""

import math
import threading

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from markupsafe import Markup
from core.db import get_db, get_all_settings, get_community_channel_ids
from core.gossip_pipeline import run_gossip_pipeline, run_collect_only, run_local_steps, run_force_summarize, run_reanalyze
from core.gossip_report import generate_report_html
from core.executive_summary import (
    generate_executive_summary, generate_top_insights,
    get_cached_report, get_report_by_id,
)

bp = Blueprint("gossip", __name__)

# Anthropic pricing: model-prefix → (input $/MTok, output $/MTok)
_PRICING = {
    "claude-haiku-4-5":  (0.80,  4.00),
    "claude-haiku-3-5":  (0.80,  4.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-6":   (15.00, 75.00),
    "claude-opus-4-5":   (15.00, 75.00),
}
_DEFAULT_BUFFER = 120_000
_SYSTEM_PROMPT_TOKENS = 900   # rough size of extract_gossip_batch.txt
_OUTPUT_TOKENS_PER_VIDEO = 300


def _calc_cost(video_count: int, total_comment_chars: int,
               model: str, buffer_chars: int) -> dict:
    """Return cost estimate dict for a summarize run."""
    if video_count == 0:
        return {"videos": 0, "cost": 0.0, "model": model}
    in_price, out_price = next(
        (v for k, v in _PRICING.items() if model.startswith(k)),
        (3.00, 15.00),  # fallback: Sonnet price
    )
    # Estimate number of LLM batches (each batch ≤ buffer_chars of comment text)
    num_batches = max(1, math.ceil(total_comment_chars / buffer_chars))
    input_tokens  = total_comment_chars // 4 + num_batches * _SYSTEM_PROMPT_TOKENS + video_count * 50
    output_tokens = video_count * _OUTPUT_TOKENS_PER_VIDEO
    cost = (input_tokens / 1_000_000 * in_price) + (output_tokens / 1_000_000 * out_price)
    # Pretty model label: "claude-haiku-4-5" → "Haiku 4.5"
    label = model.replace("claude-", "").replace("-", " ").title()
    return {"videos": video_count, "cost": cost, "model": label}


def _get_cost_estimates(conn, community_id: int) -> dict | None:
    """
    Return cost estimates for incremental and force summarize runs,
    or None if the summarize backend is not anthropic.
    """
    settings = get_all_settings(conn)
    if settings.get("llm_summarize_backend", "ollama") != "anthropic":
        return None

    model = settings.get("llm_summarize_anthropic_model", "claude-haiku-4-5")
    buffer_chars = int(settings.get("llm_summarize_buffer_chars", str(_DEFAULT_BUFFER)))
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        return None

    ph = ",".join("?" * len(channel_ids))

    pending = conn.execute(f"""
        SELECT COUNT(DISTINCT v.video_id) AS cnt,
               COALESCE(SUM(LENGTH(c.text)), 0) AS chars
        FROM videos v
        JOIN comments c ON v.video_id = c.video_id
        LEFT JOIN video_summaries vs ON v.video_id = vs.video_id
        WHERE v.channel_id IN ({ph})
          AND (vs.video_id IS NULL
               OR vs.last_comment_published_at IS NULL
               OR EXISTS (
                   SELECT 1 FROM comments c2
                   WHERE c2.video_id = v.video_id
                     AND c2.published_at > vs.last_comment_published_at))
    """, channel_ids).fetchone()

    total = conn.execute(f"""
        SELECT COUNT(DISTINCT v.video_id) AS cnt,
               COALESCE(SUM(LENGTH(c.text)), 0) AS chars
        FROM videos v
        JOIN comments c ON v.video_id = c.video_id
        WHERE v.channel_id IN ({ph})
    """, channel_ids).fetchone()

    return {
        "incremental": _calc_cost(pending["cnt"], pending["chars"], model, buffer_chars),
        "force":       _calc_cost(total["cnt"],   total["chars"],   model, buffer_chars),
    }


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

    cost_estimates = _get_cost_estimates(conn, community_id)
    conn.close()
    return render_template(
        "gossip_runs.html",
        community=dict(community),
        active_run=dict(active_run) if active_run else None,
        queued_runs=[dict(r) for r in queued_runs],
        history=[dict(r) for r in history],
        cost_estimates=cost_estimates,
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


@bp.route("/<int:community_id>/reanalyze", methods=["POST"])
def start_reanalyze(community_id):
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
        target=run_reanalyze, args=(db_path, community_id, run_id), daemon=True
    )
    t.start()
    flash("Re-analyze started — aggregate + analyze + report (no collect/summarize).", "success")
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


@bp.route("/<int:community_id>/executive-summary")
def executive_summary(community_id):
    conn = get_db(current_app.config["DB_PATH"])
    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.home"))

    regenerate = request.args.get("regenerate") == "1"
    cached = None if regenerate else get_cached_report(
        conn, community_id, "executive_summary"
    )

    if cached:
        report_id = cached["id"]
        html = cached["report_html"]
    else:
        try:
            result = generate_executive_summary(conn, community_id)
            report_id = result["id"]
            html = result["html"]
        except Exception as e:
            conn.close()
            flash(f"Executive summary failed: {e}", "error")
            return redirect(url_for("gossip.runs", community_id=community_id))

    conn.close()
    return render_template(
        "executive_summary.html",
        summary_html=Markup(html),
        community=dict(community),
        report_id=report_id,
        report_type="executive_summary",
    )


@bp.route("/<int:community_id>/top-insights")
def top_insights(community_id):
    conn = get_db(current_app.config["DB_PATH"])
    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.home"))

    min_evidence = request.args.get("min_evidence", 5, type=int)
    regenerate = request.args.get("regenerate") == "1"
    cached = None if regenerate else get_cached_report(
        conn, community_id, "top_insights", min_evidence
    )

    if cached:
        report_id = cached["id"]
        html = cached["report_html"]
    else:
        try:
            result = generate_top_insights(conn, community_id,
                                           min_evidence=min_evidence)
            report_id = result["id"]
            html = result["html"]
        except Exception as e:
            conn.close()
            flash(f"Top insights failed: {e}", "error")
            return redirect(url_for("gossip.runs", community_id=community_id))

    conn.close()
    return render_template(
        "executive_summary.html",
        summary_html=Markup(html),
        community=dict(community),
        report_id=report_id,
        report_type="top_insights",
        min_evidence=min_evidence,
    )


@bp.route("/executive-report/<int:report_id>/pdf")
def executive_report_pdf(report_id):
    """Serve a cached executive report as a standalone HTML page for
    browser print-to-PDF (no app chrome, just the report)."""
    conn = get_db(current_app.config["DB_PATH"])
    report = get_report_by_id(conn, report_id)
    conn.close()
    if not report:
        flash("Report not found.", "error")
        return redirect(url_for("main.home"))
    return report["report_html"]
