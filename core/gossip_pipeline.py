"""
Gossip pipeline orchestrator — runs all 5 steps sequentially for a community.

Updates the gossip_runs table at each step so the GUI can poll for progress.
"""

from __future__ import annotations

import logging

from .db import get_db, get_all_settings
from . import gossip_collect, gossip_summarize, gossip_aggregate, gossip_analyze, gossip_report

log = logging.getLogger(__name__)


def _update_run(conn, run_id: int, status: str, detail: str = ""):
    conn.execute(
        "UPDATE gossip_runs SET status = ?, current_step = ?, progress_detail = ? WHERE id = ?",
        (status, status, detail, run_id),
    )
    conn.commit()


def _complete_run(conn, run_id: int, analysis_id: int):
    conn.execute(
        "UPDATE gossip_runs SET status = 'complete', analysis_id = ?, "
        "completed_at = datetime('now') WHERE id = ?",
        (analysis_id, run_id),
    )
    conn.commit()


def _fail_run(conn, run_id: int, error: str):
    conn.execute(
        "UPDATE gossip_runs SET status = 'failed', error_message = ?, "
        "completed_at = datetime('now') WHERE id = ?",
        (error, run_id),
    )
    conn.commit()


def _make_progress(conn, run_id: int):
    """
    Return a progress callback that:
    - updates progress_detail with a short human-readable summary
    - appends the raw tab-delimited message to progress_log
    """
    def progress(msg: str):
        parts = msg.split("\t")
        kind = parts[0] if parts else ""
        if kind == "channel":
            detail = f"{parts[1]} — {parts[2]}" if len(parts) >= 3 else msg
        elif kind == "video":
            detail = f"Video {parts[1]}: {parts[2][:50]}" if len(parts) >= 3 else msg
        elif kind == "done":
            detail = f"✓ {parts[1]}: {parts[2]}" if len(parts) >= 3 else msg
        elif kind == "quota":
            detail = parts[1] if len(parts) >= 2 else msg
        elif kind == "info":
            detail = parts[1] if len(parts) >= 2 else msg
        else:
            detail = msg
        conn.execute(
            "UPDATE gossip_runs SET progress_detail = ?, "
            "progress_log = progress_log || ? || char(10) WHERE id = ?",
            (detail, msg, run_id),
        )
        conn.commit()
    return progress


def run_collect_only(db_path: str, community_id: int, run_id: int):
    """Run Step 1 only (comment collection). No LLM calls."""
    conn = get_db(db_path)
    try:
        _update_run(conn, run_id, "collecting", "Fetching YouTube comments...")
        log.info(f"[Run {run_id}] Collect-only: fetching comments")
        gossip_collect.collect_community(conn, community_id,
                                         progress_callback=_make_progress(conn, run_id))
        conn.execute(
            "UPDATE gossip_runs SET status = 'complete', current_step = 'complete', "
            "completed_at = datetime('now') WHERE id = ?",
            (run_id,),
        )
        conn.commit()
        log.info(f"[Run {run_id}] Comment collection complete.")
    except Exception as e:
        log.error(f"[Run {run_id}] Collection failed: {e}", exc_info=True)
        _fail_run(conn, run_id, str(e))
    finally:
        conn.close()


