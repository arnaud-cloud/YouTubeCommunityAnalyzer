"""
Executive summary generator — produces two report types:

1. **Top Insights** — concise ~2-page briefing of high-confidence findings
   (themes with N+ supporting comments, default N=10).

2. **Full Executive Summary** — comprehensive 5-7 page report with tiered
   analysis (Key Findings N≥10, Emerging Trends 5–9, Early Signals <5),
   plus a Forecast Review comparing against the previous edition.

Both reports are cached in the executive_reports table (HTML + raw JSON)
so they survive app restarts without re-calling the LLM.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .db import get_all_settings, get_community_channel_ids
from .llm_client import LLMClient, _settings_to_llm_config

log = logging.getLogger(__name__)

PROMPT_TOP_INSIGHTS = Path(__file__).resolve().parent.parent / "prompts" / "executive_summary.txt"
PROMPT_FULL_REPORT = Path(__file__).resolve().parent.parent / "prompts" / "executive_summary_full.txt"


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

def _gather_data(conn, community_id: int, min_evidence: int = 0,
                 max_themes: int = 20) -> dict:
    """Pull together all data the LLM needs."""
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

    # Themes
    themes = []
    for row in conn.execute(
        """SELECT title, description, gossip_type, subjects, activity_json,
                  first_seen_at, last_seen_at, total_evidence
           FROM themes WHERE community_id = ? AND total_evidence >= ?
           ORDER BY last_seen_at DESC, total_evidence DESC
           LIMIT ?""",
        (community_id, min_evidence, max_themes),
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


def _base_payload(data: dict) -> dict:
    """Common payload fields shared by both report types."""
    channel_display = [
        data["channel_names"].get(ch, ch) for ch in data["channel_ids"]
    ]
    top_entities = sorted(
        data["entity_metrics"].values(),
        key=lambda x: -x.get("total_mentions", 0),
    )[:30]
    a = data["analysis"]
    return {
        "community_name": data["community"]["name"],
        "channels": channel_display,
        "date_range": {
            "start": data["agg_meta"].get("date_range_start", "?"),
            "end": data["agg_meta"].get("date_range_end", "?"),
        },
        "total_videos": data["agg_meta"].get("total_videos", 0),
        "total_gossip_items": data["agg_meta"].get("total_gossip_items", 0),
        "entity_metrics": top_entities,
        "reputation_rankings": a.get("reputation_rankings", [])[:15],
        "top_drama_items": a.get("top_drama_items", [])[:10],
        "community_trends": a.get("community_trends", [])[:10],
        "persona_vs_reality": a.get("persona_vs_reality", [])[:10],
    }


def _build_top_insights_payload(data: dict, min_evidence: int) -> str:
    payload = _base_payload(data)
    payload["recent_themes"] = data["themes"]
    payload["NOTE"] = (
        f"All themes below have been pre-filtered to those with at least "
        f"{min_evidence} supporting comments. These are HIGH-CONFIDENCE "
        f"findings only. Use assertive language rather than hedging."
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_full_report_payload(data: dict, previous_json: dict | None) -> str:
    """Build payload with themes segmented into three tiers."""
    payload = _base_payload(data)
    themes = data["themes"]

    payload["themes_key_findings"] = [
        t for t in themes if t.get("total_evidence", 0) >= 10
    ]
    payload["themes_emerging"] = [
        t for t in themes if 5 <= t.get("total_evidence", 0) < 10
    ]
    payload["themes_early_signals"] = [
        t for t in themes if t.get("total_evidence", 0) < 5
    ]

    if previous_json:
        payload["previous_report"] = {
            "early_signals": previous_json.get("early_signals", []),
            "emerging_trends": previous_json.get("emerging_trends", []),
            "report_date": previous_json.get("report_date", "unknown"),
        }
    else:
        payload["previous_report"] = None

    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTML rendering — shared styles
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
h3{font-size:1rem;color:#c0b0ff;margin:1.2rem 0 .4rem}
.meta{color:var(--muted);font-size:.85rem;margin-bottom:1.5rem}
.story{background:var(--surface);border:1px solid var(--border);border-radius:6px;
       padding:.8rem 1rem;margin-bottom:.6rem}
.story-title{font-weight:bold;color:#fff;font-size:.95rem}
.story-type{display:inline-block;font-size:.7rem;padding:.15em .5em;border-radius:3px;
            color:#fff;margin-left:.5rem;vertical-align:middle}
.story-body{color:var(--muted);font-size:.88rem;margin-top:.25rem}
.story-evidence{color:var(--muted);font-size:.75rem;margin-top:.2rem}
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
.trajectory{font-size:.75rem;display:inline-block;padding:.1em .4em;border-radius:3px;
            margin-left:.5rem;color:#fff}
.trajectory-accelerating{background:#e74c3c}
.trajectory-steady{background:#f39c12}
.trajectory-slowing{background:#3498db}
.forecast-item{background:var(--surface);border:1px solid var(--border);border-radius:6px;
               padding:.6rem .9rem;margin-bottom:.5rem}
.forecast-status{display:inline-block;font-size:.75rem;font-weight:bold;
                 padding:.15em .5em;border-radius:3px;margin-right:.5rem}
.forecast-confirmed{background:#2ecc71;color:#fff}
.forecast-developing{background:#f39c12;color:#fff}
.forecast-faded{background:#555;color:#aaa}
.analyst-note{background:var(--surface);border-left:3px solid var(--accent);
              padding:.5rem .8rem;margin-bottom:.4rem;color:var(--muted);
              font-size:.88rem;font-style:italic}

@media print{
  :root{--bg:#fff;--surface:#f8f8f8;--border:#ccc;--text:#111;
        --muted:#555;--accent:#4a2fbf}
  body{background:#fff;color:#111;font-size:10pt;padding:0}
  .container{max-width:100%;padding:0}
  h1{font-size:16pt;color:#4a2fbf}
  h2{font-size:12pt;color:#4a2fbf;margin:1rem 0 .5rem}
  h3{font-size:10pt;color:#4a2fbf}
  .meta{font-size:8pt}
  .story,.status-card,.forecast-item{border:1px solid #ccc;background:#f8f8f8;
    break-inside:avoid}
  .story-title,.status-name,.palm-name{color:#111}
  .story-body,.status-body,.palm-reason{color:#333}
  .palmares{grid-template-columns:1fr 1fr}
  .no-print{display:none !important}
}
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


# -- Shared HTML fragments ---------------------------------------------------

def _html_stories(stories: list, heading: str = "Developing Stories") -> str:
    if not stories:
        return ""
    parts = [f'<h2>{heading}</h2>\n']
    for s in stories:
        gtype = s.get("gossip_type", "")
        color = _TYPE_COLORS.get(gtype, "#7c4dff")
        ev = s.get("evidence_count", "")
        ev_html = f'<div class="story-evidence">{ev} supporting comments</div>' if ev else ""
        parts.append(
            f'<div class="story">'
            f'<span class="story-title">{_esc(s.get("title", ""))}</span>'
            f'<span class="story-type" style="background:{color}">{_esc(gtype)}</span>'
            f'<div class="story-body">{_esc(s.get("summary", ""))}</div>'
            f'{ev_html}'
            f'</div>\n'
        )
    return "".join(parts)


def _html_status_grid(statuses: list) -> str:
    if not statuses:
        return ""
    parts = ['<h2>YouTuber Status Report</h2>\n<div class="status-grid">\n']
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
    return "".join(parts)


def _html_palmares(top5: list, worst5: list) -> str:
    if not top5 and not worst5:
        return ""
    parts = ['<h2>Palmar&egrave;s</h2>\n<div class="palmares">\n']

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
    return "".join(parts)


# -- Top Insights renderer (short report) ------------------------------------

def _render_top_insights_html(result: dict, community_name: str,
                               date_range: str, min_evidence: int) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        f'<title>Top Insights — {_esc(community_name)}</title>\n'
        f'<style>{_CSS}</style>\n</head>\n<body>\n'
        '<div class="container">\n'
        f'<h1>Top Insights</h1>\n'
        f'<p class="meta">{_esc(community_name)} | {_esc(date_range)} | Generated: {ts}'
        f'<br>Filtered to items with {min_evidence}+ supporting comments</p>\n'
    ]
    parts.append(_html_stories(result.get("developing_stories", [])))
    parts.append(_html_status_grid(result.get("youtuber_status", [])))
    parts.append(_html_palmares(
        result.get("top_5", []), result.get("worst_5", [])
    ))
    parts.append('</div>\n</body>\n</html>')
    return "".join(parts)


# -- Full Executive Summary renderer -----------------------------------------

def _render_full_html(result: dict, community_name: str,
                      date_range: str) -> str:
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

    # Section 1: Key Findings
    kf = result.get("key_findings", {})
    parts.append('<h2>Key Findings</h2>\n'
                 '<p style="color:var(--muted);font-size:.82rem;margin-bottom:.8rem">'
                 'Established narratives backed by 10+ supporting comments.</p>\n')
    parts.append(_html_stories(kf.get("developing_stories", []),
                               heading="Developing Stories"))
    parts.append(_html_status_grid(kf.get("youtuber_status", [])))
    parts.append(_html_palmares(kf.get("top_5", []), kf.get("worst_5", [])))

    # Section 2: Emerging Trends
    emerging = result.get("emerging_trends", [])
    if emerging:
        parts.append(
            '<h2>Emerging Trends</h2>\n'
            '<p style="color:var(--muted);font-size:.82rem;margin-bottom:.8rem">'
            'Gaining traction (5–9 supporting comments) — not yet fully established.</p>\n'
        )
        for item in emerging:
            gtype = item.get("gossip_type", "")
            color = _TYPE_COLORS.get(gtype, "#7c4dff")
            traj = item.get("trajectory", "steady")
            traj_cls = f"trajectory trajectory-{traj}"
            ev = item.get("evidence_count", "?")
            parts.append(
                f'<div class="story">'
                f'<span class="story-title">{_esc(item.get("title", ""))}</span>'
                f'<span class="story-type" style="background:{color}">{_esc(gtype)}</span>'
                f'<span class="{traj_cls}">{_esc(traj)}</span>'
                f'<div class="story-body">{_esc(item.get("summary", ""))}</div>'
                f'<div class="story-evidence">{ev} supporting comments'
                f' — {_esc(item.get("why_it_matters", ""))}</div>'
                f'</div>\n'
            )

    # Section 3: Early Signals
    signals = result.get("early_signals", [])
    if signals:
        parts.append(
            '<h2>Early Signals</h2>\n'
            '<p style="color:var(--muted);font-size:.82rem;margin-bottom:.8rem">'
            'Speculative items with fewer than 5 supporting comments — possibly noise, '
            'possibly the start of something significant.</p>\n'
        )
        for item in signals:
            gtype = item.get("gossip_type", "")
            color = _TYPE_COLORS.get(gtype, "#7c4dff")
            ev = item.get("evidence_count", "?")
            parts.append(
                f'<div class="story">'
                f'<span class="story-title">{_esc(item.get("title", ""))}</span>'
                f'<span class="story-type" style="background:{color}">{_esc(gtype)}</span>'
                f'<div class="story-body">{_esc(item.get("summary", ""))}</div>'
                f'<div class="story-evidence">{ev} supporting comments'
                f' — {_esc(item.get("why_to_watch", ""))}</div>'
                f'</div>\n'
            )

    # Section 4: Forecast Review
    review = result.get("forecast_review", [])
    if review:
        parts.append(
            '<h2>Forecast Review</h2>\n'
            '<p style="color:var(--muted);font-size:.82rem;margin-bottom:.8rem">'
            'Comparing items flagged in the previous edition against current evidence.</p>\n'
        )
        for item in review:
            status = item.get("current_status", "developing")
            status_cls = f"forecast-status forecast-{status}"
            prev_tier = (item.get("previous_tier") or "").replace("_", " ")
            parts.append(
                f'<div class="forecast-item">'
                f'<span class="{status_cls}">{_esc(status.upper())}</span>'
                f'<strong>{_esc(item.get("title", ""))}</strong>'
                f' <span style="color:var(--muted);font-size:.78rem">'
                f'(was: {_esc(prev_tier)})</span>'
                f'<div style="color:var(--muted);font-size:.85rem;margin-top:.2rem">'
                f'{_esc(item.get("explanation", ""))}</div>'
                f'</div>\n'
            )

    # Section 5: Analyst Notes
    notes = result.get("analyst_notes", [])
    if notes:
        parts.append('<h2>Analyst Notes</h2>\n')
        for note in notes:
            parts.append(f'<div class="analyst-note">{_esc(note)}</div>\n')

    parts.append('</div>\n</body>\n</html>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# DB caching
# ---------------------------------------------------------------------------

def _save_report(conn, community_id: int, report_type: str,
                 min_evidence: int, html: str, result_json: dict,
                 llm_backend: str) -> int:
    cur = conn.execute(
        """INSERT INTO executive_reports
               (community_id, report_type, min_evidence, report_html,
                report_json, llm_backend, created_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        (community_id, report_type, min_evidence, html,
         json.dumps(result_json, ensure_ascii=False), llm_backend),
    )
    conn.commit()
    return cur.lastrowid


