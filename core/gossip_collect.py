"""
Gossip collector — Step 1: Fetch comments into the database.

Fully incremental: already-collected comments are skipped via PRIMARY KEY
deduplication. Re-running adds only new data.

Dispatches to per-platform collectors registered in source_collector.py.
"""

from __future__ import annotations

import logging
import time

from googleapiclient.errors import HttpError

from .db import get_community_sources, get_community_channel_ids, get_setting, get_all_settings
from .source_collector import CollectResult, register_collector
# Import reddit_api to register RedditCollector (side-effect import, OK to fail if praw missing)
try:
    from . import reddit_api as _reddit_api  # noqa: F401
except ImportError:
    pass

from .youtube_api import (
    QuotaTracker,
    build_youtube,
    fetch_channel_videos_for_gossip,
    fetch_comments_for_video,
)

log = logging.getLogger(__name__)


# ── Shared DB helpers (used by all collectors) ────────────────────────────────

def _upsert_channel(conn, channel_id: str, channel_name: str,
                    handle: str | None, source_type: str = "youtube"):
    conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle) "
        "VALUES (?, ?, ?)",
        (channel_id, channel_name or "", handle),
    )
    conn.commit()


def _upsert_video(conn, video: dict, channel_id: str,
                  source_type: str = "youtube"):
    conn.execute(
        """INSERT INTO videos
               (video_id, channel_id, title, published_at, comment_count,
                collected_at, source_type)
           VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
           ON CONFLICT(video_id) DO UPDATE SET
               title = excluded.title,
               comment_count = excluded.comment_count,
               collected_at = datetime('now')""",
        (video["video_id"], channel_id, video["title"],
         video["published_at"], video.get("comment_count", 0), source_type),
    )
    conn.commit()


