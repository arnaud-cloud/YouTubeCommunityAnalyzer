"""
Commenter credibility scorer — Step 1.5 (between Collect and Summarize).

Pure algorithmic scoring from existing DB data. No LLM calls.
Scores are per-community (relative rankings within that community's channels).
Results cached in the commenter_scores table; consumed by Steps 2 and 3.

Scoring formula (algorithmic):
    quality_score = (
        avg(engagement_normalized) / 100  * 0.20   # community validation
      + channel_spread_score              * 0.20   # breadth of engagement
      + vocab_richness_percentile         * 0.20   # varied vocabulary = analytical thinking
      + like_ratio_percentile             * 0.10   # likes-per-comment within community
      + factual_anchor_ratio              * 0.15   # URL/date mentions
      + avg_length_score                  * 0.15   # comment thoughtfulness
    ) * (1 - reply_penalty)

When llm_tone_score is available (from score_community_tone()), it replaces
vocab_richness_percentile in the formula — it's a better signal for the same slot.

Tiers: A >= 0.65, B >= 0.45, C >= 0.25, D < 0.25
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re

from .db import get_community_channel_ids, get_all_settings
from .llm_client import LLMClient, _settings_to_llm_config

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

# Regex for vocabulary richness: 3+ char words, handles French accents
_WORD_RE = re.compile(r"[a-zA-ZÀ-ÿ]{3,}")

_TONE_SYSTEM_PROMPT = """\
You are evaluating YouTube comment quality. For each commenter listed below, rate their
overall commenting style on a 0.0–1.0 scale based on the sample comments provided.

The score reflects THREE equally important dimensions — weight all three:
  1. POLITENESS / COURTESY: Are they respectful toward creators and other commenters?
     Do they disagree without being hostile? Do they show patience even in frustration?
  2. CONSTRUCTIVENESS: Do they add something — a fact, a question, a nuanced point?
     Or is it empty praise/complaint?
  3. ANALYTICAL DEPTH: Do they engage with specifics, or stay at surface level?

Score anchors:
  0.0 = aggressive, dismissive, rude, or trollish — OR purely sycophantic with zero substance
  0.3 = impolite or impatient even if occasionally making a point
  0.5 = neutral, polite fan engagement — not harmful, not particularly insightful
  0.7 = polite and constructive, engages genuinely
  1.0 = notably courteous even under disagreement, analytical, adds real value

Score each commenter independently. Respond with JSON only — no prose, no markdown fences:
{"scores": [{"author": "<author_name>", "score": 0.0, "reason": "one sentence"}]}"""


def _score_tier(score: float) -> str:
    if score >= 0.65:
        return "A"
    if score >= 0.45:
        return "B"
    if score >= 0.25:
        return "C"
    return "D"


def _vocab_ttr(text: str) -> float | None:
    """Type-token ratio for a single comment. Returns None if too short to be meaningful."""
    words = _WORD_RE.findall(text.lower())
    if len(words) < 5:
        return None
    return len(set(words)) / len(words)


def _load_commenter_stats(conn, channel_ids: list[str]) -> list[dict]:
    """
    Load per-commenter aggregate stats for the given channels.
    Two passes: SQL aggregate query, then Python text scan for factual anchors + vocab richness.
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
                   CASE
                     WHEN SUM(CASE WHEN channel_id != author_channel_id THEN 1 ELSE 0 END) = 0
                       THEN 0.0
                     ELSE SUM(CASE WHEN is_reply = 1 AND channel_id != author_channel_id THEN 1.0 ELSE 0.0 END)
                          / SUM(CASE WHEN channel_id != author_channel_id THEN 1 ELSE 0 END)
                   END AS reply_ratio,
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

    # Text pass: factual anchors + vocabulary richness
    author_ids = list({s["author_channel_id"] for s in stats})
    aid_ph = ",".join("?" * len(author_ids))
    text_rows = conn.execute(
        f"SELECT author_channel_id, text FROM comments "
        f"WHERE channel_id IN ({ph}) AND author_channel_id IN ({aid_ph})",
        channel_ids + author_ids,
    ).fetchall()

    anchor_counts: dict[str, list[int]] = {}
    vocab_data: dict[str, list[float]] = {}
    for r in text_rows:
        aid = r["author_channel_id"]
        text = r["text"] or ""

        # Factual anchors
        has_anchor = 1 if _FACTUAL_RE.search(text) else 0
        if aid not in anchor_counts:
            anchor_counts[aid] = [0, 0]
        anchor_counts[aid][0] += has_anchor
        anchor_counts[aid][1] += 1

        # Vocabulary richness
        ttr = _vocab_ttr(text)
        if ttr is not None:
            vocab_data.setdefault(aid, []).append(ttr)

    for s in stats:
        aid = s["author_channel_id"]
        ac = anchor_counts.get(aid, [0, 1])
        s["factual_ratio"] = ac[0] / max(ac[1], 1)
        ttrs = vocab_data.get(aid, [])
        s["vocab_richness"] = sum(ttrs) / len(ttrs) if ttrs else 0.0

    return stats


