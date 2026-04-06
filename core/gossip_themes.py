"""
Gossip theme tracker — clusters gossip_items into persistent named themes
with activity timelines.

Three phases:
  A. Rule-based clustering by (gossip_type, frozenset(canonical_subjects))
  B. Activity timeline computed from evidence comment dates
  C. Optional LLM pass to generate human-readable titles and descriptions
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

from .db import get_all_settings, get_community_channel_ids
from .entity_resolver import EntityResolver

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "cluster_themes.txt"

_GOSSIP_TYPE_COLORS = {
    "drama":        "#e74c3c",
    "relationship": "#9b59b6",
    "collaboration":"#3498db",
    "reputation":   "#f39c12",
    "irl_vs_persona":"#1abc9c",
    "trend":        "#2ecc71",
}
DEFAULT_TYPE_COLOR = "#7c4dff"


def _safe_json(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------

def sparkline_svg(activity: dict, width: int = 100, height: int = 28) -> str:
    """Mini inline SVG bar sparkline for a theme card."""
    if not activity:
        return f'<svg width="{width}" height="{height}"></svg>'
    months = sorted(activity.keys())[-12:]
    values = [activity.get(m, 0) for m in months]
    max_v = max(values) or 1
    n = len(values)
    if n == 0:
        return f'<svg width="{width}" height="{height}"></svg>'
    bar_w = width / n
    bars = []
    for i, v in enumerate(values):
        h = max(2, int(v / max_v * (height - 2)))
        x = i * bar_w
        y = height - h
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(1, bar_w - 1):.1f}" '
            f'height="{h}" fill="#7c4dff" opacity="0.75" rx="1"/>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="display:block;overflow:visible">'
        + "".join(bars)
        + "</svg>"
    )


def activity_chart_svg(activity: dict, title: str = "", width: int = 700) -> str:
    """Full monthly bar chart for the theme detail page."""
    if not activity:
        return ""
    months = sorted(activity.keys())
    if not months:
        return ""
    values = [activity[m] for m in months]
    max_v = max(values) or 1

    ml, mr, mt, mb = 50, 20, 40, 50
    cw = width - ml - mr
    n = len(months)
    bar_w = cw / n
    ch = 160
    height = mt + ch + mb

    def xc(i):
        return ml + i * bar_w + bar_w / 2

    def bar_h(v):
        return max(2, int(v / max_v * ch))

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="background:#1a1a2e;border-radius:8px;font-family:\'Segoe UI\',sans-serif">',
    ]
    if title:
        out.append(
            f'<text x="{width//2}" y="26" text-anchor="middle" fill="#c0b0ff" '
            f'font-size="13" font-weight="bold">{title[:60]}</text>'
        )
    # Baseline
    out.append(
        f'<line x1="{ml}" y1="{mt+ch}" x2="{ml+cw}" y2="{mt+ch}" '
        f'stroke="#333" stroke-width="1"/>'
    )
    for i, (m, v) in enumerate(zip(months, values)):
        h = bar_h(v)
        x = ml + i * bar_w + 1
        y = mt + ch - h
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(2, bar_w-2):.1f}" '
            f'height="{h}" fill="#7c4dff" opacity="0.8" rx="2"/>'
        )
        if v > 0:
            out.append(
                f'<text x="{xc(i):.1f}" y="{y-4:.1f}" text-anchor="middle" '
                f'fill="#c0b0ff" font-size="9">{v}</text>'
            )
        # Month label (every other one if dense)
        if n <= 18 or i % 2 == 0:
            label = m[5:] if len(m) >= 7 else m  # show MM only
            out.append(
                f'<text x="{xc(i):.1f}" y="{mt+ch+16}" text-anchor="middle" '
                f'fill="#888" font-size="9" '
                f'transform="rotate(-35,{xc(i):.1f},{mt+ch+16})">{label}</text>'
            )
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Phase A + B: rule-based clustering and timeline
# ---------------------------------------------------------------------------

def _cluster_items(conn, channel_ids: list[str],
                   resolver: EntityResolver) -> list[dict]:
    """Load gossip_items and group them by (gossip_type, sorted_subjects)."""
    placeholders = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        f"""SELECT gi.id, gi.video_id, gi.channel_id, gi.gossip_type,
                   gi.subjects, gi.claim, gi.evidence_comment_ids
            FROM gossip_items gi
            WHERE gi.channel_id IN ({placeholders})""",
        channel_ids,
    ).fetchall()

    clusters: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        subjects = resolver.resolve_list(_safe_json(row["subjects"], []))
        key = "{0}::{1}".format(
            row["gossip_type"] or "",
            "||".join(sorted(s.lower() for s in subjects)),
        )
        clusters[key].append({
            "id": row["id"],
            "gossip_type": row["gossip_type"] or "unknown",
            "subjects": subjects,
            "claim": row["claim"] or "",
            "evidence_comment_ids": _safe_json(row["evidence_comment_ids"], []),
        })
    return list(clusters.values())


def _build_activity(conn, items: list[dict]) -> dict:
    """Return {YYYY-MM: count} from evidence comment published_at dates."""
    all_ids: list[str] = []
    for item in items:
        all_ids.extend(item["evidence_comment_ids"])
    all_ids = list(set(all_ids))
    if not all_ids:
        return {}

    activity: dict[str, int] = defaultdict(int)
    batch = 900  # stay well under SQLite variable limit
    for i in range(0, len(all_ids), batch):
        chunk = all_ids[i:i + batch]
        ph = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT published_at FROM comments WHERE comment_id IN ({ph}) "
            f"AND published_at IS NOT NULL",
            chunk,
        ).fetchall():
            month = (row["published_at"] or "")[:7]
            if month:
                activity[month] += 1
    return dict(sorted(activity.items()))


def _auto_title(gossip_type: str, subjects: list[str]) -> str:
    if subjects:
        names = " & ".join(subjects[:3])
        return f"{names} — {gossip_type}"
    return gossip_type


# ---------------------------------------------------------------------------
# Phase C: LLM enhancement
# ---------------------------------------------------------------------------

def _llm_enhance(client, candidates: list[dict]) -> list[dict]:
    """Generate human-readable titles and descriptions via LLM (batched)."""
    with open(PROMPT_PATH, encoding="utf-8") as f:
        system = f.read()

    BATCH = 20
    for start in range(0, len(candidates), BATCH):
        batch = candidates[start:start + BATCH]
        lines = []
        for j, t in enumerate(batch):
            claims = [c for c in t.get("claims", []) if c][:5]
            lines += [
                f"CLUSTER {j}:",
                f"  type: {t['gossip_type']}",
                f"  subjects: {json.dumps(t['subjects'])}",
                f"  sample_claims: {json.dumps(claims)}",
                f"  first_seen: {t['first_seen_at'] or 'unknown'}",
                f"  last_seen: {t['last_seen_at'] or 'unknown'}",
                f"  evidence_count: {t['total_evidence']}",
                "",
            ]
        try:
            result = client.complete_json(system, "\n".join(lines))
            if isinstance(result, list):
                for item in result:
                    idx = item.get("cluster_id")
                    if idx is not None and 0 <= idx < len(batch):
                        if item.get("title"):
                            batch[idx]["title"] = item["title"]
                        if item.get("description"):
                            batch[idx]["description"] = item["description"]
        except Exception as e:
            log.warning(f"LLM theme batch {start // BATCH} failed: {e}")
    return candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_themes(conn, community_id: int, use_llm: bool = False,
                   progress_callback=None) -> int:
    """
    Cluster gossip_items into themes and persist to the themes table.
    Replaces all existing themes for this community.
    Returns the number of themes written.
    """
    def _cb(msg: str):
        if progress_callback:
            progress_callback(msg)
        log.info(msg)

    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        return 0

    settings = get_all_settings(conn)
    resolver = EntityResolver(_safe_json(settings.get("entity_aliases", "{}"), {}))

    _cb("Clustering gossip items...")
    raw_clusters = _cluster_items(conn, channel_ids, resolver)
    if not raw_clusters:
        conn.execute("DELETE FROM themes WHERE community_id = ?", (community_id,))
        conn.commit()
        return 0

    _cb(f"Computing activity timelines for {len(raw_clusters)} clusters...")
    candidates: list[dict] = []
    for items in raw_clusters:
        gossip_type = items[0]["gossip_type"]
        subjects = items[0]["subjects"]
        gossip_item_ids = [i["id"] for i in items]
        claims = [i["claim"] for i in items if i.get("claim")]
        activity = _build_activity(conn, items)

        dates = list(activity.keys())
        first_seen = (sorted(dates)[0] + "-01") if dates else None
        last_seen = (sorted(dates)[-1] + "-01") if dates else None

        candidates.append({
            "gossip_type": gossip_type,
            "subjects": subjects,
            "gossip_item_ids": gossip_item_ids,
            "claims": claims,
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
            "activity_json": activity,
            "total_evidence": sum(activity.values()),
            "title": _auto_title(gossip_type, subjects),
            "description": None,
        })

    # Sort by recency first, then evidence count (both descending)
    candidates.sort(key=lambda x: (x["last_seen_at"] or "", x["total_evidence"]),
                    reverse=True)

    llm_backend = None
    if use_llm and candidates:
        _cb("Running LLM pass to generate titles and descriptions...")
        try:
            from .llm_client import LLMClient, _settings_to_llm_config
            cfg = _settings_to_llm_config(settings)
            client = LLMClient(cfg, role="analyze")
            llm_backend = client.backend
            candidates = _llm_enhance(client, candidates)
        except Exception as e:
            log.warning(f"LLM theme enhancement skipped: {e}")

    _cb(f"Saving {len(candidates)} themes...")
    conn.execute("DELETE FROM themes WHERE community_id = ?", (community_id,))
    for t in candidates:
        conn.execute(
            """INSERT INTO themes
               (community_id, title, description, gossip_type, subjects,
                gossip_item_ids, first_seen_at, last_seen_at, activity_json,
                total_evidence, created_at, llm_backend)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
            (
                community_id,
                t["title"], t.get("description"),
                t["gossip_type"],
                json.dumps(t["subjects"]),
                json.dumps(t["gossip_item_ids"]),
                t["first_seen_at"], t["last_seen_at"],
                json.dumps(t["activity_json"]),
                t["total_evidence"],
                llm_backend,
            ),
        )
    conn.commit()
    _cb(f"Done — {len(candidates)} themes saved.")
    return len(candidates)


