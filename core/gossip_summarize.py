"""
Gossip summarizer — Step 2: LLM per-video gossip extraction.

Reads raw comments from SQLite, sends them to the configured LLM,
stores structured results. Incremental: already-summarised videos skipped.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .db import get_all_settings, get_community_channel_ids
from .entity_resolver import EntityResolver
from .llm_client import LLMClient, _settings_to_llm_config

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extract_gossip.txt"


def _load_comments_for_video(conn, video_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT comment_id, author_name, text, like_count,
                  published_at, is_reply, parent_id
           FROM comments
           WHERE video_id = ?
           ORDER BY like_count DESC, published_at DESC""",
        (video_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _format_comments_for_llm(comments: list[dict],
                              max_chars: int = 80_000) -> str:
    lines = []
    total = 0
    for c in comments:
        prefix = "  REPLY> " if c["is_reply"] else "COMMENT> "
        line = (
            f"{prefix}[id={c['comment_id']} likes={c['like_count']}] "
            f"{c['author_name']}: {c['text']}"
        )
        if total + len(line) > max_chars:
            lines.append(f"... (truncated, {len(comments) - len(lines)} more)")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _verify_evidence(conn, summary: dict, video_id: str) -> dict:
    valid_ids = {
        row[0] for row in conn.execute(
            "SELECT comment_id FROM comments WHERE video_id = ?", (video_id,)
        )
    }
    verified_items = []
    dropped = 0
    for item in summary.get("gossip_items", []):
        evidence = item.get("evidence_comment_ids", [])
        if not evidence:
            dropped += 1
            continue
        verified = [eid for eid in evidence if eid in valid_ids]
        if not verified:
            dropped += 1
            continue
        item["evidence_comment_ids"] = verified
        verified_items.append(item)
    if dropped:
        log.info(f"  Verification: dropped {dropped} unverifiable gossip items")
    summary["gossip_items"] = verified_items
    return summary


def _save_summary(conn, video_id: str, channel_id: str,
                  summary: dict, backend: str):
    gossip_items = summary.get("gossip_items", [])
    entity_mentions = summary.get("entities_mentioned", [])

    conn.execute(
        """INSERT OR REPLACE INTO video_summaries
               (video_id, channel_id, summary_json, comment_count,
                gossip_count, processed_at, llm_backend)
           VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
        (video_id, channel_id, json.dumps(summary),
         summary.get("_comment_count", 0), len(gossip_items), backend),
    )

    conn.execute("DELETE FROM gossip_items WHERE video_id = ?", (video_id,))
    for item in gossip_items:
        conn.execute(
            """INSERT INTO gossip_items
                   (video_id, channel_id, gossip_type, subjects, claim,
                    evidence_comment_ids, confidence, external_refs,
                    comment_likes_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (video_id, channel_id,
             item.get("gossip_type", ""),
             json.dumps(item.get("subjects", [])),
             item.get("claim", ""),
             json.dumps(item.get("evidence_comment_ids", [])),
             item.get("confidence", "low"),
             json.dumps(item.get("external_refs", [])),
             item.get("comment_likes_total", 0)),
        )

    conn.execute("DELETE FROM entity_mentions WHERE video_id = ?", (video_id,))
    sentiment_map = summary.get("sentiment_map", {})
    for entity in entity_mentions:
        conn.execute(
            """INSERT INTO entity_mentions
                   (entity_name, canonical_name, video_id, channel_id,
                    mention_count, sentiment_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity, entity, video_id, channel_id, 1,
             sentiment_map.get(entity, 0.0)),
        )
    conn.commit()


def _get_pending_videos(conn, channel_ids: list[str],
                        force: bool = False) -> list[dict]:
    placeholders = ",".join("?" * len(channel_ids))
    if force:
        q = f"""SELECT v.video_id, v.channel_id, v.title, v.published_at
                FROM videos v WHERE v.channel_id IN ({placeholders})
                ORDER BY v.published_at DESC"""
        return [dict(r) for r in conn.execute(q, channel_ids).fetchall()]
    else:
        q = f"""SELECT v.video_id, v.channel_id, v.title, v.published_at
                FROM videos v
                LEFT JOIN video_summaries vs ON v.video_id = vs.video_id
                WHERE v.channel_id IN ({placeholders}) AND vs.video_id IS NULL
                ORDER BY v.published_at DESC"""
        return [dict(r) for r in conn.execute(q, channel_ids).fetchall()]


def summarize_community(conn, community_id: int,
                        force: bool = False,
                        progress_callback=None) -> int:
    """
    Summarize all unsummarized videos for a community.
    Returns the number of videos processed.
    """
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="summarize")

    alias_json = settings.get("entity_aliases", "{}")
    try:
        alias_map = json.loads(alias_json)
    except json.JSONDecodeError:
        alias_map = {}
    resolver = EntityResolver(alias_map)

    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        return 0

    videos = _get_pending_videos(conn, channel_ids, force)
    log.info(f"Found {len(videos)} videos to summarize")

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    processed = 0

    for i, v in enumerate(videos, 1):
        video_id = v["video_id"]
        channel_id = v["channel_id"]
        title = v.get("title", "")

        if progress_callback:
            progress_callback(f"Summarizing video {i}/{len(videos)}: {title[:50]}")

        comments = _load_comments_for_video(conn, video_id)
        if not comments:
            log.info(f"  [{i}] No comments for {title[:50]}, skipping")
            continue

        log.info(f"  [{i}/{len(videos)}] Summarizing: {title[:70]}")
        comment_block = _format_comments_for_llm(comments)
        user_prompt = (
            f"VIDEO ID: {video_id}\n"
            f"CHANNEL: {channel_id}\n"
            f"TITLE: {title}\n\n"
            f"COMMENTS ({len(comments)} total):\n\n"
            f"{comment_block}\n\n"
            "IMPORTANT: Respond with valid JSON only. "
            "Start your response with {{ and end with }}. "
            "No prose, no explanation, no markdown."
        )

        try:
            summary = llm.complete_json(
                system_prompt, user_prompt,
                max_tokens=llm.max_tokens_summarize,
            )
        except Exception as e:
            log.error(f"  LLM call failed for {video_id}: {e}")
            continue

        if "entities_mentioned" in summary:
            summary["entities_mentioned"] = resolver.resolve_list(
                summary["entities_mentioned"]
            )
        for item in summary.get("gossip_items", []):
            if "subjects" in item:
                item["subjects"] = resolver.resolve_list(item["subjects"])

        summary = _verify_evidence(conn, summary, video_id)
        summary["_comment_count"] = len(comments)
        _save_summary(conn, video_id, channel_id, summary, llm.backend)

        n = len(summary.get("gossip_items", []))
        log.info(f"    -> {n} gossip items saved")
        processed += 1

    return processed