def _compute_component_scores(stats: list[dict]) -> list[dict]:
    """
    Normalize raw stats into 0-1 sub-scores and compute the final quality_score.
    Uses vocab_richness_score as the content-quality signal unless llm_tone_score
    is already populated on the row (set by score_community_tone()).
    """
    if not stats:
        return []

    max_channels = max(s["channel_count"] for s in stats)
    log_max = math.log(max_channels + 1)

    # Percentile lists
    like_per_comment_vals = sorted(
        s["total_likes"] / max(s["comment_count"], 1) for s in stats
    )
    vocab_vals = sorted(s.get("vocab_richness", 0.0) for s in stats)
    n = len(like_per_comment_vals)

    enriched = []
    for s in stats:
        # 1. Engagement normalization (0-100 scale → 0-1)
        eng_score = min(s["avg_engagement_norm"] / 100.0, 1.0)

        # 2. Channel spread: log scale, relative to max in community
        ch_spread = math.log(s["channel_count"] + 1) / log_max if log_max > 0 else 0.0

        # 3. Like-per-comment percentile
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

        # 6. Vocabulary richness percentile
        vr = s.get("vocab_richness", 0.0)
        vrank = bisect.bisect_left(vocab_vals, vr)
        vocab_score = vrank / (n - 1) if n > 1 else 0.5

        # 7. Reply penalty (external channels only)
        reply_penalty = min(s["reply_ratio"] * 0.5, 0.3)

        # Content-quality slot: use LLM tone score when available, else vocab richness
        llm_tone = s.get("llm_tone_score")
        content_score = float(llm_tone) if llm_tone is not None else vocab_score

        raw = (
            eng_score      * 0.20
            + ch_spread    * 0.20
            + content_score * 0.20
            + like_ratio   * 0.10
            + factual      * 0.15
            + length_score * 0.15
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
            "vocab_richness_score": round(vocab_score, 4),
            "reply_penalty":        round(reply_penalty, 4),
            "reply_ratio":          round(s["reply_ratio"], 4),
        })

    return enriched


