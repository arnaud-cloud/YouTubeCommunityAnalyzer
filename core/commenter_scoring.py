"""
Commenter credibility scorer — Step 1.5 (between Collect and Summarize).

Pure algorithmic scoring from existing DB data. No LLM calls.
Scores are per-community (relative rankings within that community's channels).
Results cached in the commenter_scores table; consumed by Steps 2 and 3.

Scoring formula:
    quality_score = (
        avg(engagement_normalized) / 100  * 0.35   # community validation
      + log(channel_count+1) / log(max+1) * 0.25   # breadth of engagement
      + like_ratio_percentile              * 0.20   # likes-per-comment within community
      + factual_anchor_ratio               * 0.10   # URL/date mentions
      + avg_length_score                   * 0.10   # comment thoughtfulness
    ) * (1 - min(reply_ratio * 0.5, 0.3))           # penalty for high reply ratio

Tiers: A >= 0.65, B >= 0.45, C >= 0.25, D < 0.25
"""

from __future__ import annotations

import bisect
import logging
import math
import re

from .db import get_community_channel_ids

log = logging.getLogger(__name__)

# Regex for factual anchor detection: URLs and date-like patterns
_FACTUAL_RE = re.compile(
    r"https?://\S+"
    r"|"
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"
    r"|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4}\b",
    re.IGNORECASE,
)


def _score_tier(score: float) -> str:
    if score >= 0.65:
        return "A"
    if score >= 0.45:
        return "B"
    if score >= 0.25:
        return "C"
    return "D"


def _load_commenter_stats(conn, channel_ids: list[str]) -> list[dict]:
    """
    Load per-commenter aggregate stats for the given channels.
    Two passes: SQL aggregate query, then Python text scan for factual anchors.
    """
    if not channel_ids:
        return []

    ph = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        f"""SELECT author_channel_id,
                   MAX(author_name)                                 AS author_name,
                   COUNT(*)                                         AS comment_count,
                   COUNT(DISTINCT channel_id)                       AS channel_count,
                   SUM(like_count)                                  AS total_likes,
                   AVG(COALESCE(engagement_normalized, 0))          AS avg_engagement_norm,
                   SUM(CASE WHEN is_reply = 1 THEN 1.0 ELSE 0.0 END) / COUNT(*) AS reply_ratio,
                   AVG(LENGTH(COALESCE(text, '')))                  AS avg_length
            FROM comments
            WHERE channel_id IN ({ph})
              AND author_channel_id IS NOT NULL
              AND author_channel_id != ''
            GROUP BY author_channel_id""",
        channel_ids,
    ).fetchall()

    stats = [dict(r) for r in rows]
    if not stats:
        return stats

    # Build author → anchor count map via Python regex
    author_ids = list({s["author_channel_id"] for s in stats})
    aid_ph = ",".join("?" * len(author_ids))
    text_rows = conn.execute(
        f"SELECT author_channel_id, text FROM comments "
        f"WHERE channel_id IN ({ph}) AND author_channel_id IN ({aid_ph})",
        channel_ids + author_ids,
    ).fetchall()

    anchor_counts: dict[str, list[int]] = {}
    for r in text_rows:
        aid = r["author_channel_id"]
        has_anchor = 1 if _FACTUAL_RE.search(r["text"] or "") else 0
        if aid not in anchor_counts:
            anchor_counts[aid] = [0, 0]
        anchor_counts[aid][0] += has_anchor
        anchor_counts[aid][1] += 1

    for s in stats:
        aid = s["author_channel_id"]
        ac = anchor_counts.get(aid, [0, 1])
        s["factual_ratio"] = ac[0] / max(ac[1], 1)

    return stats