def get_theme_detail(conn, theme_id: int) -> dict | None:
    """
    Load a theme with its full evidence: gossip items and comments.
    Returns a rich dict ready for template rendering.
    """
    row = conn.execute("SELECT * FROM themes WHERE id = ?", (theme_id,)).fetchone()
    if not row:
        return None

    theme = dict(row)
    theme["subjects"] = _safe_json(theme["subjects"], [])
    theme["activity"] = _safe_json(theme["activity_json"], {})
    gossip_item_ids = _safe_json(theme["gossip_item_ids"], [])

    # Load gossip items
    gossip_items: list[dict] = []
    channels_involved: set[str] = set()
    all_comment_ids: list[str] = []

    if gossip_item_ids:
        ph = ",".join("?" * len(gossip_item_ids))
        for gi in conn.execute(
            f"""SELECT gi.*, v.title AS video_title,
                       v.published_at AS video_published_at,
                       c.channel_name, c.handle
                FROM gossip_items gi
                JOIN videos v ON gi.video_id = v.video_id
                JOIN channels c ON gi.channel_id = c.channel_id
                WHERE gi.id IN ({ph})
                ORDER BY v.published_at ASC""",
            gossip_item_ids,
        ).fetchall():
            d = dict(gi)
            d["evidence_ids"] = _safe_json(d.get("evidence_comment_ids"), [])
            d["subjects_list"] = _safe_json(d.get("subjects"), [])
            channels_involved.add(d["channel_name"])
            all_comment_ids.extend(d["evidence_ids"])
            gossip_items.append(d)

    # Load evidence comments
    all_comment_ids = list(set(all_comment_ids))
    comments_by_id: dict[str, dict] = {}
    if all_comment_ids:
        for i in range(0, len(all_comment_ids), 900):
            chunk = all_comment_ids[i:i + 900]
            ph2 = ",".join("?" * len(chunk))
            for c in conn.execute(
                f"""SELECT co.*, ch.channel_name AS ch_name
                    FROM comments co
                    LEFT JOIN channels ch ON co.channel_id = ch.channel_id
                    WHERE co.comment_id IN ({ph2})""",
                chunk,
            ).fetchall():
                comments_by_id[c["comment_id"]] = dict(c)

    all_comments = sorted(
        comments_by_id.values(),
        key=lambda x: x.get("published_at") or "",
    )

    # Group gossip items by month (for full timeline)
    timeline: dict[str, list[dict]] = defaultdict(list)
    for gi in gossip_items:
        month = (gi.get("video_published_at") or "")[:7]
        if month:
            timeline[month].append(gi)

    theme["gossip_items"] = gossip_items
    theme["comments_by_id"] = comments_by_id
    theme["origin_comments"] = all_comments[:5]
    theme["recent_comments"] = list(reversed(all_comments))[:10]
    theme["all_comments_count"] = len(all_comment_ids)
    theme["channels_involved"] = sorted(channels_involved)
    theme["timeline"] = dict(sorted(timeline.items()))
    theme["type_color"] = _GOSSIP_TYPE_COLORS.get(
        theme.get("gossip_type") or "", DEFAULT_TYPE_COLOR
    )
    return theme
