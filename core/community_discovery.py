"""
Community discovery — find relevant sources for a set of keywords.

Flow:
  1. Search YouTube channels via YouTube Data API (search.list)
  2. Search Reddit subreddits via PRAW
  3. Ask LLM to suggest channels/subreddits from keywords
  4. Verify LLM suggestions via respective APIs
  5. Deduplicate + rank by relevance
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class DiscoveryResult:
    source_type: str         # 'youtube' | 'reddit'
    source_id: str           # channel_id or 'r/name'
    display_name: str
    description: str = ""
    member_count: int = 0    # subscribers or subreddit members
    relevance: float = 0.5   # 0-1
    origin: str = "api_search"  # 'api_search' | 'llm_verified' | 'llm_unverified'


def discover_sources(
    keywords: str,
    settings: dict[str, str],
    progress_callback=None,
) -> list[DiscoveryResult]:
    """
    Search for community sources matching the given keywords.
    Returns a deduplicated, ranked list of DiscoveryResult objects.
    """
    results: list[DiscoveryResult] = []
    seen: set[str] = set()  # set of (source_type, source_id) keys

    def _key(r: DiscoveryResult) -> str:
        return f"{r.source_type}:{r.source_id.lower()}"

    def _add(r: DiscoveryResult):
        k = _key(r)
        if k not in seen:
            seen.add(k)
            results.append(r)

    # 1. YouTube channel search
    if settings.get("youtube_api_key"):
        if progress_callback:
            progress_callback("info\tSearching YouTube channels...")
        for r in _search_youtube(keywords, settings):
            _add(r)

    # 2. Reddit subreddit search
    if settings.get("reddit_client_id") and settings.get("reddit_client_secret"):
        if progress_callback:
            progress_callback("info\tSearching Reddit subreddits...")
        for r in _search_reddit(keywords, settings):
            _add(r)

    # 3. LLM suggestions
    if progress_callback:
        progress_callback("info\tAsking LLM for suggestions...")
    llm_suggestions = _llm_suggest(keywords, settings)

    # 4. Verify LLM suggestions
    for suggestion in llm_suggestions:
        platform = suggestion.get("platform", "")
        identifier = suggestion.get("identifier", "")
        reason = suggestion.get("reason", "")
        if not platform or not identifier:
            continue
        if platform == "youtube":
            verified = _verify_youtube(identifier, settings)
            if verified:
                verified.origin = "llm_verified"
                verified.description = reason
                verified.relevance = 0.6
                _add(verified)
            else:
                # Still add as unverified hint if identifier looks plausible
                source_id = identifier if identifier.startswith("UC") else identifier
                _add(DiscoveryResult(
                    source_type="youtube",
                    source_id=source_id,
                    display_name=identifier,
                    description=reason,
                    relevance=0.3,
                    origin="llm_unverified",
                ))
        elif platform == "reddit":
            clean = identifier.lstrip("r/").strip()
            source_id = f"r/{clean}"
            verified = _verify_reddit(clean, settings)
            if verified:
                verified.origin = "llm_verified"
                verified.description = reason
                verified.relevance = 0.6
                _add(verified)
            else:
                _add(DiscoveryResult(
                    source_type="reddit",
                    source_id=source_id,
                    display_name=source_id,
                    description=reason,
                    relevance=0.3,
                    origin="llm_unverified",
                ))

    # 5. Sort: api_search first, then llm_verified, then unverified; within each by member_count
    priority = {"api_search": 0, "llm_verified": 1, "llm_unverified": 2}
    results.sort(key=lambda r: (priority.get(r.origin, 3), -r.member_count))
    return results


# ── YouTube ───────────────────────────────────────────────────────────────────

def _search_youtube(keywords: str, settings: dict) -> list[DiscoveryResult]:
    results = []
    try:
        from .youtube_api import build_youtube
        youtube = build_youtube(settings["youtube_api_key"])
        resp = youtube.search().list(
            part="snippet",
            q=keywords,
            type="channel",
            maxResults=10,
        ).execute()
        for item in resp.get("items", []):
            snippet = item.get("snippet", {})
            channel_id = item.get("id", {}).get("channelId", "")
            if not channel_id:
                continue
            results.append(DiscoveryResult(
                source_type="youtube",
                source_id=channel_id,
                display_name=snippet.get("title", channel_id),
                description=snippet.get("description", "")[:300],
                relevance=0.8,
                origin="api_search",
            ))
    except Exception as e:
        log.warning(f"YouTube search failed: {e}")
    return results


def _verify_youtube(identifier: str, settings: dict) -> DiscoveryResult | None:
    if not settings.get("youtube_api_key"):
        return None
    try:
        from .youtube_api import build_youtube, resolve_channel_id
        youtube = build_youtube(settings["youtube_api_key"])
        info = resolve_channel_id(youtube, identifier)
        if not info:
            return None
        return DiscoveryResult(
            source_type="youtube",
            source_id=info["id"],
            display_name=info["title"],
            relevance=0.7,
            origin="llm_verified",
        )
    except Exception as e:
        log.debug(f"YouTube verify failed for {identifier!r}: {e}")
        return None


# ── Reddit ────────────────────────────────────────────────────────────────────

def _search_reddit(keywords: str, settings: dict) -> list[DiscoveryResult]:
    results = []
    try:
        from .reddit_api import build_reddit, search_subreddits
        reddit = build_reddit(
            settings["reddit_client_id"],
            settings["reddit_client_secret"],
            settings.get("reddit_user_agent", "CommunityAnalyzer/1.0"),
        )
        raw = search_subreddits(reddit, keywords, limit=10)
        for r in raw:
            results.append(DiscoveryResult(
                source_type="reddit",
                source_id=r["source_id"],
                display_name=r["display_name"],
                description=r["description"],
                member_count=r["member_count"],
                relevance=0.8,
                origin="api_search",
            ))
    except Exception as e:
        log.warning(f"Reddit search failed: {e}")
    return results


def _verify_reddit(subreddit_name: str, settings: dict) -> DiscoveryResult | None:
    if not settings.get("reddit_client_id"):
        return None
    try:
        from .reddit_api import build_reddit, fetch_subreddit_info
        reddit = build_reddit(
            settings["reddit_client_id"],
            settings["reddit_client_secret"],
            settings.get("reddit_user_agent", "CommunityAnalyzer/1.0"),
        )
        info = fetch_subreddit_info(reddit, subreddit_name)
        if not info:
            return None
        return DiscoveryResult(
            source_type="reddit",
            source_id=info["channel_id"],
            display_name=info["channel_name"],
            description=info.get("description", ""),
            member_count=info.get("subscriber_count", 0),
            relevance=0.7,
            origin="llm_verified",
        )
    except Exception as e:
        log.debug(f"Reddit verify failed for r/{subreddit_name}: {e}")
        return None


# ── LLM suggestions ───────────────────────────────────────────────────────────

_DISCOVER_PROMPT_PATH = None


def _llm_suggest(keywords: str, settings: dict) -> list[dict]:
    """Ask LLM to suggest YouTube channels and Reddit subreddits for keywords."""
    from pathlib import Path
    global _DISCOVER_PROMPT_PATH
    if _DISCOVER_PROMPT_PATH is None:
        _DISCOVER_PROMPT_PATH = (
            Path(__file__).resolve().parent.parent / "prompts" / "discover_sources.txt"
        )

    if not _DISCOVER_PROMPT_PATH.exists():
        log.warning("discover_sources.txt prompt not found — skipping LLM suggestions")
        return []

    try:
        from .llm_client import LLMClient, _settings_to_llm_config
        cfg = _settings_to_llm_config(settings)
        llm = LLMClient(cfg, role="discovery")
        system_prompt = _DISCOVER_PROMPT_PATH.read_text(encoding="utf-8")
        result = llm.complete_json(
            system_prompt,
            f"Find communities for: {keywords}",
        )
        suggestions = result.get("suggestions", [])
        if isinstance(suggestions, list):
            return suggestions
    except Exception as e:
        log.warning(f"LLM suggestion failed: {e}")
    return []