def score_community(conn, community_id: int) -> int:
    """
    Compute and cache algorithmic credibility scores for all commenters in the community.
    Preserves existing llm_tone_score / llm_tone_reason values if present.
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

    # Preserve existing LLM tone scores across re-scoring
    existing_tone: dict[str, dict] = {
        r["author_channel_id"]: {
            "llm_tone_score": r["llm_tone_score"],
            "llm_tone_reason": r["llm_tone_reason"],
            "llm_tone_backend": r["llm_tone_backend"],
            "llm_tone_model": r["llm_tone_model"],
        }
        for r in conn.execute(
            "SELECT author_channel_id, llm_tone_score, llm_tone_reason, "
            "llm_tone_backend, llm_tone_model "
            "FROM commenter_scores WHERE community_id = ? AND llm_tone_score IS NOT NULL",
            (community_id,),
        ).fetchall()
    }
    for s in stats:
        tone = existing_tone.get(s["author_channel_id"])
        if tone:
            s["llm_tone_score"]   = tone["llm_tone_score"]
            s["llm_tone_reason"]  = tone["llm_tone_reason"]
            s["llm_tone_backend"] = tone["llm_tone_backend"]
            s["llm_tone_model"]   = tone["llm_tone_model"]

    enriched = _compute_component_scores(stats)

    conn.execute(
        "DELETE FROM commenter_scores WHERE community_id = ?", (community_id,)
    )
    conn.executemany(
        """INSERT INTO commenter_scores
               (community_id, author_channel_id, author_name,
                quality_score, tier,
                avg_engagement_norm, channel_spread_score, like_ratio_score,
                factual_anchor_score, avg_length_score, vocab_richness_score,
                llm_tone_score, llm_tone_reason, llm_tone_backend, llm_tone_model,
                reply_penalty, reply_ratio,
                comment_count, channel_count, total_likes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                r["vocab_richness_score"],
                r.get("llm_tone_score"),
                r.get("llm_tone_reason"),
                r.get("llm_tone_backend"),
                r.get("llm_tone_model"),
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


def score_community_tone(conn, community_id: int,
                         progress_callback=None,
                         scope: str = "all") -> int:
    """
    Run an LLM pass to score tone (politeness + constructiveness + depth)
    for commenters in the community. Updates llm_tone_score and llm_tone_reason,
    then recomputes quality_score / tier using the LLM score in the content slot.

    scope: "all" = every commenter, "creators" = channel owners only.
    Commenters already scored with the currently-configured backend+model are skipped.

    Requires commenter scores to already exist (call score_community() first).
    Returns number of commenters scored.
    """
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="tone")
    current_backend = llm.backend
    current_model = llm._model

    # Determine creator channel IDs when scoping to creators only
    creator_ids: set[str] | None = None
    if scope == "creators":
        channel_ids_for_scope = get_community_channel_ids(conn, community_id)
        # Channel IDs in community_sources / community_channels map to channel records
        # The creator's author_channel_id equals their channel_id in the channels table
        creator_ids = set(channel_ids_for_scope)

    # Load scored commenters, filtered by scope and skip already-processed
    query = (
        "SELECT author_channel_id, author_name, llm_tone_backend, llm_tone_model "
        "FROM commenter_scores WHERE community_id = ? ORDER BY quality_score DESC"
    )
    all_rows = conn.execute(query, (community_id,)).fetchall()

    rows = []
    for r in all_rows:
        # Scope filter
        if creator_ids is not None and r["author_channel_id"] not in creator_ids:
            continue
        # Skip if already scored with the same backend+model
        if r["llm_tone_backend"] == current_backend and r["llm_tone_model"] == current_model:
            continue
        rows.append(r)

    if not rows:
        return 0

    channel_ids = get_community_channel_ids(conn, community_id)
    ph = ",".join("?" * len(channel_ids))

    BATCH_SIZE = 10
    COMMENTS_PER_AUTHOR = 30
    total_scored = 0

    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start: batch_start + BATCH_SIZE]
        author_ids = [r["author_channel_id"] for r in batch]
        aid_ph = ",".join("?" * len(author_ids))

        # Fetch top comments per author
        comments_by_author: dict[str, list[str]] = {r["author_channel_id"]: [] for r in batch}
        for cr in conn.execute(
            f"SELECT author_channel_id, text FROM comments "
            f"WHERE channel_id IN ({ph}) AND author_channel_id IN ({aid_ph}) "
            f"AND text IS NOT NULL AND LENGTH(text) > 10 "
            f"ORDER BY like_count DESC",
            channel_ids + author_ids,
        ).fetchall():
            aid = cr["author_channel_id"]
            if len(comments_by_author.get(aid, [])) < COMMENTS_PER_AUTHOR:
                comments_by_author.setdefault(aid, []).append(cr["text"])

        # Build user prompt
        sections = []
        author_name_map = {r["author_channel_id"]: r["author_name"] for r in batch}
        for r in batch:
            aid = r["author_channel_id"]
            name = author_name_map[aid] or aid
            comments = comments_by_author.get(aid, [])
            if not comments:
                continue
            comment_block = "\n".join(f"  - {c[:200]}" for c in comments)
            sections.append(f"COMMENTER: {name}\nCOMMENTS:\n{comment_block}")

        if not sections:
            continue

        user_prompt = (
            f"Rate the following {len(sections)} commenter(s).\n\n"
            + "\n\n".join(sections)
        )

        if progress_callback:
            progress_callback(batch_start, len(rows))

        try:
            result = llm.complete_json(_TONE_SYSTEM_PROMPT, user_prompt, max_tokens=1024)
        except Exception as e:
            log.warning(f"commenter_scoring: tone batch failed: {e}")
            continue

        scores = result.get("scores", []) if isinstance(result, dict) else []

        # Match results back by author name
        name_to_aid = {(r["author_name"] or r["author_channel_id"]): r["author_channel_id"]
                       for r in batch}
        for item in scores:
            author_name = item.get("author", "")
            tone_score = item.get("score")
            reason = item.get("reason", "")
            aid = name_to_aid.get(author_name)
            if aid is None or tone_score is None:
                continue
            try:
                tone_score = float(tone_score)
                tone_score = round(min(max(tone_score, 0.0), 1.0), 4)
            except (TypeError, ValueError):
                continue
            conn.execute(
                "UPDATE commenter_scores "
                "SET llm_tone_score = ?, llm_tone_reason = ?, "
                "    llm_tone_backend = ?, llm_tone_model = ? "
                "WHERE community_id = ? AND author_channel_id = ?",
                (tone_score, reason, current_backend, current_model, community_id, aid),
            )
            total_scored += 1

        conn.commit()

    if total_scored == 0:
        return 0

    # Recompute quality_score / tier now that LLM scores are stored
    # Load fresh stats (existing rows already have llm_tone_score set)
    stats = _load_commenter_stats(conn, channel_ids)
    if not stats:
        return total_scored

    tone_map: dict[str, dict] = {
        r["author_channel_id"]: {
            "llm_tone_score":   r["llm_tone_score"],
            "llm_tone_reason":  r["llm_tone_reason"],
            "llm_tone_backend": r["llm_tone_backend"],
            "llm_tone_model":   r["llm_tone_model"],
        }
        for r in conn.execute(
            "SELECT author_channel_id, llm_tone_score, llm_tone_reason, "
            "llm_tone_backend, llm_tone_model "
            "FROM commenter_scores WHERE community_id = ?",
            (community_id,),
        ).fetchall()
    }
    for s in stats:
        t = tone_map.get(s["author_channel_id"], {})
        if t.get("llm_tone_score") is not None:
            s["llm_tone_score"]   = t["llm_tone_score"]
            s["llm_tone_reason"]  = t["llm_tone_reason"]
            s["llm_tone_backend"] = t["llm_tone_backend"]
            s["llm_tone_model"]   = t["llm_tone_model"]

    enriched = _compute_component_scores(stats)

    conn.execute(
        "DELETE FROM commenter_scores WHERE community_id = ?", (community_id,)
    )
    conn.executemany(
        """INSERT INTO commenter_scores
               (community_id, author_channel_id, author_name,
                quality_score, tier,
                avg_engagement_norm, channel_spread_score, like_ratio_score,
                factual_anchor_score, avg_length_score, vocab_richness_score,
                llm_tone_score, llm_tone_reason, llm_tone_backend, llm_tone_model,
                reply_penalty, reply_ratio,
                comment_count, channel_count, total_likes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                r["vocab_richness_score"],
                r.get("llm_tone_score"),
                r.get("llm_tone_reason"),
                r.get("llm_tone_backend"),
                r.get("llm_tone_model"),
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
    if progress_callback:
        progress_callback(len(rows), len(rows))
    log.info(
        f"commenter_scoring: tone-scored {total_scored} commenters "
        f"for community {community_id}"
    )
    return total_scored


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
