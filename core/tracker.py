"""
Channel metrics tracker — collects daily channel + video snapshots into SQLite.

Replaces the JSONL-based storage of the original HobbyTracker project.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from googleapiclient.errors import HttpError

from .db import get_community_channel_ids, get_setting
from .youtube_api import (
    build_youtube,
    fetch_all_video_ids,
    fetch_channel_stats,
    fetch_channels_subscriber_counts,
    fetch_recent_video_ids,
    fetch_video_details,
    is_quota_exceeded,
)

log = logging.getLogger(__name__)


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Single-channel collection ────────────────────────────────────────────────

def collect_channel_snapshot(conn, youtube, channel_id: str) -> dict | None:
    """Fetch and store a daily channel stats snapshot. Returns the stats dict."""
    try:
        stats = fetch_channel_stats(youtube, channel_id)
    except HttpError as e:
        if is_quota_exceeded(e):
            raise
        log.error(f"Channel stats error for {channel_id}: {e}")
        return None

    if not stats:
        log.warning(f"No data returned for channel {channel_id}")
        return None

    today = today_str()

    # Upsert the channel row with latest metadata
    conn.execute(
        """INSERT INTO channels
               (channel_id, channel_name, handle, description, custom_url,
                country, published_at, thumbnail_url, keywords, topic_categories)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(channel_id) DO UPDATE SET
               channel_name = excluded.channel_name,
               handle = excluded.handle,
               description = excluded.description,
               custom_url = excluded.custom_url,
               country = excluded.country,
               published_at = excluded.published_at,
               thumbnail_url = excluded.thumbnail_url,
               keywords = excluded.keywords,
               topic_categories = excluded.topic_categories""",
        (
            stats["channel_id"], stats["title"], stats["custom_url"],
            stats["description"], stats["custom_url"], stats["country"],
            stats["published_at"], stats["thumbnail_medium"],
            stats["keywords"], json.dumps(stats["topic_categories"]),
        ),
    )

    # Insert daily snapshot (UNIQUE constraint skips duplicates)
    conn.execute(
        """INSERT OR IGNORE INTO channel_snapshots
               (channel_id, snapshot_date, subscriber_count, video_count,
                view_count, hidden_subscriber, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            channel_id, today,
            stats["subscriber_count"], stats["video_count"],
            stats["view_count"], int(stats["hidden_subscriber"]),
            json.dumps(stats),
        ),
    )
    conn.commit()
    log.info(
        f"  {stats['title']}: {stats['subscriber_count']:,} subs, "
        f"{stats['view_count']:,} views"
    )
    return stats