def get_cached_report(conn, community_id: int, report_type: str,
                      min_evidence: int = 0) -> dict | None:
    row = conn.execute(
        """SELECT * FROM executive_reports
           WHERE community_id = ? AND report_type = ? AND min_evidence = ?
           ORDER BY id DESC LIMIT 1""",
        (community_id, report_type, min_evidence),
    ).fetchone()
    return dict(row) if row else None


def get_report_by_id(conn, report_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM executive_reports WHERE id = ?", (report_id,)
    ).fetchone()
    return dict(row) if row else None


def _get_previous_report_json(conn, community_id: int,
                              report_type: str) -> dict | None:
    """Load the structured JSON from the most recent report of this type,
    so the Forecast Review section can reference it."""
    row = conn.execute(
        """SELECT report_json FROM executive_reports
           WHERE community_id = ? AND report_type = ?
           ORDER BY id DESC LIMIT 1""",
        (community_id, report_type),
    ).fetchone()
    if row and row["report_json"]:
        return _safe_json(row["report_json"])
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_top_insights(conn, community_id: int,
                          min_evidence: int = 10,
                          progress_callback=None) -> dict:
    """
    Generate a concise "Top Insights" report (~2 pages) restricted to
    themes with at least *min_evidence* supporting comments.
    Returns {"id": <report_id>, "html": <html_string>}.
    """
    def _cb(msg):
        if progress_callback:
            progress_callback(msg)
        log.info(msg)

    _cb("Gathering data for top insights...")
    data = _gather_data(conn, community_id, min_evidence=min_evidence)

    _cb("Calling LLM for top insights synthesis...")
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="analyze")

    system_prompt = PROMPT_TOP_INSIGHTS.read_text(encoding="utf-8")
    user_prompt = _build_top_insights_payload(data, min_evidence)

    result = llm.complete_json(system_prompt, user_prompt,
                               max_tokens=llm.max_tokens_analyze)

    date_range = (
        f"{data['agg_meta'].get('date_range_start', '?')} → "
        f"{data['agg_meta'].get('date_range_end', '?')}"
    )
    _cb("Rendering top insights HTML...")
    html = _render_top_insights_html(result, data["community"]["name"],
                                     date_range, min_evidence)

    report_id = _save_report(conn, community_id, "top_insights",
                             min_evidence, html, result, llm.backend)
    _cb(f"Top insights saved (id={report_id}).")
    return {"id": report_id, "html": html}


