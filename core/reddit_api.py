"""
Reddit API wrapper for the Community Analyzer.

Uses PRAW (Python Reddit API Wrapper) in read-only mode.
Subreddits map to channels, posts map to videos, comments map to comments.

ID prefix conventions to avoid collisions with YouTube IDs:
  subreddit channel_id  → 'r/subredditname'
  post video_id         → 'reddit_t3_<post_id>'
  comment comment_id    → 'reddit_t1_<comment_id>'
  author channel_id     → 'reddit_u/<username>'
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


def build_reddit(client_id: str, client_secret: str, user_agent: str):
    """Build a read-only PRAW Reddit instance."""
    try:
        import praw
    except ImportError:
        raise ImportError(
            "praw is required for Reddit support. Install it with: pip install praw"
        )
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
    )


def fetch_subreddit_info(reddit, name: str) -> dict | None:
    """
    Fetch metadata for a subreddit. Returns None if not found.
    Returns dict with: channel_id, channel_name, description, subscriber_count.
    """
    clean = name.lstrip("r/").strip()
    try:
        sub = reddit.subreddit(clean)
        # Accessing display_name triggers a network call and raises if not found
        _ = sub.display_name
        return {
            "channel_id": f"r/{sub.display_name}",
            "channel_name": sub.title or sub.display_name,
            "description": (sub.public_description or sub.description or "")[:500],
            "subscriber_count": sub.subscribers or 0,
            "handle": f"r/{sub.display_name}",
        }
    except Exception as e:
        log.warning(f"Could not fetch subreddit info for r/{clean}: {e}")
        return None


def fetch_subreddit_posts(
    reddit, name: str, sort: str = "hot",
    time_filter: str = "week", limit: int = 50,
) -> list[dict]:
    """
    Fetch posts from a subreddit.
    Returns list of dicts compatible with _upsert_video expectations.
    """
    clean = name.lstrip("r/").strip()
    try:
        sub = reddit.subreddit(clean)
        if sort == "hot":
            listing = sub.hot(limit=limit)
        elif sort == "new":
            listing = sub.new(limit=limit)
        elif sort == "top":
            listing = sub.top(time_filter=time_filter, limit=limit)
        elif sort == "rising":
            listing = sub.rising(limit=limit)
        else:
            listing = sub.hot(limit=limit)

        posts = []
        for post in listing:
            if post.stickied:
                continue
            posts.append({
                "video_id": f"reddit_t3_{post.id}",
                "title": post.title or "(untitled)",
                "published_at": _ts_to_iso(post.created_utc),
                "comment_count": post.num_comments,
                # Store the post URL + selftext as "description" for context
                "description": _post_description(post),
                "_praw_post": post,  # kept for comment fetching, not stored in DB
            })
        return posts
    except Exception as e:
        log.error(f"Failed to fetch posts from r/{clean}: {e}")
        return []


def fetch_post_comments(post, max_comments: int = 500) -> list[dict]:
    """
    Fetch and flatten comments for a single Reddit post.
    Replaces MoreComments up to a reasonable depth.
    Returns list of dicts compatible with _insert_comments expectations.
    """
    try:
        import praw.models
        post.comments.replace_more(limit=3)
        flat = post.comments.list()
    except Exception as e:
        log.warning(f"Failed to expand comments for {post.id}: {e}")
        try:
            flat = list(post.comments)
        except Exception:
            return []

    results = []
    for c in flat[:max_comments]:
        try:
            if not hasattr(c, "body") or c.body in ("[deleted]", "[removed]"):
                continue
            author = c.author.name if c.author else "[deleted]"
            parent_full = c.parent_id or ""
            # parent_id is e.g. "t1_xxxx" (comment) or "t3_xxxx" (post)
            is_reply = parent_full.startswith("t1_")
            parent_comment_id = (
                f"reddit_{parent_full}" if is_reply else None
            )
            results.append({
                "comment_id": f"reddit_t1_{c.id}",
                "author_name": author,
                "author_channel_id": f"reddit_u/{author}",
                "text": c.body[:2000],  # cap at 2000 chars
                "like_count": max(0, c.score),  # score can be negative
                "published_at": _ts_to_iso(c.created_utc),
                "is_reply": 1 if is_reply else 0,
                "parent_id": parent_comment_id,
            })
        except Exception as e:
            log.debug(f"Skipping comment: {e}")
            continue

    return results


def search_subreddits(reddit, query: str, limit: int = 10) -> list[dict]:
    """Search for subreddits matching a query. Returns list of info dicts."""
    results = []
    try:
        for sub in reddit.subreddits.search(query, limit=limit):
            results.append({
                "source_type": "reddit",
                "source_id": f"r/{sub.display_name}",
                "display_name": sub.title or sub.display_name,
                "description": (sub.public_description or "")[:300],
                "member_count": sub.subscribers or 0,
                "origin": "api_search",
            })
    except Exception as e:
        log.warning(f"Reddit subreddit search failed for {query!r}: {e}")
    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts_to_iso(ts: float) -> str:
    """Convert Unix timestamp to ISO-8601 string."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_description(post) -> str:
    """Build a short description from post selftext or URL."""
    if post.is_self and post.selftext:
        return post.selftext[:500]
    return post.url or ""


