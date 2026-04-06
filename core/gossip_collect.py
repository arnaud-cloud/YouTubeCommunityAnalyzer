"""
Gossip collector — Step 1: Fetch YouTube comments into the database.

Fully incremental: already-collected comments are skipped via PRIMARY KEY
deduplication. Re-running adds only new data.
"""

from __future__ import annotations

import logging
import time

from googleapiclient.errors import HttpError

from .db import get_community_channel_ids, get_setting, get_all_settings
from .youtube_api import (
    QuotaTracker,
    build_youtube,
    channel_id_to_uploads_playlist,
    fetch_channel_videos_for_gossip,
    fetch_comments_for_video,
)

log = logging.getLogger(__name__)


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


def _upsert_channel(conn, channel_id: str, channel_name: str,
                    handle: str | None):
    conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle) "
        "VALUES (?, ?, ?)",
        (channel_id, channel_name or "", handle),
    )
    conn.commit()


def _upsert_video(conn, video: dict, channel_id: str):
    conn.execute(
        """INSERT INTO videos
               (video_id, channel_id, title, published_at, comment_count, collected_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(video_id) DO UPDATE SET
               title = excluded.title,
               comment_count = excluded.comment_count,
               collected_at = datetime('now')""",
        (video["video_id"], channel_id, video["title"],
         video["published_at"], video.get("comment_count", 0)),
    )
    conn.commit()


def _insert_comments(conn, comments: list[dict],
                     video_id: str, channel_id: str) -> int:
    inserted = 0
    for c in comments:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO comments
                       (comment_id, video_id, channel_id, author_name,
                        author_channel_id, text, like_count, published_at,
                        is_reply, parent_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (c["comment_id"], video_id, channel_id,
                 c["author_name"], c["author_channel_id"],
                 c["text"], c["like_count"], c["published_at"],
                 c["is_reply"], c["parent_id"]),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as e:
            log.warning(f"Failed to insert comment {c['comment_id']}: {e}")
    conn.commit()
    return inserted


def collect_channel_comments(
    conn, youtube, channel_id: str,
    max_videos: int | None,
    after: str | None,
    max_comments: int,
    fetch_replies: bool,
    quota: QuotaTracker,
    progress_callback=None,
) -> None:
    """Collect comments for all videos of a single channel."""
    channel_row = conn.execute(
        "SELECT channel_name, handle FROM channels WHERE channel_id = ?",
        (channel_id,)
    ).fetchone()
    channel_name = channel_row["channel_name"] if channel_row else channel_id
    handle = channel_row["handle"] if channel_row else None

    log.info(f"=== {channel_name} ({channel_id}) ===")
    _upsert_channel(conn, channel_id, channel_name, handle)

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
        _upsert_video(conn, video, channel_id)

        comments = fetch_comments_for_video(
            youtube, vid_id, max_comments, fetch_replies, quota
        )
        new_count = _insert_comments(conn, comments, vid_id, channel_id)
        channel_new += new_count
        log.info(f"    {len(comments)} fetched, {new_count} new")

        if progress_callback:
            status = "new" if new_count > 0 else "skip"
            progress_callback(
                f"video\t{i}/{len(videos)}\t{video['title'][:60]}"
                f"\t{len(comments)} fetched\t{new_count} new\t{status}"
            )
        time.sleep(0.3)

    if progress_callback:
        progress_callback(f"done\t{channel_name}\t{channel_new} new comments")


def collect_community(conn, community_id: int,
                      progress_callback=None) -> str:
    """
    Collect comments for all channels in a community.
    Returns the quota summary string.
    """
    settings = get_all_settings(conn)
    api_key = settings.get("youtube_api_key", "")
    if not api_key:
        raise ValueError("YouTube API key not configured. Go to Settings.")

    youtube = build_youtube(api_key)
    quota = QuotaTracker()

    channel_ids = get_community_channel_ids(conn, community_id)
    max_videos = None
    raw = settings.get("max_videos_per_channel", "")
    if raw and raw != "null":
        max_videos = int(raw)
    after = settings.get("date_filter_after", "") or None
    max_comments = int(settings.get("max_comments_per_video", "500"))
    fetch_replies = settings.get("fetch_replies", "true").lower() == "true"

    for cid in channel_ids:
        try:
            collect_channel_comments(
                conn, youtube, cid,
                max_videos, after, max_comments, fetch_replies,
                quota, progress_callback,
            )
        except Exception as e:
            log.error(f"Failed on channel {cid}: {e}", exc_info=True)

    summary = quota.summary()
    log.info("\n" + summary)
    if progress_callback:
        progress_callback(f"quota\t{quota.total} / {quota.DAILY_FREE_QUOTA} units used")
    return summary
