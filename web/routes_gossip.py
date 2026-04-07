"""Gossip pipeline routes — trigger, status polling, report viewing."""

import logging
import math
import threading

log = logging.getLogger(__name__)

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from markupsafe import Markup
from core.db import get_db, get_all_settings, get_community_channel_ids
from core.gossip_pipeline import run_gossip_pipeline, run_collect_only, run_local_steps, run_force_summarize, run_reanalyze, run_resummarize_all
from core.gossip_report import generate_report_html
from core.executive_summary import (
    generate_executive_summary, generate_top_insights,
    get_cached_report, get_report_by_id,
)

bp = Blueprint("gossip", __name__)

# In-memory tone scoring job state keyed by community_id
_tone_jobs: dict[int, dict] = {}

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
# Downstream analysis chain: analyze + themes(LLM) + 2 exec reports ≈ 4 LLM calls
# each with the full aggregation payload; outputs are JSON-heavy
_ANALYZE_PROMPT_TOKENS = 2_500   # per call: system prompt overhead
_ANALYZE_CALLS = 4               # analyze + themes + exec_summary + top_insights
_ANALYZE_OUTPUT_TOKENS = 12_000  # total output across all 4 calls


def _model_label(model: str) -> str:
    return model.replace("claude-", "").replace("-", " ").title()


def _model_prices(model: str) -> tuple[float, float]:
    return next(
        (v for k, v in _PRICING.items() if model.startswith(k)),
        (3.00, 15.00),
    )


def _calc_summarize_cost(video_count: int, total_comment_chars: int,
                         model: str, buffer_chars: int) -> dict:
    """Return cost estimate dict for a summarize run."""
    if video_count == 0:
        return {"videos": 0, "cost": 0.0, "model": _model_label(model)}
    in_price, out_price = _model_prices(model)
    num_batches   = max(1, math.ceil(total_comment_chars / buffer_chars))
    input_tokens  = total_comment_chars // 4 + num_batches * _SYSTEM_PROMPT_TOKENS + video_count * 50
    output_tokens = video_count * _OUTPUT_TOKENS_PER_VIDEO
    cost = (input_tokens / 1_000_000 * in_price) + (output_tokens / 1_000_000 * out_price)
    return {"videos": video_count, "cost": cost, "model": _model_label(model)}