def _insert_comments(conn, comments: list[dict],
                     video_id: str, channel_id: str,
                     source_type: str = "youtube") -> int:
    inserted = 0
    for c in comments:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO comments
                       (comment_id, video_id, channel_id, author_name,
                        author_channel_id, text, like_count, published_at,
                        is_reply, parent_id, source_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (c["comment_id"], video_id, channel_id,
                 c["author_name"], c.get("author_channel_id", ""),
                 c["text"], c["like_count"], c["published_at"],
                 c["is_reply"], c.get("parent_id"), source_type),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as e:
            log.warning(f"Failed to insert comment {c['comment_id']}: {e}")
    conn.commit()
    return inserted


# ── YouTube collector ─────────────────────────────────────────────────────────

def _resolve_channel_id(youtube, id_or_handle: str,
                        quota: QuotaTracker) -> tuple[str | None, str | None]:
    """Resolve @handle to UC... channel ID. Returns (channel_id, name)."""
    id_or_handle = id_or_handle.strip()
    if id_or_handle.startswith("UC") and len(id_or_handle) == 24:
        return id_or_handle, None
    handle = id_or_handle.lstrip("@")
    try:
        resp = youtube.channels().list(
            part="id,snippet", forHandle=handle
        ).execute()
        quota.charge("channels.list")
        items = resp.get("items", [])
        if not items:
            return None, None
        return items[0]["id"], items[0]["snippet"]["title"]
    except HttpError as e:
        log.error(f"Handle resolution failed for '@{handle}': {e}")
        return None, None


def collect_channel_comments(
    conn, youtube, channel_id: str,
    max_videos: int | None,
    after: str | None,
    max_comments: int,
    fetch_replies: bool,
    quota: QuotaTracker,
    progress_callback=None,
) -> None:
    """Collect comments for all videos of a single YouTube channel."""
    channel_row = conn.execute(
        "SELECT channel_name, handle FROM channels WHERE channel_id = ?",
        (channel_id,)
    ).fetchone()
    channel_name = channel_row["channel_name"] if channel_row else channel_id
    handle = channel_row["handle"] if channel_row else None

    log.info(f"=== {channel_name} ({channel_id}) ===")
    _upsert_channel(conn, channel_id, channel_name, handle, source_type="youtube")

    videos = fetch_channel_videos_for_gossip(
        youtube, channel_id, max_videos, after, quota
    )
    log.info(f"  {len(videos)} videos found")

    if progress_callback:
        progress_callback(f"channel\t{channel_name}\t{len(videos)} videos")

    channel_new = 0
    for i, video in enumerate(videos, 1):
        vid_id = video["video_id"]
        log.info(f"  [{i}/{len(videos)}] {video['title'][:65]}")
        _upsert_video(conn, video, channel_id, source_type="youtube")

        comments = fetch_comments_for_video(
            youtube, vid_id, max_comments, fetch_replies, quota
        )
        new_count = _insert_comments(conn, comments, vid_id, channel_id, "youtube")
        channel_new += new_count
        log.info(f"    {len(comments)} fetched, {new_count} new")

        if progress_callback:
            ch_handle = handle or channel_name
            status = "new" if new_count > 0 else "skip"
            progress_callback(
                f"video\t{ch_handle}\t{i}/{len(videos)}\t{video['title'][:60]}"
                f"\t{len(comments)} fetched\t{new_count} new\t{status}"
            )
        time.sleep(0.3)

    if progress_callback:
        progress_callback(f"done\t{channel_name}\t{channel_new} new comments")


class YouTubeCollector:
    """SourceCollector implementation for YouTube channels."""

    source_type = "youtube"

    def collect(self, conn, source_id: str, settings: dict[str, str],
                progress_callback=None) -> CollectResult:
        api_key = settings.get("youtube_api_key", "")
        if not api_key:
            raise ValueError("YouTube API key not configured. Go to Settings.")

        youtube = build_youtube(api_key)
        quota = QuotaTracker()

        max_videos = None
        raw = settings.get("max_videos_per_channel", "")
        if raw and raw != "null":
            max_videos = int(raw)
        after = settings.get("date_filter_after", "") or None
        max_comments = int(settings.get("max_comments_per_video", "500"))
        fetch_replies = settings.get("fetch_replies", "true").lower() == "true"

        try:
            collect_channel_comments(
                conn, youtube, source_id,
                max_videos, after, max_comments, fetch_replies,
                quota, progress_callback,
            )
        except Exception as e:
            log.error(f"Failed on YouTube channel {source_id}: {e}", exc_info=True)

        if progress_callback:
            progress_callback(
                f"quota_update\t{quota.total}\t{quota.DAILY_FREE_QUOTA}"
            )

        return CollectResult(quota_info=quota.summary())


# Register at import time
register_collector(YouTubeCollector())


# ── Community-level orchestration ─────────────────────────────────────────────

def collect_community(conn, community_id: int,
                      progress_callback=None) -> str:
    """
    Collect comments for all sources in a community.
    Dispatches each source to the appropriate platform collector.
    Returns a summary string.
    """
    from .source_collector import get_collector

    settings = get_all_settings(conn)
    sources = get_community_sources(conn, community_id)

    if not sources:
        # Fallback: pure YouTube community using legacy community_channels
        channel_ids = get_community_channel_ids(conn, community_id)
        sources = [
            {"source_type": "youtube", "source_id": cid, "display_name": cid}
            for cid in channel_ids
        ]

    summaries: list[str] = []
    for source in sources:
        stype = source["source_type"]
        sid = source["source_id"]
        try:
            collector = get_collector(stype)
            result = collector.collect(conn, sid, settings, progress_callback)
            if result.quota_info:
                summaries.append(result.quota_info)
        except ValueError as e:
            log.error(f"No collector for source_type={stype!r}: {e}")
            if progress_callback:
                progress_callback(f"info\tSkipped {sid}: {e}")
        except Exception as e:
            log.error(f"Failed collecting {stype}:{sid}: {e}", exc_info=True)

    summary = "\n".join(summaries) if summaries else "No quota used."
    log.info("\n" + summary)
    if progress_callback:
        progress_callback(f"info\tCollection complete")
    return summary
