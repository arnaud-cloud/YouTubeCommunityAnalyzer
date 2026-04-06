"""
Executive summary generator — produces a concise ~2-page briefing from
the latest analysis, themes, and entity metrics for a community.

Uses the LLM to synthesise all available data into three sections:
developing stories, per-YouTuber status, and a top/worst-5 palmarès.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .db import get_all_settings, get_community_channel_ids
from .llm_client import LLMClient, _settings_to_llm_config

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "executive_summary.txt"


def _safe_json(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _esc(s) -> str:
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _gather_data(conn, community_id: int) -> dict:
    """Pull together all data the LLM needs for the executive summary."""
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        raise ValueError("Community has no channels.")

    community = dict(conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone())

    channel_names = {
        r["channel_id"]: r["channel_name"]
        for r in conn.execute("SELECT channel_id, channel_name FROM channels")
    }

    # Latest analysis
    analysis_row = conn.execute(
        "SELECT * FROM analysis_results WHERE community_id = ? ORDER BY id DESC LIMIT 1",
        (community_id,),
    ).fetchone()
    analysis = {}
    if analysis_row:
        analysis = _safe_json(analysis_row["analysis_json"], {})

    # Recent themes (top 20 by recency then evidence)
    themes = []
    for row in conn.execute(
        """SELECT title, description, gossip_type, subjects, activity_json,
                  first_seen_at, last_seen_at, total_evidence
           FROM themes WHERE community_id = ?
           ORDER BY last_seen_at DESC, total_evidence DESC
           LIMIT 20""",
        (community_id,),
    ).fetchall():
        t = dict(row)
        t["subjects"] = _safe_json(t["subjects"], [])
        t["activity"] = _safe_json(t.pop("activity_json"), {})
        themes.append(t)

    # Entity metrics from latest aggregation
    agg_row = conn.execute(
        "SELECT entity_metrics_json, date_range_start, date_range_end, "
        "total_videos, total_gossip_items "
        "FROM aggregation_results WHERE community_id = ? ORDER BY id DESC LIMIT 1",
        (community_id,),
    ).fetchone()
    entity_metrics = {}
    agg_meta = {}
    if agg_row:
        entity_metrics = _safe_json(agg_row["entity_metrics_json"], {})
        agg_meta = {
            "date_range_start": agg_row["date_range_start"],
            "date_range_end": agg_row["date_range_end"],
            "total_videos": agg_row["total_videos"],
            "total_gossip_items": agg_row["total_gossip_items"],
        }

    return {
        "community": community,
        "channel_ids": channel_ids,
        "channel_names": channel_names,
        "analysis": analysis,
        "themes": themes,
        "entity_metrics": entity_metrics,
        "agg_meta": agg_meta,
    }


def _build_llm_payload(data: dict) -> str:
    """Build the user prompt for the LLM from gathered data."""
    channel_display = [
        data["channel_names"].get(ch, ch) for ch in data["channel_ids"]
    ]

    # Top entities by mention count
    top_entities = sorted(
        data["entity_metrics"].values(),
        key=lambda x: -x.get("total_mentions", 0),
    )[:25]

    # Analysis highlights (reputation, drama, trends)
    a = data["analysis"]
    reputation = a.get("reputation_rankings", [])[:15]
    drama = a.get("top_drama_items", [])[:10]
    trends = a.get("community_trends", [])[:10]
    persona = a.get("persona_vs_reality", [])[:10]

    payload = {
        "community_name": data["community"]["name"],
        "channels": channel_display,
        "date_range": {
            "start": data["agg_meta"].get("date_range_start", "?"),
            "end": data["agg_meta"].get("date_range_end", "?"),
        },
        "total_videos": data["agg_meta"].get("total_videos", 0),
        "total_gossip_items": data["agg_meta"].get("total_gossip_items", 0),
        "entity_metrics": top_entities,
        "recent_themes": data["themes"],
        "reputation_rankings": reputation,
        "top_drama_items": drama,
        "community_trends": trends,
        "persona_vs_reality": persona,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """\
