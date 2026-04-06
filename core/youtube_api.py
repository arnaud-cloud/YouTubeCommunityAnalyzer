"""
Shared YouTube Data API v3 helpers.

Used by both the tracker (channel stats, video details) and the gossip
collector (comment fetching). All functions accept a pre-built ``youtube``
service object so callers control API key management.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)


# ── Service builder ───────────────────────────────────────────────────────────

def build_youtube(api_key: str):
    """Build a YouTube Data API v3 service object."""
    if not api_key:
        raise RuntimeError(
            "No YouTube API key configured. "
            "Go to Settings to add your YOUTUBE_API_KEY."
        )
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


# ── Channel resolution ────────────────────────────────────────────────────────

def resolve_channel_id(youtube, identifier: str) -> dict | None:
    """
    Resolve a channel identifier to {id, title, handle}.

    Accepts:
      - UC... channel ID
      - @Handle
      - Plain text (falls back to search)

    Returns None if not found.
    """
    identifier = identifier.strip()

    # Direct channel ID
    if identifier.startswith("UC"):
        resp = youtube.channels().list(part="snippet", id=identifier).execute()
        items = resp.get("items", [])
        if items:
            s = items[0]["snippet"]
            return {"id": identifier, "title": s["title"],
                    "handle": s.get("customUrl", "")}

    # Handle (@name or name)
    handle = identifier.lstrip("@")
    resp = youtube.channels().list(part="snippet", forHandle=handle).execute()
    items = resp.get("items", [])
    if items:
        ch = items[0]
        s = ch["snippet"]
        return {"id": ch["id"], "title": s["title"],
                "handle": s.get("customUrl", handle)}

    # Fallback: search
    resp = youtube.search().list(
        part="snippet", q=identifier, type="channel", maxResults=1
    ).execute()
    items = resp.get("items", [])
    if items:
        cid = items[0]["snippet"]["channelId"]
        return resolve_channel_id(youtube, cid)

    return None


# ── Channel statistics ────────────────────────────────────────────────────────

def fetch_channel_stats(youtube, channel_id: str) -> dict:
    """Fetch channel-level statistics, snippet, branding, topic details."""
    resp = youtube.channels().list(
        part="snippet,statistics,brandingSettings,contentDetails,status,topicDetails",
        id=channel_id,
    ).execute()
    items = resp.get("items", [])
    if not items:
        return {}
    ch = items[0]
    stats = ch.get("statistics", {})
    snip = ch.get("snippet", {})
    brand = ch.get("brandingSettings", {}).get("channel", {})
    topics = ch.get("topicDetails", {}).get("topicCategories", [])
    return {
        "channel_id":        ch["id"],
        "title":             snip.get("title", ""),
        "description":       snip.get("description", ""),
        "custom_url":        snip.get("customUrl", ""),
        "country":           snip.get("country", ""),
        "published_at":      snip.get("publishedAt", ""),
        "thumbnail_default": snip.get("thumbnails", {}).get("default", {}).get("url", ""),
        "thumbnail_medium":  snip.get("thumbnails", {}).get("medium", {}).get("url", ""),
        "keywords":          brand.get("keywords", ""),
        "topic_categories":  topics,
        "subscriber_count":  int(stats.get("subscriberCount", 0)),
        "video_count":       int(stats.get("videoCount", 0)),
        "view_count":        int(stats.get("viewCount", 0)),
        "hidden_subscriber": stats.get("hiddenSubscriberCount", False),
        "uploads_playlist":  ch.get("contentDetails", {})
                               .get("relatedPlaylists", {})
                               .get("uploads", ""),
    }


# ── Video listing & details ──────────────────────────────────────────────────

def fetch_all_video_ids(youtube, uploads_playlist_id: str) -> list[str]:
    """Page through the uploads playlist to get all video IDs."""
    ids = []
    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            ids.append(item["contentDetails"]["videoId"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_recent_video_ids(youtube, uploads_playlist_id: str,
                           max_results: int = 50) -> list[str]:
    """Fetch the most recent video IDs from the uploads playlist (one page)."""
    resp = youtube.playlistItems().list(
        part="contentDetails",
        playlistId=uploads_playlist_id,
        maxResults=max_results,
    ).execute()
    return [item["contentDetails"]["videoId"] for item in resp.get("items", [])]


def fetch_video_details(youtube, video_ids: list[str]) -> list[dict]:
    """Fetch full metadata for videos in batches of 50."""
    results = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        resp = youtube.videos().list(
            part="snippet,statistics,contentDetails,status,topicDetails",
            id=",".join(chunk),
        ).execute()
        for v in resp.get("items", []):
            snip = v.get("snippet", {})
            stats = v.get("statistics", {})
            detail = v.get("contentDetails", {})
            status = v.get("status", {})
            topics = v.get("topicDetails", {}).get("topicCategories", [])
            results.append({
                "video_id":        v["id"],
                "channel_id":      snip.get("channelId", ""),
                "title":           snip.get("title", ""),
                "description":     snip.get("description", "")[:1000],
                "published_at":    snip.get("publishedAt", ""),
                "tags":            snip.get("tags", []),
                "category_id":     snip.get("categoryId", ""),
                "duration":        detail.get("duration", "PT0S"),
                "definition":      detail.get("definition", ""),
                "has_captions":    detail.get("caption", "false") == "true",
                "topic_categories": topics,
                "privacy_status":  status.get("privacyStatus", ""),
                "view_count":      int(stats.get("viewCount", 0)),
                "like_count":      int(stats.get("likeCount", 0)),
                "comment_count":   int(stats.get("commentCount", 0)),
                "thumbnail_url":   snip.get("thumbnails", {})
                                       .get("medium", {}).get("url", ""),
            })
    return results


# ── Comment fetching (for gossip collector) ──────────────────────────────────

class QuotaTracker:
    """Counts YouTube API units consumed during a run."""
    COSTS = {
        "playlistItems.list": 1,
        "videos.list": 1,
        "commentThreads.list": 1,
        "comments.list": 1,
        "channels.list": 1,
        "search.list": 100,
    }
    DAILY_FREE_QUOTA = 10_000

    def __init__(self):
        self._counts: dict[str, int] = {}

    def charge(self, method: str, calls: int = 1):
        self._counts[method] = self._counts.get(method, 0) + calls

    @property
    def total(self) -> int:
        return sum(
            self.COSTS.get(m, 1) * n for m, n in self._counts.items()
        )

    def summary(self) -> str:
        lines = ["-- Quota usage summary --"]
        for method, calls in sorted(self._counts.items()):
            cost_each = self.COSTS.get(method, 1)
            subtotal = cost_each * calls
            lines.append(
                f"  {method:<28} {calls:>4} calls x {cost_each:>3} = {subtotal:>5} units"
            )
        lines.append(f"  TOTAL: {self.total} / {self.DAILY_FREE_QUOTA}")
        return "\n".join(lines)


def is_quota_exceeded(error: HttpError) -> bool:
    """Return True if the error is a YouTube quota exceeded error."""
    if error.resp.status != 403:
        return False
    try:
        content = json.loads(error.content)
        for err in content.get("error", {}).get("errors", []):
            if err.get("reason") in ("quotaExceeded", "dailyLimitExceeded"):
                return True
    except Exception:
        pass
    return False


def fetch_channels_subscriber_counts(youtube, channel_ids: list[str]) -> dict[str, int]:
    """
    Batch-fetch subscriber counts for multiple channel IDs.
    Costs 1 API unit per 50 channels. Returns {channel_id: subscriber_count}.
    """
    counts: dict[str, int] = {}
    for i in range(0, len(channel_ids), 50):
        chunk = channel_ids[i:i + 50]
        resp = youtube.channels().list(
            part="statistics",
            id=",".join(chunk),
            maxResults=50,
        ).execute()
        for item in resp.get("items", []):
            cid = item["id"]
            counts[cid] = int(item.get("statistics", {}).get("subscriberCount", 0))
    return counts


def channel_id_to_uploads_playlist(channel_id: str) -> str:
    """Derive the uploads playlist ID from a channel ID (free, no API call)."""
    if channel_id.startswith("UC"):
        return "UU" + channel_id[2:]
    return channel_id


def fetch_channel_videos_for_gossip(
    youtube, channel_id: str,
    max_videos: int | None,
    after: str | None,
    quota: QuotaTracker,
) -> list[dict]:
    """
    Fetch video list via playlistItems.list for gossip collection.
    Enriches with comment counts via videos.list.
    """
    playlist_id = channel_id_to_uploads_playlist(channel_id)
    videos: list[dict] = []
    page_token = None
    after_dt = datetime.fromisoformat(after) if after else None

    while True:
        resp = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        quota.charge("playlistItems.list")

        for item in resp.get("items", []):
            snippet = item["snippet"]
            vid_id = snippet.get("resourceId", {}).get("videoId")
            if not vid_id:
                continue
            published_raw = snippet.get("publishedAt", "")
            if after_dt and published_raw:
                pub_dt = datetime.fromisoformat(
                    published_raw.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                if pub_dt < after_dt:
                    return _enrich_with_comment_counts(youtube, videos, quota)
            videos.append({
                "video_id": vid_id,
                "title": snippet.get("title", ""),
                "published_at": published_raw,
            })
            if max_videos and len(videos) >= max_videos:
                return _enrich_with_comment_counts(youtube, videos, quota)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return _enrich_with_comment_counts(youtube, videos, quota)


def _enrich_with_comment_counts(
    youtube, videos: list[dict], quota: QuotaTracker
) -> list[dict]:
    """Fetch comment counts via videos.list in batches of 50."""
    if not videos:
        return videos
    for i in range(0, len(videos), 50):
        batch = videos[i:i + 50]
        ids = ",".join(v["video_id"] for v in batch)
        try:
            resp = youtube.videos().list(part="statistics", id=ids).execute()
            quota.charge("videos.list")
            counts = {
                item["id"]: int(item["statistics"].get("commentCount", 0))
                for item in resp.get("items", [])
            }
            for v in batch:
                v["comment_count"] = counts.get(v["video_id"], 0)
        except HttpError as e:
            log.warning(f"Failed to fetch stats for batch: {e}")
            for v in batch:
                v["comment_count"] = 0
    return videos


def _parse_reply(reply: dict, parent_id: str) -> dict:
    rs = reply["snippet"]
    return {
        "comment_id":        reply["id"],
        "author_name":       rs.get("authorDisplayName", ""),
        "author_channel_id": rs.get("authorChannelId", {}).get("value", ""),
        "text":              rs.get("textDisplay", ""),
        "like_count":        rs.get("likeCount", 0),
        "published_at":      rs.get("publishedAt", ""),
        "is_reply":          1,
        "parent_id":         parent_id,
    }


def fetch_remaining_replies(
    youtube, thread_id: str, quota: QuotaTracker
) -> list[dict]:
    """Fetch replies beyond the 5 inline ones via comments.list."""
    replies = []
    page_token = None
    try:
        while True:
            resp = youtube.comments().list(
                part="snippet",
                parentId=thread_id,
                maxResults=100,
                pageToken=page_token,
                textFormat="plainText",
            ).execute()
            quota.charge("comments.list")
            for item in resp.get("items", []):
                replies.append(_parse_reply(item, thread_id))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        log.warning(f"Could not fetch extra replies for thread {thread_id}: {e}")
    return replies


def fetch_comments_for_video(
    youtube, video_id: str,
    max_comments: int,
    fetch_replies: bool,
    quota: QuotaTracker,
) -> list[dict]:
    """Return flat list of comment dicts (top-level + replies)."""
    comments: list[dict] = []
    page_token: str | None = None

    try:
        while len(comments) < max_comments:
            resp = youtube.commentThreads().list(
                part="snippet,replies",
                videoId=video_id,
                maxResults=100,
                pageToken=page_token,
                textFormat="plainText",
            ).execute()
            quota.charge("commentThreads.list")

            for thread in resp.get("items", []):
                thread_id = thread["snippet"]["topLevelComment"]["id"]
                top = thread["snippet"]["topLevelComment"]["snippet"]
                reply_count = thread["snippet"].get("totalReplyCount", 0)

                comments.append({
                    "comment_id":        thread_id,
                    "author_name":       top.get("authorDisplayName", ""),
                    "author_channel_id": top.get("authorChannelId", {}).get("value", ""),
                    "text":              top.get("textDisplay", ""),
                    "like_count":        top.get("likeCount", 0),
                    "published_at":      top.get("publishedAt", ""),
                    "is_reply":          0,
                    "parent_id":         None,
                })

                if not fetch_replies or reply_count == 0:
                    continue

                if reply_count <= 5:
                    for reply in thread.get("replies", {}).get("comments", []):
                        comments.append(_parse_reply(reply, thread_id))
                else:
                    comments.extend(
                        fetch_remaining_replies(youtube, thread_id, quota)
                    )

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    except HttpError as e:
        if e.resp.status == 403:
            log.warning(f"Comments disabled for video {video_id}")
        else:
            log.error(f"HTTP error fetching comments for {video_id}: {e}")

    return comments[:max_comments]