# ── RedditCollector ───────────────────────────────────────────────────────────

from .source_collector import CollectResult, register_collector
from .gossip_collect import _upsert_channel, _upsert_video, _insert_comments


class RedditCollector:
    """SourceCollector implementation for Reddit subreddits."""

    source_type = "reddit"

    def collect(self, conn, source_id: str, settings: dict[str, str],
                progress_callback=None) -> CollectResult:
        client_id = settings.get("reddit_client_id", "")
        client_secret = settings.get("reddit_client_secret", "")
        if not client_id or not client_secret:
            raise ValueError(
                "Reddit client_id and client_secret not configured. Go to Settings."
            )

        user_agent = settings.get("reddit_user_agent", "CommunityAnalyzer/1.0")
        sort = settings.get("reddit_post_sort", "hot")
        time_filter = settings.get("reddit_time_filter", "week")
        max_posts = int(settings.get("max_posts_per_subreddit", "50"))
        max_comments = int(settings.get("max_comments_per_video", "500"))

        # source_id is like 'r/homelab'
        subreddit_name = source_id.lstrip("r/")

        reddit = build_reddit(client_id, client_secret, user_agent)

        # Upsert subreddit as a channel
        info = fetch_subreddit_info(reddit, subreddit_name)
        if not info:
            log.warning(f"Could not fetch subreddit info for {source_id}")
            channel_name = source_id
        else:
            channel_name = info["channel_name"]
            conn.execute(
                """INSERT OR IGNORE INTO channels
                       (channel_id, channel_name, handle, description)
                   VALUES (?, ?, ?, ?)""",
                (source_id, channel_name,
                 info.get("handle", source_id),
                 info.get("description", "")),
            )
            conn.commit()

        posts = fetch_subreddit_posts(
            reddit, subreddit_name, sort=sort,
            time_filter=time_filter, limit=max_posts,
        )
        log.info(f"=== {source_id} === {len(posts)} posts")

        if progress_callback:
            progress_callback(f"channel\t{channel_name}\t{len(posts)} posts")

        total_new = 0
        for i, post in enumerate(posts, 1):
            vid_id = post["video_id"]
            _upsert_video(conn, post, source_id, source_type="reddit")

            praw_post = post.get("_praw_post")
            if praw_post is None:
                continue

            comments = fetch_post_comments(praw_post, max_comments)
            new_count = _insert_comments(
                conn, comments, vid_id, source_id, "reddit"
            )
            total_new += new_count
            log.info(f"  [{i}/{len(posts)}] {post['title'][:60]}: "
                     f"{len(comments)} fetched, {new_count} new")

            if progress_callback:
                status = "new" if new_count > 0 else "skip"
                progress_callback(
                    f"video\t{source_id}\t{i}/{len(posts)}\t{post['title'][:60]}"
                    f"\t{len(comments)} fetched\t{new_count} new\t{status}"
                )
            time.sleep(0.5)  # be polite to Reddit API

        if progress_callback:
            progress_callback(f"done\t{channel_name}\t{total_new} new comments")

        return CollectResult(new_comments=total_new)


# Register at import time
register_collector(RedditCollector())
