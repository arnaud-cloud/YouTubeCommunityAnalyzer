"""
Gossip pipeline orchestrator — runs all 5 steps sequentially for a community.

Updates the gossip_runs table at each step so the GUI can poll for progress.
"""

from __future__ import annotations

import logging

from .db import get_db
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


def run_gossip_pipeline(db_path: str, community_id: int, run_id: int):
    """
    Execute the full 5-step gossip pipeline.

    This function is designed to run in a background thread.
    It opens its own DB connection (SQLite connections aren't thread-safe).
    """
    conn = get_db(db_path)
    try:
        def progress(detail):
            conn.execute(
                "UPDATE gossip_runs SET progress_detail = ? WHERE id = ?",
                (detail, run_id),
            )
            conn.commit()

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
