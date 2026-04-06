"""
Gossip summarizer — Step 2: LLM batch gossip extraction.

Groups pending videos into buffer-sized batches (reducing LLM calls and
avoiding per-video truncation), sends each batch in a single LLM call, and
tracks last_comment_published_at for incremental processing — only videos
with new comments since the last run are re-processed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .db import get_all_settings, get_community_channel_ids
from .entity_resolver import EntityResolver
from .llm_client import LLMClient, _settings_to_llm_config

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extract_gossip_batch.txt"
DEFAULT_BUFFER_CHARS = 120_000


# ── Querying pending videos ────────────────────────────────────────────────────

def _get_pending_videos(conn, channel_ids: list[str],
                        force: bool = False) -> list[dict]:
    """
    Return videos that need (re)summarizing.

    force=False (incremental):
      - never summarized yet
      - summarized but last_comment_published_at is not tracked (NULL)
      - new comments arrived after last_comment_published_at

    force=True: all videos that have at least one comment.
    """
    placeholders = ",".join("?" * len(channel_ids))
    if force:
        q = f"""
            SELECT v.video_id, v.channel_id, v.title, v.published_at
            FROM videos v
            WHERE v.channel_id IN ({placeholders})
              AND EXISTS (SELECT 1 FROM comments c WHERE c.video_id = v.video_id)
            ORDER BY v.published_at DESC
        """
        return [dict(r) for r in conn.execute(q, channel_ids).fetchall()]

    q = f"""
        SELECT v.video_id, v.channel_id, v.title, v.published_at
        FROM videos v
        LEFT JOIN video_summaries vs ON v.video_id = vs.video_id
        WHERE v.channel_id IN ({placeholders})
          AND EXISTS (SELECT 1 FROM comments c WHERE c.video_id = v.video_id)
          AND (
            vs.video_id IS NULL
            OR vs.last_comment_published_at IS NULL
            OR EXISTS (
                SELECT 1 FROM comments c
                WHERE c.video_id = v.video_id
                  AND c.published_at > vs.last_comment_published_at
            )
          )
        ORDER BY v.published_at DESC
    """
    return [dict(r) for r in conn.execute(q, channel_ids).fetchall()]


# ── Comment loading ────────────────────────────────────────────────────────────

def _load_comments(conn, video_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT comment_id, author_name, text, like_count,
                  published_at, is_reply, parent_id
           FROM comments
           WHERE video_id = ?
           ORDER BY like_count DESC, published_at DESC""",
        (video_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Batch construction ─────────────────────────────────────────────────────────

def _format_video_block(video_id: str, channel_id: str,
                        title: str, comments: list[dict]) -> str:
    """Format one video as a labelled block for inclusion in a batch prompt."""
    lines = [
        f"=== VIDEO: {video_id} ===",
        f"CHANNEL: {channel_id}",
        f"TITLE: {title}",
        f"COMMENTS ({len(comments)} total):",
        "",
    ]
    for c in comments:
        prefix = "  REPLY> " if c["is_reply"] else "COMMENT> "
        lines.append(
            f"{prefix}[id={c['comment_id']} likes={c['like_count']}] "
            f"{c['author_name']}: {c['text']}"
        )
    lines.append("")
    return "\n".join(lines)


# VideoItem = (video_id, channel_id, title, comments, max_published_at, block_text)
_VI_ID    = 0
_VI_CID   = 1
_VI_TITLE = 2
_VI_COMMS = 3
_VI_MAXDT = 4
_VI_BLOCK = 5


def _build_batches(video_items: list[tuple], buffer_chars: int) -> list[list[tuple]]:
    """
    Group video items into batches each fitting within buffer_chars.
    A single video that exceeds the buffer is still sent alone.
    """
    batches: list[list[tuple]] = []
    current: list[tuple] = []
    current_size = 0

    for item in video_items:
        block_size = len(item[_VI_BLOCK])
        if current and current_size + block_size > buffer_chars:
            batches.append(current)
            current = [item]
            current_size = block_size
        else:
            current.append(item)
            current_size += block_size

    if current:
        batches.append(current)
    return batches


# ── Evidence verification ──────────────────────────────────────────────────────

def _verify_evidence(conn, summaries: list[dict]) -> list[dict]:
    """Drop gossip items whose evidence comment IDs don't exist in the DB."""
    verified = []
    for s in summaries:
        video_id = s.get("video_id", "")
        valid_ids = {
            row[0] for row in conn.execute(
                "SELECT comment_id FROM comments WHERE video_id = ?", (video_id,)
            )
        }
        ok_items, dropped = [], 0
        for item in s.get("gossip_items", []):
            evidence = item.get("evidence_comment_ids", [])
            if not evidence:
                dropped += 1
                continue
            good = [eid for eid in evidence if eid in valid_ids]
            if not good:
                dropped += 1
                continue
            item["evidence_comment_ids"] = good
            ok_items.append(item)
        if dropped:
            log.info(f"  [{video_id}] Dropped {dropped} unverifiable gossip items")
        s["gossip_items"] = ok_items
        verified.append(s)
    return verified


# ── Persistence ────────────────────────────────────────────────────────────────

def _save_result(conn, summary: dict, backend: str,
                 max_published_at: str | None, comment_count: int) -> None:
    """Persist one video's LLM result and update last_comment_published_at."""
    video_id  = summary["video_id"]
    channel_id = summary.get("channel_id", "")
    gossip_items    = summary.get("gossip_items", [])
    entity_mentions = summary.get("entities_mentioned", [])

    conn.execute(
        """INSERT OR REPLACE INTO video_summaries
               (video_id, channel_id, summary_json, comment_count,
                gossip_count, processed_at, llm_backend,
                last_comment_published_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
        (video_id, channel_id, json.dumps(summary),
         comment_count, len(gossip_items), backend, max_published_at),
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


# ── Main entry point ───────────────────────────────────────────────────────────

def summarize_community(conn, community_id: int,
                        force: bool = False,
                        progress_callback=None) -> int:
    """
    Summarize all pending videos for a community using buffer-based LLM batching.

    Videos are grouped into char-limited batches so that:
    - Videos with few comments share a single LLM call (cheaper)
    - No video is silently truncated (each batch stays within the configured limit)
    - Only videos with new comments since the last run are included (incremental)

    Returns the number of videos successfully processed.
    """
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="summarize")
    buffer_chars = int(settings.get("llm_summarize_buffer_chars", DEFAULT_BUFFER_CHARS))

    alias_json = settings.get("entity_aliases", "{}")
    try:
        alias_map = json.loads(alias_json)
    except json.JSONDecodeError:
        alias_map = {}
    resolver = EntityResolver(alias_map)

    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        return 0

    pending = _get_pending_videos(conn, channel_ids, force)
    log.info(f"Found {len(pending)} pending videos to summarize")
    if not pending:
        return 0

    # Build video items: load comments, compute max published_at, format block
    video_items: list[tuple] = []
    for v in pending:
        comments = _load_comments(conn, v["video_id"])
        if not comments:
            continue
        dates = [c["published_at"] for c in comments if c.get("published_at")]
        max_pub = max(dates) if dates else None
        block = _format_video_block(
            v["video_id"], v["channel_id"], v.get("title", ""), comments
        )
        video_items.append((
            v["video_id"], v["channel_id"], v.get("title", ""),
            comments, max_pub, block,
        ))

    if not video_items:
        return 0

    batches = _build_batches(video_items, buffer_chars)
    log.info(f"  -> {len(video_items)} videos in {len(batches)} batches "
             f"(buffer={buffer_chars:,} chars)")

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    processed = 0

    for batch_idx, batch in enumerate(batches, 1):
        titles_preview = ", ".join(item[_VI_TITLE][:30] for item in batch[:3])
        if len(batch) > 3:
            titles_preview += f" +{len(batch) - 3} more"

        if progress_callback:
            progress_callback(
                f"Batch {batch_idx}/{len(batches)}: {len(batch)} videos — {titles_preview}"
            )

        log.info(f"  Batch {batch_idx}/{len(batches)}: {len(batch)} videos — {titles_preview}")

        user_prompt = (
            f"Process {len(batch)} video(s) below.\n\n"
            + "".join(item[_VI_BLOCK] for item in batch)
            + "IMPORTANT: Respond with valid JSON only. "
              "Start your response with { and end with }. "
              "No prose, no explanation, no markdown."
        )

        try:
            result = llm.complete_json(
                system_prompt, user_prompt,
                max_tokens=llm.max_tokens_summarize,
            )
        except Exception as e:
            log.error(f"  Batch {batch_idx} LLM call failed: {e}")
            continue

        # Normalise output: accept {"videos": [...]} or bare list or single dict
        if isinstance(result, list):
            raw = result
        else:
            raw = result.get("videos", [])
            if not isinstance(raw, list):
                raw = [raw] if isinstance(raw, dict) else []

        # Index by video_id; warn about unexpected IDs
        expected_ids = {item[_VI_ID] for item in batch}
        summary_by_id: dict[str, dict] = {}
        for s in raw:
            vid = s.get("video_id")
            if vid and vid in expected_ids:
                summary_by_id[vid] = s
            elif vid:
                log.warning(f"  LLM returned unexpected video_id={vid!r} in batch {batch_idx}")

        # Verify evidence and save
        to_verify = [summary_by_id[item[_VI_ID]] for item in batch
                     if item[_VI_ID] in summary_by_id]
        verified_list = _verify_evidence(conn, to_verify)
        verified_by_id = {s["video_id"]: s for s in verified_list}

        for item in batch:
            video_id = item[_VI_ID]
            summary  = verified_by_id.get(video_id)
            if not summary:
                log.warning(f"  No LLM result for {video_id} ({item[_VI_TITLE][:50]})")
                continue

            # Resolve entity aliases
            if "entities_mentioned" in summary:
                summary["entities_mentioned"] = resolver.resolve_list(
                    summary["entities_mentioned"]
                )
            for gi in summary.get("gossip_items", []):
                if "subjects" in gi:
                    gi["subjects"] = resolver.resolve_list(gi["subjects"])

            _save_result(conn, summary, llm.backend, item[_VI_MAXDT], len(item[_VI_COMMS]))
            n = len(summary.get("gossip_items", []))
            log.info(f"    [{video_id}] {item[_VI_TITLE][:60]}: {n} gossip items")
            processed += 1

    return processed