:root{--bg:#0f0f1a;--surface:#1a1a2e;--border:#2a2a4a;--text:#e8e8f0;
      --muted:#9090b0;--accent:#7c4dff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
     font-family:'Segoe UI',system-ui,sans-serif;line-height:1.6;padding:0 1rem}
.container{max-width:900px;margin:0 auto;padding:2rem 0 4rem}
h1{font-size:1.8rem;color:var(--accent);margin-bottom:.2rem}
h2{font-size:1.2rem;color:var(--accent);margin:2rem 0 .8rem;
   border-bottom:1px solid var(--border);padding-bottom:.3rem}
.meta{color:var(--muted);font-size:.85rem;margin-bottom:1.5rem}
.story{background:var(--surface);border:1px solid var(--border);border-radius:6px;
       padding:.8rem 1rem;margin-bottom:.6rem}
.story-title{font-weight:bold;color:#fff;font-size:.95rem}
.story-type{display:inline-block;font-size:.7rem;padding:.15em .5em;border-radius:3px;
            color:#fff;margin-left:.5rem;vertical-align:middle}
.story-body{color:var(--muted);font-size:.88rem;margin-top:.25rem}
.status-grid{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
@media(max-width:700px){.status-grid{grid-template-columns:1fr}}
.status-card{background:var(--surface);border:1px solid var(--border);border-radius:6px;
             padding:.7rem .9rem}
.status-name{font-weight:bold;color:#fff;font-size:.92rem}
.status-score{float:right;font-size:1.1rem;font-weight:bold}
.status-trend{font-size:.75rem;margin-left:.4rem}
.status-body{color:var(--muted);font-size:.82rem;margin-top:.2rem;clear:both}
.palmares{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}
@media(max-width:700px){.palmares{grid-template-columns:1fr}}
.palmares-col h3{font-size:1rem;margin-bottom:.6rem}
.palmares-col.top h3{color:#2ecc71}
.palmares-col.worst h3{color:#e74c3c}
.palm-row{display:flex;align-items:baseline;gap:.5rem;margin-bottom:.4rem;
          font-size:.88rem}
.palm-rank{color:var(--muted);min-width:1.5rem;text-align:right}
.palm-name{font-weight:bold;color:#fff}
.palm-score{font-weight:bold;min-width:2.5rem}
.palm-reason{color:var(--muted);font-size:.82rem}
"""

_TYPE_COLORS = {
    "drama": "#e74c3c", "relationship": "#9b59b6", "collaboration": "#3498db",
    "reputation": "#f39c12", "irl_vs_persona": "#1abc9c", "trend": "#2ecc71",
}


def _score_color(score: float) -> str:
    if score >= 7:
        return "#2ecc71"
    if score >= 4:
        return "#f39c12"
    return "#e74c3c"


def _trend_arrow(trend: str) -> str:
    return {"rising": "&#9650;", "falling": "&#9660;", "stable": "&#9644;"}.get(
        trend, ""
    )


def _trend_color(trend: str) -> str:
    return {"rising": "#2ecc71", "falling": "#e74c3c", "stable": "#9090b0"}.get(
        trend, "#9090b0"
    )


def _render_html(result: dict, community_name: str, date_range: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        f'<title>Executive Summary — {_esc(community_name)}</title>\n'
        f'<style>{_CSS}</style>\n</head>\n<body>\n'
        '<div class="container">\n'
        f'<h1>Executive Summary</h1>\n'
        f'<p class="meta">{_esc(community_name)} | {_esc(date_range)} | Generated: {ts}</p>\n'
    ]

    # Section 1: Developing Stories
    stories = result.get("developing_stories", [])
    if stories:
        parts.append('<h2>Developing Stories</h2>\n')
        for s in stories:
            gtype = s.get("gossip_type", "")
            color = _TYPE_COLORS.get(gtype, "#7c4dff")
            parts.append(
                f'<div class="story">'
                f'<span class="story-title">{_esc(s.get("title", ""))}</span>'
                f'<span class="story-type" style="background:{color}">{_esc(gtype)}</span>'
                f'<div class="story-body">{_esc(s.get("summary", ""))}</div>'
                f'</div>\n'
            )

    # Section 2: YouTuber Status
    statuses = result.get("youtuber_status", [])
    if statuses:
        parts.append('<h2>YouTuber Status Report</h2>\n<div class="status-grid">\n')
        for st in statuses:
            score = st.get("score", 5)
            trend = st.get("trend", "stable")
            sc = _score_color(score)
            tc = _trend_color(trend)
            arrow = _trend_arrow(trend)
            parts.append(
                f'<div class="status-card">'
                f'<span class="status-score" style="color:{sc}">{score:.1f}</span>'
                f'<span class="status-name">{_esc(st.get("name", "?"))}</span>'
                f'<span class="status-trend" style="color:{tc}">{arrow} {_esc(trend)}</span>'
                f'<div class="status-body">{_esc(st.get("status_summary", ""))}</div>'
                f'</div>\n'
            )
        parts.append('</div>\n')

    # Section 3: Palmarès
    top5 = result.get("top_5", [])
    worst5 = result.get("worst_5", [])
    if top5 or worst5:
        parts.append('<h2>Palmar&egrave;s</h2>\n<div class="palmares">\n')

        parts.append('<div class="palmares-col top"><h3>&#9733; Top 5 — Most Respected</h3>\n')
        for entry in top5:
            sc = _score_color(entry.get("score", 5))
            parts.append(
                f'<div class="palm-row">'
                f'<span class="palm-rank">#{entry.get("rank", "")}</span>'
                f'<span class="palm-name">{_esc(entry.get("name", ""))}</span>'
                f'<span class="palm-score" style="color:{sc}">{entry.get("score", 0):.1f}</span>'
                f'<span class="palm-reason">{_esc(entry.get("reason", ""))}</span>'
                f'</div>\n'
            )
        parts.append('</div>\n')

        parts.append('<div class="palmares-col worst"><h3>&#9888; Worst 5 — Lowest Reputation</h3>\n')
        for entry in worst5:
            sc = _score_color(entry.get("score", 5))
            parts.append(
                f'<div class="palm-row">'
                f'<span class="palm-rank">#{entry.get("rank", "")}</span>'
                f'<span class="palm-name">{_esc(entry.get("name", ""))}</span>'
                f'<span class="palm-score" style="color:{sc}">{entry.get("score", 0):.1f}</span>'
                f'<span class="palm-reason">{_esc(entry.get("reason", ""))}</span>'
                f'</div>\n'
            )
        parts.append('</div>\n</div>\n')

    parts.append('</div>\n</body>\n</html>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_executive_summary(conn, community_id: int,
                               progress_callback=None) -> str:
    """
    Generate an executive summary HTML string for a community.
    Calls the LLM (analyze backend) to synthesise data into a briefing.
    """
    def _cb(msg):
        if progress_callback:
            progress_callback(msg)
        log.info(msg)

    _cb("Gathering data for executive summary...")
    data = _gather_data(conn, community_id)

    _cb("Calling LLM for executive summary synthesis...")
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="analyze")

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    user_prompt = _build_llm_payload(data)

    result = llm.complete_json(system_prompt, user_prompt,
                               max_tokens=llm.max_tokens_analyze)

    date_range = (
        f"{data['agg_meta'].get('date_range_start', '?')} → "
        f"{data['agg_meta'].get('date_range_end', '?')}"
    )
    _cb("Rendering executive summary HTML...")
    html = _render_html(result, data["community"]["name"], date_range)
    return html