def _compute_component_scores(stats: list[dict]) -> list[dict]:
    """
    Normalize raw stats into 0-1 sub-scores and compute the final quality_score.
    """
    if not stats:
        return []

    max_channels = max(s["channel_count"] for s in stats)
    log_max = math.log(max_channels + 1)

    # Build sorted like-per-comment list for percentile computation
    like_per_comment_vals = sorted(
        s["total_likes"] / max(s["comment_count"], 1) for s in stats
    )
    n = len(like_per_comment_vals)

    enriched = []
    for s in stats:
        # 1. Engagement normalization (0-100 scale → 0-1)
        eng_score = min(s["avg_engagement_norm"] / 100.0, 1.0)

        # 2. Channel spread: log scale, relative to max in community
        ch_spread = math.log(s["channel_count"] + 1) / log_max if log_max > 0 else 0.0

        # 3. Like-per-comment percentile (bisect for O(log n), handles ties)
        lpc = s["total_likes"] / max(s["comment_count"], 1)
        rank = bisect.bisect_left(like_per_comment_vals, lpc)
        like_ratio = rank / (n - 1) if n > 1 else 0.5

        # 4. Factual anchor ratio (already 0-1)
        factual = min(s.get("factual_ratio", 0.0), 1.0)

        # 5. Comment length score: trapezoid (50=0, 50-300=1, >300 diminishes)
        avg_len = s["avg_length"] or 0.0
        if avg_len < 50:
            length_score = avg_len / 50.0
        elif avg_len <= 300:
            length_score = 1.0
        else:
            length_score = max(0.0, 1.0 - (avg_len - 300) / 500.0)

        # 6. Reply penalty
        reply_penalty = min(s["reply_ratio"] * 0.5, 0.3)

        raw = (
            eng_score   * 0.35
            + ch_spread * 0.25
            + like_ratio * 0.20
            + factual    * 0.10
            + length_score * 0.10
        )
        quality_score = round(min(max(raw * (1.0 - reply_penalty), 0.0), 1.0), 4)

        enriched.append({
            **s,
            "quality_score":        quality_score,
            "tier":                 _score_tier(quality_score),
            "avg_eng_score":        round(eng_score, 4),
            "channel_spread_score": round(ch_spread, 4),
            "like_ratio_score":     round(like_ratio, 4),
            "factual_anchor_score": round(factual, 4),
            "avg_length_score":     round(length_score, 4),
            "reply_penalty":        round(reply_penalty, 4),
            "reply_ratio":          round(s["reply_ratio"], 4),
        })

    return enriched


def score_community(conn, community_id: int) -> int:
    """
    Compute and cache credibility scores for all commenters in the community.
    Clears existing scores for the community before inserting fresh ones.
    Returns the number of commenters scored.
    """
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        log.warning(f"commenter_scoring: community {community_id} has no channels")
        return 0

    stats = _load_commenter_stats(conn, channel_ids)
    if not stats:
        log.info(f"commenter_scoring: no comments found for community {community_id}")
        return 0

    enriched = _compute_component_scores(stats)

    conn.execute(
        "DELETE FROM commenter_scores WHERE community_id = ?", (community_id,)
    )
    conn.executemany(
        """INSERT INTO commenter_scores
               (community_id, author_channel_id, author_name,
                quality_score, tier,
                avg_engagement_norm, channel_spread_score, like_ratio_score,
                factual_anchor_score, avg_length_score, reply_penalty, reply_ratio,
                comment_count, channel_count, total_likes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                community_id,
                r["author_channel_id"],
                r["author_name"],
                r["quality_score"],
                r["tier"],
                r["avg_eng_score"],
                r["channel_spread_score"],
                r["like_ratio_score"],
                r["factual_anchor_score"],
                r["avg_length_score"],
                r["reply_penalty"],
                r["reply_ratio"],
                r["comment_count"],
                r["channel_count"],
                r["total_likes"],
            )
            for r in enriched
        ],
    )
    conn.commit()
    log.info(
        f"commenter_scoring: scored {len(enriched)} commenters "
        f"for community {community_id}"
    )
    return len(enriched)


def get_scores_for_community(conn, community_id: int) -> dict[str, dict]:
    """
    Return {author_channel_id: score_row} for fast lookup during batch formatting.
    Returns empty dict if no scores have been computed yet.
    """
    rows = conn.execute(
        "SELECT * FROM commenter_scores WHERE community_id = ?",
        (community_id,),
    ).fetchall()
    return {r["author_channel_id"]: dict(r) for r in rows}