def run_local_steps(db_path: str, community_id: int, run_id: int):
    """
    Run pipeline steps that use only local (Ollama) LLM backends.
    Stops before any step configured to use a cloud (non-ollama) backend.

    Typical daily use: collect + ollama summarize + aggregate.
    Weekly full analysis still uses run_gossip_pipeline() with Claude.
    """
    conn = get_db(db_path)
    try:
        settings = get_all_settings(conn)
        summarize_backend = settings.get("llm_summarize_backend", "ollama")
        analyze_backend   = settings.get("llm_analyze_backend", "anthropic")
        progress = _make_progress(conn, run_id)

        # Step 1: Collect (always local)
        _update_run(conn, run_id, "collecting", "Fetching YouTube comments...")
        log.info(f"[Run {run_id}] Local steps — Step 1: Collecting")
        gossip_collect.collect_community(conn, community_id, progress_callback=progress)

        # Step 2: Summarize — only if local
        if summarize_backend != "ollama":
            log.info(f"[Run {run_id}] Stopping before summarize (backend={summarize_backend})")
            progress(f"info\tStopped before summarize — backend is '{summarize_backend}' (not local)")
            conn.execute(
                "UPDATE gossip_runs SET status='complete', current_step='complete', "
                "completed_at=datetime('now') WHERE id=?", (run_id,)
            )
            conn.commit()
            return

        _update_run(conn, run_id, "summarizing", "Extracting gossip with local LLM...")
        log.info(f"[Run {run_id}] Local steps — Step 2: Summarizing (ollama)")
        gossip_summarize.summarize_community(conn, community_id, progress_callback=progress)

        # Step 3: Aggregate (always local)
        _update_run(conn, run_id, "aggregating", "Computing metrics...")
        log.info(f"[Run {run_id}] Local steps — Step 3: Aggregating")
        agg_id = gossip_aggregate.aggregate_community(conn, community_id, progress_callback=progress)

        # Step 4: Analyze — only if local
        if analyze_backend != "ollama":
            log.info(f"[Run {run_id}] Stopping before analyze (backend={analyze_backend})")
            progress(f"info\tStopped before analyze — backend is '{analyze_backend}' (not local)")
            conn.execute(
                "UPDATE gossip_runs SET status='complete', current_step='complete', "
                "completed_at=datetime('now') WHERE id=?", (run_id,)
            )
            conn.commit()
            return

        _update_run(conn, run_id, "analyzing", "Running local LLM analysis...")
        log.info(f"[Run {run_id}] Local steps — Step 4: Analyzing (ollama)")
        analysis_id = gossip_analyze.analyze_aggregation(conn, agg_id, progress_callback=progress)

        # Step 5: Report
        _update_run(conn, run_id, "reporting", "Generating report...")
        log.info(f"[Run {run_id}] Local steps — Step 5: Generating report")
        gossip_report.generate_report_html(conn, analysis_id)
        _complete_run(conn, run_id, analysis_id)
        log.info(f"[Run {run_id}] Local pipeline complete. Analysis ID: {analysis_id}")

    except Exception as e:
        log.error(f"[Run {run_id}] Local pipeline failed: {e}", exc_info=True)
        _fail_run(conn, run_id, str(e))
    finally:
        conn.close()


def run_gossip_pipeline(db_path: str, community_id: int, run_id: int):
    """
    Execute the full 5-step gossip pipeline.

    This function is designed to run in a background thread.
    It opens its own DB connection (SQLite connections aren't thread-safe).
    """
    conn = get_db(db_path)
    try:
        progress = _make_progress(conn, run_id)

        # Step 1: Collect comments
        _update_run(conn, run_id, "collecting", "Fetching YouTube comments...")
        log.info(f"[Run {run_id}] Step 1: Collecting comments")
        gossip_collect.collect_community(conn, community_id, progress_callback=progress)

        # Step 2: Summarize (LLM)
        _update_run(conn, run_id, "summarizing", "Extracting gossip with LLM...")
        log.info(f"[Run {run_id}] Step 2: Summarizing")
        gossip_summarize.summarize_community(conn, community_id, progress_callback=progress)

        # Step 3: Aggregate (pure computation)
        _update_run(conn, run_id, "aggregating", "Computing metrics...")
        log.info(f"[Run {run_id}] Step 3: Aggregating")
        agg_id = gossip_aggregate.aggregate_community(conn, community_id, progress_callback=progress)

        # Step 4: Analyze (LLM)
        _update_run(conn, run_id, "analyzing", "Running LLM narrative synthesis...")
        log.info(f"[Run {run_id}] Step 4: Analyzing")
        analysis_id = gossip_analyze.analyze_aggregation(conn, agg_id, progress_callback=progress)

        # Step 5: Generate report
        _update_run(conn, run_id, "reporting", "Generating report...")
        log.info(f"[Run {run_id}] Step 5: Generating report")
        gossip_report.generate_report_html(conn, analysis_id)

        _complete_run(conn, run_id, analysis_id)
        log.info(f"[Run {run_id}] Pipeline complete. Analysis ID: {analysis_id}")

    except Exception as e:
        log.error(f"[Run {run_id}] Pipeline failed: {e}", exc_info=True)
        _fail_run(conn, run_id, str(e))
    finally:
        conn.close()
