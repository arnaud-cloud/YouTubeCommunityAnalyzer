"""
Gossip aggregator — Step 3: Pure computation, no LLM calls.

Reads all video_summaries for a community and computes entity metrics,
gossip corpus, corroborated claims, cross-mention asymmetries, comment
velocity, and top cross-channel commenters.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

from .db import get_community_channel_ids

log = logging.getLogger(__name__)


def _safe_json_loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _load_summaries(conn, channel_ids: list[str]) -> list[dict]:
    placeholders = ",".join("?" * len(channel_ids))
    q = f"""
        SELECT vs.video_id, vs.channel_id, vs.summary_json,
               vs.gossip_count, vs.processed_at,
               v.title, v.published_at,
               c.channel_name
        FROM video_summaries vs
        JOIN videos v ON vs.video_id = v.video_id
        JOIN channels c ON vs.channel_id = c.channel_id
        WHERE vs.channel_id IN ({placeholders})
        ORDER BY v.published_at ASC
    """
    result = []
    for r in conn.execute(q, channel_ids).fetchall():
        d = dict(r)
        d["summary"] = _safe_json_loads(d.pop("summary_json"), {})
        result.append(d)
    return result


def _build_entity_metrics(summaries: list[dict]) -> dict:
    sentiments: dict[str, list[float]] = defaultdict(list)
    mentions: dict[str, int] = defaultdict(int)
    by_channel: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    video_ids: dict[str, list[str]] = defaultdict(list)

    for s in summaries:
        summary = s.get("summary", {})
        channel_name = s.get("channel_name", s["channel_id"])
        video_id = s["video_id"]
        sentiment_map = summary.get("sentiment_map", {})

        for entity in summary.get("entities_mentioned", []):
            mentions[entity] += 1
            by_channel[entity][channel_name] += 1
            video_ids[entity].append(video_id)
            if entity in sentiment_map:
                sentiments[entity].append(sentiment_map[entity])

    metrics = {}
    for entity in mentions:
        sents = sentiments.get(entity, [0.0])
        metrics[entity] = {
            "entity": entity,
            "total_mentions": mentions[entity],
            "avg_sentiment": round(sum(sents) / len(sents), 3),
            "by_channel": dict(by_channel[entity]),
            "video_ids": list(set(video_ids[entity])),
            "channel_count": len(by_channel[entity]),
        }
    return metrics


def _build_gossip_corpus(summaries: list[dict]) -> list[dict]:
    corpus = []
    for s in summaries:
        for item in s.get("summary", {}).get("gossip_items", []):
            corpus.append({
                **item,
                "video_id": s["video_id"],
                "channel_id": s["channel_id"],
                "channel_name": s.get("channel_name", ""),
                "video_title": s.get("title", ""),
                "published_at": s.get("published_at", ""),
            })
    return corpus


def _find_corroborated_claims(corpus: list[dict]) -> list[dict]:
    clusters: dict[str, list[dict]] = defaultdict(list)
    for item in corpus:
        subjects = tuple(sorted(item.get("subjects", [])))
        key = f"{item.get('gossip_type', '')}::{'::'.join(subjects)}"
        clusters[key].append(item)

    corroborated = []
    for items in clusters.values():
        video_ids = list({i["video_id"] for i in items})
        if len(video_ids) >= 2:
            corroborated.append({
                "gossip_type": items[0].get("gossip_type"),
                "subjects": items[0].get("subjects", []),
                "occurrences": len(items),
                "video_ids": video_ids,
                "channel_names": list({i["channel_name"] for i in items}),
                "claims": [i.get("claim", "") for i in items],
            })
    return sorted(corroborated, key=lambda x: -x["occurrences"])


def _build_cross_mention_asymmetry(summaries: list[dict]) -> list[dict]:
    channel_mentions: dict[str, set[str]] = defaultdict(set)
    entity_mentioned_by: dict[str, set[str]] = defaultdict(set)

    for s in summaries:
        ch = s.get("channel_name", s["channel_id"])
        for entity in s.get("summary", {}).get("entities_mentioned", []):
            channel_mentions[ch].add(entity)
            entity_mentioned_by[entity].add(ch)

    channel_names = set(channel_mentions.keys())
    asymmetries = []
    for entity, mentioning in entity_mentioned_by.items():
        if entity not in channel_names:
            continue
        outbound = channel_mentions.get(entity, set())
        missing = (mentioning - {entity}) - outbound
        if len(missing) >= 1:
            asymmetries.append({
                "entity": entity,
                "mentioned_by": sorted(mentioning - {entity}),
                "does_not_mention_back": sorted(missing),
                "asymmetry_score": len(missing),
            })
    return sorted(asymmetries, key=lambda x: -x["asymmetry_score"])


def _build_comment_velocity(summaries: list[dict]) -> dict:
    velocity: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in summaries:
        month = (s.get("published_at") or "")[:7]
        if not month:
            continue
        for entity in s.get("summary", {}).get("entities_mentioned", []):
            velocity[entity][month] += 1
    return {
        entity: dict(sorted(months.items()))
        for entity, months in velocity.items()
    }


def _build_top_commenters(conn, summaries: list[dict],
                           min_channels: int = 2) -> list[dict]:
    video_ids = [s["video_id"] for s in summaries]
    if not video_ids:
        return []
    placeholders = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"""SELECT author_channel_id, author_name,
                   COUNT(DISTINCT channel_id) AS channel_count,
                   COUNT(DISTINCT video_id) AS video_count,
                   SUM(like_count) AS total_likes,
                   COUNT(*) AS comment_count
            FROM comments
            WHERE video_id IN ({placeholders})
              AND author_channel_id != ''
            GROUP BY author_channel_id
            HAVING channel_count >= ?
            ORDER BY channel_count DESC, total_likes DESC
            LIMIT 30""",
        video_ids + [min_channels],
    ).fetchall()
    return [dict(r) for r in rows]


def aggregate_community(conn, community_id: int,
                        progress_callback=None) -> int:
    """
    Aggregate all summaries for a community.
    Returns the aggregation_results.id.
    """
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        raise ValueError("Community has no channels")

    if progress_callback:
        progress_callback("Loading video summaries...")

    summaries = _load_summaries(conn, channel_ids)
    log.info(f"Loaded {len(summaries)} video summaries")
    if not summaries:
        raise ValueError("No summaries found -- run summarize step first.")

    if progress_callback:
        progress_callback("Computing entity metrics...")
    entity_metrics = _build_entity_metrics(summaries)
    log.info(f"  {len(entity_metrics)} unique entities tracked")

    gossip_corpus = _build_gossip_corpus(summaries)
    log.info(f"  {len(gossip_corpus)} gossip items total")

    corroborated = _find_corroborated_claims(gossip_corpus)
    log.info(f"  {len(corroborated)} corroborated clusters")

    asymmetries = _build_cross_mention_asymmetry(summaries)
    velocity = _build_comment_velocity(summaries)
    top_commenters = _build_top_commenters(conn, summaries)
    log.info(f"  {len(top_commenters)} cross-channel superfans found")

    dates = [s["published_at"] for s in summaries if s.get("published_at")]
    date_start = min(dates)[:10] if dates else ""
    date_end = max(dates)[:10] if dates else ""

    cur = conn.execute(
        """INSERT INTO aggregation_results
               (community_id, channels_included, date_range_start, date_range_end,
                entity_metrics_json, gossip_corpus_json, corroborated_json,
                asymmetries_json, comment_velocity_json, top_commenters_json,
                total_videos, total_gossip_items, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (
            community_id,
            json.dumps(channel_ids), date_start, date_end,
            json.dumps(entity_metrics), json.dumps(gossip_corpus),
            json.dumps(corroborated), json.dumps(asymmetries),
            json.dumps(velocity), json.dumps(top_commenters),
            len(summaries), len(gossip_corpus),
        ),
    )
    conn.commit()
    agg_id = cur.lastrowid
    log.info(f"Aggregation saved (id={agg_id})")
    return agg_id