def _calc_analyze_cost(conn, community_id: int, model: str) -> dict:
    """
    Estimate cost for the full downstream analysis chain:
    analyze + themes (LLM titles) + exec_summary + top_insights.
    Uses the last stored aggregation's JSON sizes as a proxy for input volume.
    """
    in_price, out_price = _model_prices(model)
    row = conn.execute(
        """SELECT COALESCE(LENGTH(entity_metrics_json), 0)
                + COALESCE(LENGTH(gossip_corpus_json), 0)
                + COALESCE(LENGTH(corroborated_json), 0)
                + COALESCE(LENGTH(asymmetries_json), 0)
                + COALESCE(LENGTH(comment_velocity_json), 0) AS total_chars
           FROM aggregation_results WHERE community_id = ?
           ORDER BY id DESC LIMIT 1""",
        (community_id,),
    ).fetchone()
    data_chars = row[0] if row else 40_000  # rough default if no prior run
    # Each call receives the aggregation payload; _ANALYZE_CALLS calls total
    input_tokens  = (data_chars // 4) * _ANALYZE_CALLS + _ANALYZE_PROMPT_TOKENS * _ANALYZE_CALLS
    output_tokens = _ANALYZE_OUTPUT_TOKENS
    cost = (input_tokens / 1_000_000 * in_price) + (output_tokens / 1_000_000 * out_price)
    return {"cost": cost, "model": _model_label(model)}


def _get_cost_estimates(conn, community_id: int) -> dict:
    """
    Return cost estimates for each LLM role that uses Anthropic.
    Keys: summarize (with incremental/force sub-keys), analyze.
    Always returns a dict; missing keys mean that role uses a local backend.
    """
    settings     = get_all_settings(conn)
    channel_ids  = get_community_channel_ids(conn, community_id)
    result: dict = {}

    # Summarize cost (per-video, scales with corpus size)
    if settings.get("llm_summarize_backend", "ollama") == "anthropic" and channel_ids:
        summ_model   = settings.get("llm_summarize_anthropic_model", "claude-haiku-4-5")
        buffer_chars = int(settings.get("llm_summarize_buffer_chars", str(_DEFAULT_BUFFER)))
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

        result["summarize"] = {
            "incremental": _calc_summarize_cost(pending["cnt"], pending["chars"], summ_model, buffer_chars),
            "force":       _calc_summarize_cost(total["cnt"],   total["chars"],   summ_model, buffer_chars),
        }

    # Analyze cost (analyze + themes + exec reports — all use role="analyze")
    if settings.get("llm_analyze_backend", "anthropic") == "anthropic":
        analyze_model = settings.get("llm_analyze_anthropic_model", "claude-sonnet-4-6")
        result["analyze"] = _calc_analyze_cost(conn, community_id, analyze_model)

    return result


def _get_pipeline_status(conn, community_id: int) -> dict:
    """Compute staleness state for each pipeline step."""
    channel_ids = get_community_channel_ids(conn, community_id)
    never = {"state": "never", "timestamp": None, "detail": ""}
    if not channel_ids:
        return {k: never for k in
                ("collect", "summarize", "aggregate", "analyze", "report", "themes", "exec_reports")}

    ph = ",".join("?" * len(channel_ids))

    def q1(sql, params=()):
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None

    last_collect   = q1(f"SELECT MAX(collected_at) FROM comments WHERE channel_id IN ({ph})", channel_ids)
    last_summarize = q1(f"SELECT MAX(processed_at) FROM video_summaries WHERE channel_id IN ({ph})", channel_ids)
    last_aggregate = q1("SELECT MAX(created_at) FROM aggregation_results WHERE community_id = ?", (community_id,))
    last_analyze   = q1("SELECT MAX(created_at) FROM analysis_results WHERE community_id = ?", (community_id,))
    last_themes    = q1("SELECT MAX(created_at) FROM themes WHERE community_id = ?", (community_id,))
    last_exec      = q1("SELECT MAX(created_at) FROM executive_reports WHERE community_id = ?", (community_id,))

    pending_summaries = q1(f"""
        SELECT COUNT(DISTINCT v.video_id)
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
    """, channel_ids) or 0

    def _state(ts, stale=False):
        if not ts:
            return "never"
        return "stale" if stale else "fresh"

    def _gt(a, b):
        """True if both non-null and a > b."""
        return bool(a and b and a > b)

    summarize_stale  = pending_summaries > 0
    aggregate_stale  = _gt(last_summarize, last_aggregate)
    analyze_stale    = _gt(last_aggregate, last_analyze)
    themes_stale     = _gt(last_summarize, last_themes)
    exec_stale       = (_gt(last_themes, last_exec) or _gt(last_analyze, last_exec)
                        or _gt(last_aggregate, last_exec))

    return {
        "collect":      {"state": _state(last_collect),                          "timestamp": last_collect,   "detail": ""},
        "summarize":    {"state": _state(last_summarize, summarize_stale),        "timestamp": last_summarize, "detail": f"{pending_summaries} pending" if pending_summaries else ""},
        "aggregate":    {"state": _state(last_aggregate, aggregate_stale),        "timestamp": last_aggregate, "detail": ""},
        "analyze":      {"state": _state(last_analyze,   analyze_stale),          "timestamp": last_analyze,   "detail": ""},
        "report":       {"state": _state(last_analyze,   analyze_stale),          "timestamp": last_analyze,   "detail": ""},
        "themes":       {"state": _state(last_themes,    themes_stale),           "timestamp": last_themes,    "detail": ""},
        "exec_reports": {"state": _state(last_exec,      exec_stale),             "timestamp": last_exec,      "detail": ""},
    }


def _get_preset_availability(conn, community_id: int, cost_estimates) -> dict:
    """Return enabled/disabled state and cost hints for each preset card."""
    channel_ids = get_community_channel_ids(conn, community_id)
    has_channels = len(channel_ids) > 0

    has_summaries = False
    if has_channels:
        ph = ",".join("?" * len(channel_ids))
        has_summaries = conn.execute(
            f"SELECT COUNT(*) FROM video_summaries WHERE channel_id IN ({ph})",
            channel_ids,
        ).fetchone()[0] > 0

    summ  = cost_estimates.get("summarize") or {}
    anlyz = cost_estimates.get("analyze")

    return {
        "collect": {
            "enabled": has_channels,
            "reason": None if has_channels else "No channels in community",
            "summarize_cost": None,
            "analyze_cost": None,
        },
        "full_pipeline": {
            "enabled": has_channels,
            "reason": None if has_channels else "No channels in community",
            "summarize_cost": summ.get("incremental"),
            "analyze_cost": anlyz,
        },
        "reanalyze": {
            "enabled": has_summaries,
            "reason": None if has_summaries else "No summaries yet — run Full Pipeline first",
            "summarize_cost": None,
            "analyze_cost": anlyz,
        },
        "resummarize_all": {
            "enabled": has_channels,
            "reason": None if has_channels else "No channels in community",
            "summarize_cost": summ.get("force"),
            "analyze_cost": anlyz,
        },
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

    cost_estimates  = _get_cost_estimates(conn, community_id)
    pipeline_status = _get_pipeline_status(conn, community_id)
    presets         = _get_preset_availability(conn, community_id, cost_estimates)
    conn.close()
    return render_template(
        "gossip_runs.html",
        community=dict(community),
        active_run=dict(active_run) if active_run else None,
        queued_runs=[dict(r) for r in queued_runs],
        history=[dict(r) for r in history],
        cost_estimates=cost_estimates,
        pipeline_status=pipeline_status,
        presets=presets,
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


@bp.route("/<int:community_id>/resummarize-all", methods=["POST"])
def start_resummarize_all(community_id):
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
        target=run_resummarize_all, args=(db_path, community_id, run_id), daemon=True
    )
    t.start()
    flash("Re-summarize All started — all videos will be reprocessed from scratch.", "success")
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

    min_evidence = request.args.get("min_evidence", 10, type=int)
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


@bp.route("/<int:community_id>/commenters")
def commenters(community_id):
    """Commenter credibility scores panel."""
    from core.commenter_scoring import score_community
    conn = get_db(current_app.config["DB_PATH"])
    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.home"))

    page = request.args.get("page", 1, type=int)
    per_page = 50
    tier_filter = request.args.get("tier", "")
    sort_by = request.args.get("sort", "quality_score")
    creators_only = request.args.get("creators_only", "") == "1"
    if sort_by not in {"quality_score", "channel_count", "comment_count", "total_likes", "reply_ratio"}:
        sort_by = "quality_score"

    # Fetch channel owner IDs for this community
    channel_owner_ids = [
        r["source_id"] for r in conn.execute(
            "SELECT source_id FROM community_sources WHERE community_id = ?",
            (community_id,),
        ).fetchall()
    ]

    where_clauses = ["community_id = ?"]
    params: list = [community_id]
    if tier_filter in ("A", "B", "C", "D"):
        where_clauses.append("tier = ?")
        params.append(tier_filter)
    if creators_only and channel_owner_ids:
        where_clauses.append(
            "author_channel_id IN (%s)" % ",".join("?" * len(channel_owner_ids))
        )
        params.extend(channel_owner_ids)
    where = " AND ".join(where_clauses)

    total = conn.execute(
        f"SELECT COUNT(*) FROM commenter_scores WHERE {where}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"SELECT * FROM commenter_scores WHERE {where} "
        f"ORDER BY {sort_by} DESC "
        f"LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()

    tier_counts = {}
    for r in conn.execute(
        "SELECT tier, COUNT(*) AS cnt FROM commenter_scores "
        "WHERE community_id = ? GROUP BY tier",
        (community_id,),
    ).fetchall():
        tier_counts[r["tier"]] = r["cnt"]

    computed_at = rows[0]["computed_at"] if rows else None
    settings = get_all_settings(conn)
    has_ollama = settings.get("llm_summarize_backend", "anthropic") == "ollama"
    conn.close()

    tone_job = _tone_jobs.get(community_id, {"status": "idle"})

    return render_template(
        "gossip_commenters.html",
        community=dict(community),
        commenters=[dict(r) for r in rows],
        tier_counts=tier_counts,
        channel_owner_ids=set(channel_owner_ids),
        creators_only=creators_only,
        has_ollama=has_ollama,
        tone_job=tone_job,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, math.ceil(total / per_page)),
        sort_by=sort_by,
        tier_filter=tier_filter,
        computed_at=computed_at,
    )


@bp.route("/<int:community_id>/score-commenters", methods=["POST"])
def score_commenters_now(community_id):
    """Manually trigger commenter credibility re-scoring."""
    from core.commenter_scoring import score_community
    conn = get_db(current_app.config["DB_PATH"])
    try:
        n = score_community(conn, community_id)
        flash(f"Scored {n} commenters successfully.", "success")
    except Exception as e:
        flash(f"Scoring failed: {e}", "error")
    finally:
        conn.close()
    return redirect(url_for("gossip.commenters", community_id=community_id))


@bp.route("/<int:community_id>/tone-score-commenters", methods=["POST"])
def tone_score_commenters(community_id):
    """Run Ollama LLM tone scoring pass on all scored commenters."""
    import time
    from core.commenter_scoring import score_community, score_community_tone
    db_path = current_app.config["DB_PATH"]

    # Don't start a second job if one is already running
    existing_job = _tone_jobs.get(community_id, {})
    if existing_job.get("status") == "running":
        flash("Tone scoring is already running.", "warning")
        return redirect(url_for("gossip.commenters", community_id=community_id))

    _tone_jobs[community_id] = {
        "status": "running", "done": 0, "total": 0,
        "started_at": time.time(), "error": None,
    }

    def _run():
        conn = get_db(db_path)
        try:
            existing = conn.execute(
                "SELECT COUNT(*) FROM commenter_scores WHERE community_id = ?",
                (community_id,),
            ).fetchone()[0]
            if existing == 0:
                score_community(conn, community_id)

            total = conn.execute(
                "SELECT COUNT(*) FROM commenter_scores WHERE community_id = ?",
                (community_id,),
            ).fetchone()[0]
            _tone_jobs[community_id]["total"] = total

            def _progress(done, total_count):
                _tone_jobs[community_id]["done"] = done
                _tone_jobs[community_id]["total"] = total_count

            score_community_tone(conn, community_id, progress_callback=_progress)
            _tone_jobs[community_id]["status"] = "done"
            _tone_jobs[community_id]["done"] = total
        except Exception as e:
            log.error(f"Tone scoring failed for community {community_id}: {e}", exc_info=True)
            _tone_jobs[community_id]["status"] = "error"
            _tone_jobs[community_id]["error"] = str(e)
        finally:
            conn.close()

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("gossip.commenters", community_id=community_id))


@bp.route("/<int:community_id>/tone-score-status")
def tone_score_status(community_id):
    """JSON status for the in-progress tone scoring job."""
    import time
    job = _tone_jobs.get(community_id, {"status": "idle"})
    result = dict(job)
    if job.get("status") == "running":
        elapsed = time.time() - (job.get("started_at") or time.time())
        done = job.get("done", 0)
        total = job.get("total", 0)
        if done > 0 and elapsed > 0:
            rate = done / elapsed  # commenters per second
            remaining = (total - done) / rate if rate > 0 else None
            result["eta_seconds"] = round(remaining) if remaining is not None else None
        else:
            result["eta_seconds"] = None
    return jsonify(result)


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
