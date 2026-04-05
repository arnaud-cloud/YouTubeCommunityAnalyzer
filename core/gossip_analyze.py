"""
Gossip analyzer — Step 4: LLM narrative synthesis.

Takes pre-computed metrics from the aggregation step and calls the LLM
to produce a structured narrative analysis.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .db import get_all_settings
from .llm_client import LLMClient, _settings_to_llm_config

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "synthesize_report.txt"


def _safe_json_loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _load_aggregation(conn, agg_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM aggregation_results WHERE id = ?", (agg_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Aggregation {agg_id} not found.")
    d = dict(row)
    d["entity_metrics"] = _safe_json_loads(d.pop("entity_metrics_json"), {})
    d["gossip_corpus"] = _safe_json_loads(d.pop("gossip_corpus_json"), [])
    d["corroborated"] = _safe_json_loads(d.pop("corroborated_json"), [])
    d["asymmetries"] = _safe_json_loads(d.pop("asymmetries_json"), [])
    d["velocity"] = _safe_json_loads(d.pop("comment_velocity_json"), {})
    d["top_commenters"] = _safe_json_loads(d.pop("top_commenters_json"), [])
    d["channels"] = _safe_json_loads(d.get("channels_included", "[]"), [])
    return d


def _build_prompt_payload(agg: dict) -> str:
    entity_metrics = agg["entity_metrics"]
    gossip_corpus = agg["gossip_corpus"]
    corroborated = agg["corroborated"]

    top_entities = sorted(
        entity_metrics.values(),
        key=lambda x: -x.get("total_mentions", 0),
    )[:30]

    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    top_gossip = sorted(
        gossip_corpus,
        key=lambda x: (
            confidence_rank.get(x.get("confidence", "low"), 0),
            x.get("comment_likes_total", 0),
        ),
        reverse=True,
    )[:80]

    payload = {
        "channels_analysed": agg["channels"],
        "date_range": {
            "start": agg.get("date_range_start", ""),
            "end": agg.get("date_range_end", ""),
        },
        "total_videos": agg.get("total_videos", 0),
        "total_gossip_items": agg.get("total_gossip_items", 0),
        "entity_metrics": top_entities,
        "corroborated_claims": corroborated[:20],
        "gossip_corpus": top_gossip,
        "cross_mention_asymmetries": agg["asymmetries"][:15],
        "top_cross_channel_commenters": agg["top_commenters"][:15],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def analyze_aggregation(conn, agg_id: int,
                        progress_callback=None) -> int:
    """
    Run LLM narrative synthesis on an aggregation.
    Returns the analysis_results.id.
    """
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="analyze")

    if progress_callback:
        progress_callback("Loading aggregation results...")

    agg = _load_aggregation(conn, agg_id)
    log.info(
        f"Aggregation id={agg['id']} | "
        f"{agg.get('total_videos', 0)} videos | "
        f"{agg.get('total_gossip_items', 0)} gossip items"
    )

    if progress_callback:
        progress_callback("Running LLM narrative synthesis...")

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_prompt = _build_prompt_payload(agg)

    try:
        analysis = llm.complete_json(
            system_prompt, user_prompt,
            max_tokens=llm.max_tokens_analyze,
        )
    except Exception as e:
        log.error(f"LLM synthesis failed: {e}", exc_info=True)
        raise

    # Attach pre-computed metrics so the report has everything
    analysis["entity_metrics"] = agg["entity_metrics"]
    analysis["corroborated_claims"] = agg["corroborated"]
    analysis["cross_mention_asymmetries"] = agg["asymmetries"]
    analysis["comment_velocity"] = agg["velocity"]
    analysis["top_commenters"] = agg["top_commenters"]

    community_id = agg.get("community_id")
    cur = conn.execute(
        """INSERT INTO analysis_results
               (aggregation_id, community_id, analysis_json, channels_included,
                date_range_start, date_range_end, created_at, llm_backend)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
        (
            agg_id, community_id,
            json.dumps(analysis),
            json.dumps(agg["channels"]),
            agg.get("date_range_start", ""),
            agg.get("date_range_end", ""),
            llm.backend,
        ),
    )
    conn.commit()
    analysis_id = cur.lastrowid
    log.info(f"Analysis saved (id={analysis_id})")
    return analysis_id