def collect_video_snapshots(conn, youtube, channel_id: str,
                            uploads_playlist: str,
                            backfill: bool = False) -> int:
    """
    Fetch video metadata and daily view/like/comment snapshots.

    On first run or backfill: fetches ALL videos.
    On daily runs: checks the 50 most recent, re-snapshots all known.

    Returns the number of videos snapshotted.
    """
    today = today_str()

    # Check existing videos in DB for this channel
    existing_rows = conn.execute(
        "SELECT video_id FROM videos WHERE channel_id = ?", (channel_id,)
    ).fetchall()
    existing_ids = {r["video_id"] for r in existing_rows}

    if backfill or not existing_ids:
        log.info(f"  Fetching ALL video IDs (backfill={backfill})...")
        all_ids = fetch_all_video_ids(youtube, uploads_playlist)
        log.info(f"  Found {len(all_ids)} videos total")
    else:
        recent_ids = fetch_recent_video_ids(youtube, uploads_playlist)
        all_ids = list(existing_ids | set(recent_ids))
        new_count = len(set(recent_ids) - existing_ids)
        if new_count:
            log.info(f"  {new_count} new videos found")

    # Fetch full details in batches
    try:
        videos = fetch_video_details(youtube, all_ids)
    except HttpError as e:
        if is_quota_exceeded(e):
            raise
        log.error(f"  Video details error: {e}")
        return 0

    for v in videos:
        # Upsert video metadata
        conn.execute(
            """INSERT INTO videos
                   (video_id, channel_id, title, description, published_at,
                    duration, tags, category_id, definition, has_captions,
                    topic_categories, thumbnail_url, privacy_status,
                    comment_count, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(video_id) DO UPDATE SET
                   title = excluded.title,
                   description = excluded.description,
                   duration = excluded.duration,
                   tags = excluded.tags,
                   category_id = excluded.category_id,
                   definition = excluded.definition,
                   has_captions = excluded.has_captions,
                   topic_categories = excluded.topic_categories,
                   thumbnail_url = excluded.thumbnail_url,
                   privacy_status = excluded.privacy_status,
                   comment_count = excluded.comment_count,
                   collected_at = datetime('now')""",
            (
                v["video_id"], v["channel_id"], v["title"],
                v["description"], v["published_at"], v["duration"],
                json.dumps(v["tags"]), v["category_id"], v["definition"],
                int(v["has_captions"]), json.dumps(v["topic_categories"]),
                v["thumbnail_url"], v["privacy_status"], v["comment_count"],
            ),
        )

        # Daily snapshot
        conn.execute(
            """INSERT OR IGNORE INTO video_snapshots
                   (video_id, channel_id, snapshot_date,
                    view_count, like_count, comment_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                v["video_id"], v["channel_id"], today,
                v["view_count"], v["like_count"], v["comment_count"],
            ),
        )

    conn.commit()
    log.info(f"  Snapshotted {len(videos)} videos")
    return len(videos)


# ── Community-level collection ───────────────────────────────────────────────

def collect_community(conn, youtube, community_id: int,
                      backfill: bool = False) -> None:
    """Collect channel + video snapshots for all channels in a community."""
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        log.warning(f"Community {community_id} has no channels")
        return

    community_name = conn.execute(
        "SELECT name FROM communities WHERE id = ?", (community_id,)
    ).fetchone()["name"]
    log.info(f"=== Collecting community: {community_name} ({len(channel_ids)} channels) ===")

    for cid in channel_ids:
        try:
            stats = collect_channel_snapshot(conn, youtube, cid)
            if stats and stats.get("uploads_playlist"):
                collect_video_snapshots(
                    conn, youtube, cid,
                    stats["uploads_playlist"],
                    backfill=backfill,
                )
            time.sleep(0.3)
        except Exception as e:
            log.error(f"Error collecting {cid}: {e}", exc_info=True)


def collect_all_communities(conn, youtube, backfill: bool = False) -> None:
    """Collect snapshots for every community in the database."""
    communities = conn.execute("SELECT id FROM communities").fetchall()
    for row in communities:
        collect_community(conn, youtube, row["id"], backfill=backfill)


def collect_prioritized(conn, youtube) -> None:
    """
    Quota-aware collection across all communities.

    Priority order:
      1. Channels with no snapshots yet, sorted by subscriber_count ascending
         (batch-fetched cheaply so we maximise new channels per quota unit).
      2. Channels with existing snapshots, stalest first.

    Stops gracefully and logs remaining channels when YouTube returns a
    quotaExceeded / dailyLimitExceeded error.
    """
    all_rows = conn.execute(
        "SELECT DISTINCT channel_id FROM community_channels"
    ).fetchall()
    all_ids = [r["channel_id"] for r in all_rows]

    if not all_ids:
        log.warning("No channels in any community.")
        return

    # Split by whether any snapshot exists
    new_ids: list[str] = []
    existing_ids: list[str] = []
    for cid in all_ids:
        has_data = conn.execute(
            "SELECT 1 FROM channel_snapshots WHERE channel_id = ? LIMIT 1", (cid,)
        ).fetchone()
        if has_data:
            existing_ids.append(cid)
        else:
            new_ids.append(cid)

    log.info(f"Channels: {len(new_ids)} new (no data yet), {len(existing_ids)} existing")

    # Sort new channels by subscriber count ascending (smallest → most new channels per unit)
    if new_ids:
        try:
            sub_counts = fetch_channels_subscriber_counts(youtube, new_ids)
            new_ids.sort(key=lambda cid: sub_counts.get(cid, 0))
            log.info(
                f"New channel order (smallest first): "
                + ", ".join(
                    f"{cid}({sub_counts.get(cid,0):,})" for cid in new_ids[:5]
                )
                + ("..." if len(new_ids) > 5 else "")
            )
        except HttpError as e:
            if is_quota_exceeded(e):
                log.error("Quota exceeded even before collection started.")
                return
            log.warning(f"Could not pre-fetch subscriber counts: {e}")
            # Proceed without sorting

    # Sort existing channels stalest first
    if existing_ids:
        staleness: dict[str, str] = {}
        for cid in existing_ids:
            row = conn.execute(
                "SELECT MAX(snapshot_date) FROM channel_snapshots WHERE channel_id = ?",
                (cid,),
            ).fetchone()
            staleness[cid] = row[0] or ""
        existing_ids.sort(key=lambda cid: staleness[cid])

    ordered = new_ids + existing_ids
    collected = 0

    for i, cid in enumerate(ordered):
        try:
            stats = collect_channel_snapshot(conn, youtube, cid)
            if stats and stats.get("uploads_playlist"):
                collect_video_snapshots(conn, youtube, cid, stats["uploads_playlist"])
            collected += 1
            time.sleep(0.3)
        except HttpError as e:
            if is_quota_exceeded(e):
                remaining = len(ordered) - i
                log.warning(
                    f"YouTube quota exceeded after {collected} channels. "
                    f"{remaining} channel(s) not collected today."
                )
                break
            log.error(f"HTTP error collecting {cid}: {e}")
        except Exception as e:
            log.error(f"Unexpected error collecting {cid}: {e}", exc_info=True)

    log.info(f"Prioritized collection done: {collected}/{len(ordered)} channels collected.")
