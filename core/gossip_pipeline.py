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
    current_channel: list[str | None] = [None]

    def progress(msg: str):
        parts = msg.split("\t")
        kind = parts[0] if parts else ""
        if kind == "channel":
            current_channel[0] = parts[1] if len(parts) >= 2 else None
            detail = f"{parts[1]} — {parts[2]}" if len(parts) >= 3 else msg
        elif kind == "video":
            # format: video\thandle\tpos\ttitle\t...
            ch = parts[1] if len(parts) >= 2 else (current_channel[0] or "")
            pos = parts[2] if len(parts) >= 3 else ""
            title = parts[3][:40] if len(parts) >= 4 else ""
            detail = f"{ch} · {pos}: {title}" if ch else f"{pos}: {title}"
        elif kind == "done":
            detail = f"✓ {parts[1]}: {parts[2]}" if len(parts) >= 3 else msg
        elif kind == "quota_update":
            try:
                used = int(parts[1])
            except (IndexError, ValueError):
                used = 0
            conn.execute(
                "UPDATE gossip_runs SET quota_units = ? WHERE id = ?",
                (used, run_id),
            )
            conn.commit()
            return
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


def _wait_for_active_run(conn, community_id: int, own_run_id: int,
                         timeout_s: int = 7200) -> bool:
    """
    Block until no other run is active for this community (or timeout).
    Returns True if clear to proceed, False if timed out.
    """
    import time as _time
    waited = 0
    while waited < timeout_s:
        other = conn.execute(
            "SELECT id FROM gossip_runs "
            "WHERE community_id = ? AND id != ? AND status NOT IN ('complete','failed')",
            (community_id, own_run_id),
        ).fetchone()
        if not other:
            return True
        _time.sleep(5)
        waited += 5
        # re-open connection check (WAL mode keeps it fresh)
        conn.execute("SELECT 1")  # keep alive
    return False


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

        # If another run (e.g. collect-only) is already active, wait for it
        other = conn.execute(
            "SELECT id, status FROM gossip_runs "
            "WHERE community_id = ? AND id != ? AND status NOT IN ('complete','failed')",
            (community_id, run_id),
        ).fetchone()
        if other:
            _update_run(conn, run_id, "pending",
                        f"Waiting for run #{other['id']} to finish...")
            progress(f"info\tWaiting for run #{other['id']} ({other['status']}) to complete...")
            if not _wait_for_active_run(conn, community_id, run_id):
                _fail_run(conn, run_id, "Timed out waiting for previous run to finish.")
                return
            progress(f"info\tPrevious run finished — starting local steps")

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


def run_force_summarize(db_path: str, community_id: int, run_id: int):
    """
    Re-summarize ALL videos (force=True), then aggregate.
    Skips collection. Stops before analyze (respects backend settings).
    Used to reprocess existing summaries with a different LLM backend.
    """
    conn = get_db(db_path)
    try:
        settings = get_all_settings(conn)
        analyze_backend = settings.get("llm_analyze_backend", "anthropic")
        progress = _make_progress(conn, run_id)

        _update_run(conn, run_id, "summarizing", "Force re-summarizing all videos...")
        log.info(f"[Run {run_id}] Force summarize — Step 1: Summarizing (force=True)")
        progress("info\tForce mode: reprocessing all videos regardless of prior summaries")
        gossip_summarize.summarize_community(conn, community_id,
                                             force=True, progress_callback=progress)

        _update_run(conn, run_id, "aggregating", "Computing metrics...")
        log.info(f"[Run {run_id}] Force summarize — Step 2: Aggregating")
        agg_id = gossip_aggregate.aggregate_community(conn, community_id,
                                                      progress_callback=progress)

        if analyze_backend != "ollama":
            progress(f"info\tStopped before analyze — backend is '{analyze_backend}' (not local)")
            conn.execute(
                "UPDATE gossip_runs SET status='complete', current_step='complete', "
                "completed_at=datetime('now') WHERE id=?", (run_id,)
            )
            conn.commit()
            return

        _update_run(conn, run_id, "analyzing", "Running local LLM analysis...")
        log.info(f"[Run {run_id}] Force summarize — Step 3: Analyzing (ollama)")
        analysis_id = gossip_analyze.analyze_aggregation(conn, agg_id,
                                                         progress_callback=progress)

        _update_run(conn, run_id, "reporting", "Generating report...")
        gossip_report.generate_report_html(conn, analysis_id)
        _complete_run(conn, run_id, analysis_id)
        log.info(f"[Run {run_id}] Force summarize complete. Analysis ID: {analysis_id}")

    except Exception as e:
        log.error(f"[Run {run_id}] Force summarize failed: {e}", exc_info=True)
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
        # Save analysis_id now so it survives even if report generation fails
        conn.execute("UPDATE gossip_runs SET analysis_id = ? WHERE id = ?", (analysis_id, run_id))
        conn.commit()
        log.info(f"[Run {run_id}] Step 5: Generating report")
        gossip_report.generate_report_html(conn, analysis_id)

        _complete_run(conn, run_id, analysis_id)
        log.info(f"[Run {run_id}] Pipeline complete. Analysis ID: {analysis_id}")

    except Exception as e:
        log.error(f"[Run {run_id}] Pipeline failed: {e}", exc_info=True)
        _fail_run(conn, run_id, str(e))
    finally:
        conn.close()