def generate_executive_summary(conn, community_id: int,
                               progress_callback=None) -> dict:
    """
    Generate the full Executive Summary (~5-7 pages) with tiered analysis
    and forecast review against the previous edition.
    Returns {"id": <report_id>, "html": <html_string>}.
    """
    def _cb(msg):
        if progress_callback:
            progress_callback(msg)
        log.info(msg)

    _cb("Gathering data for executive summary...")
    data = _gather_data(conn, community_id, min_evidence=0, max_themes=60)

    _cb("Loading previous report for forecast review...")
    previous_json = _get_previous_report_json(conn, community_id,
                                              "executive_summary")
    if previous_json:
        _cb(f"Found previous report (dated {previous_json.get('report_date', '?')}).")
    else:
        _cb("No previous report found — forecast review will be skipped.")

    _cb("Calling LLM for full executive summary synthesis...")
    settings = get_all_settings(conn)
    cfg = _settings_to_llm_config(settings)
    llm = LLMClient(cfg, role="analyze")

    system_prompt = PROMPT_FULL_REPORT.read_text(encoding="utf-8")
    user_prompt = _build_full_report_payload(data, previous_json)

    result = llm.complete_json(system_prompt, user_prompt,
                               max_tokens=llm.max_tokens_analyze)

    date_range = (
        f"{data['agg_meta'].get('date_range_start', '?')} → "
        f"{data['agg_meta'].get('date_range_end', '?')}"
    )
    _cb("Rendering executive summary HTML...")
    html = _render_full_html(result, data["community"]["name"], date_range)

    report_id = _save_report(conn, community_id, "executive_summary",
                             0, html, result, llm.backend)
    _cb(f"Executive summary saved (id={report_id}).")
    return {"id": report_id, "html": html}
