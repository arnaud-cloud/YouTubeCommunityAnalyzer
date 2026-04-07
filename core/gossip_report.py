"""
Gossip report generator — Step 5: Produces HTML report from analysis results.

All charts are pure SVG — no external JS libraries needed. The generated
HTML is stored in the analysis_results row and can be served by the web layer.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from datetime import datetime

log = logging.getLogger(__name__)

# -- Colour palette -----------------------------------------------------------

PALETTE = [
    "#7c4dff", "#2ecc71", "#e74c3c", "#f39c12", "#3498db",
    "#e91e63", "#00bcd4", "#ff5722", "#9c27b0", "#4caf50",
    "#ff9800", "#607d8b", "#795548", "#009688", "#673ab7",
]


def _sentiment_color(v: float) -> str:
    if v >= 0.3:  return "#2ecc71"
    if v >= 0.0:  return "#a8e6cf"
    if v >= -0.3: return "#f39c12"
    return "#e74c3c"


def _esc(s) -> str:
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _safe_json_loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


# -- SVG primitives -----------------------------------------------------------

def _svg_hbar(title, labels, values, colors, width=700,
              x_label="", x_min=None, x_max=None):
    row_h = 28
    ml, mr, mt, mb = 160, 40, 50, 50
    n = len(labels)
    if n == 0:
        return ""
    height = mt + mb + n * row_h
    x_min = x_min if x_min is not None else min(min(values), 0)
    x_max = x_max if x_max is not None else max(max(values), 0.001)
    span = x_max - x_min or 1
    cw = width - ml - mr

    def xp(v): return ml + (v - x_min) / span * cw
    zero_x = xp(0)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="background:#1a1a2e;border-radius:8px;font-family:\'Segoe UI\',sans-serif">',
        f'<text x="{width//2}" y="30" text-anchor="middle" fill="#c0b0ff" '
        f'font-size="14" font-weight="bold">{_esc(title)}</text>',
        f'<line x1="{zero_x:.1f}" y1="{mt}" x2="{zero_x:.1f}" '
        f'y2="{mt+n*row_h}" stroke="#444" stroke-width="1"/>',
    ]
    for i, (lbl, val, col) in enumerate(zip(labels, values, colors)):
        y = mt + i * row_h + 4
        bx = min(xp(0), xp(val))
        bw = abs(xp(val) - xp(0))
        bh = row_h - 8
        out += [
            f'<rect x="{bx:.1f}" y="{y}" width="{max(bw,1):.1f}" '
            f'height="{bh}" fill="{col}" rx="2" opacity="0.85"/>',
            f'<text x="{ml-6}" y="{y+bh//2+4}" text-anchor="end" '
            f'fill="#ddd" font-size="11">{_esc(str(lbl)[:22])}</text>',
        ]
        vx = xp(val) + (4 if val >= 0 else -4)
        anc = "start" if val >= 0 else "end"
        out.append(
            f'<text x="{vx:.1f}" y="{y+bh//2+4}" text-anchor="{anc}" '
            f'fill="#fff" font-size="10">{val:.2f}</text>'
        )
    out.append("</svg>")
    return "\n".join(out)


def _svg_donut(title, labels, values, width=440):
    if not values or sum(values) == 0:
        return ""
    h = width
    cx, cy = width // 2, h // 2
    ro, ri = width // 3, width // 6
    total = sum(values)
    angle = -math.pi / 2
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{h+40}" '
        f'style="background:#1a1a2e;border-radius:8px;font-family:\'Segoe UI\',sans-serif">',
        f'<text x="{cx}" y="22" text-anchor="middle" fill="#c0b0ff" '
        f'font-size="14" font-weight="bold">{_esc(title)}</text>',
    ]
    for i, (lbl, val) in enumerate(zip(labels, values)):
        sweep = 2 * math.pi * val / total
        x1 = cx + ro * math.cos(angle);       y1 = cy + ro * math.sin(angle)
        x2 = cx + ro * math.cos(angle+sweep); y2 = cy + ro * math.sin(angle+sweep)
        ix1 = cx + ri * math.cos(angle+sweep); iy1 = cy + ri * math.sin(angle+sweep)
        ix2 = cx + ri * math.cos(angle);       iy2 = cy + ri * math.sin(angle)
        lg = 1 if sweep > math.pi else 0
        col = PALETTE[i % len(PALETTE)]
        ma = angle + sweep / 2
        lx = cx + (ro + 22) * math.cos(ma)
        ly = cy + (ro + 22) * math.sin(ma)
        out.append(
            f'<path d="M{x1:.1f},{y1:.1f} A{ro},{ro} 0 {lg},1 '
            f'{x2:.1f},{y2:.1f} L{ix1:.1f},{iy1:.1f} A{ri},{ri} 0 '
            f'{lg},0 {ix2:.1f},{iy2:.1f} Z" fill="{col}" opacity="0.9"/>'
        )
        if sweep > 0.15:
            out.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                f'fill="{col}" font-size="10">{_esc(lbl)}: {val}</text>'
            )
        angle += sweep
    out += [
        f'<text x="{cx}" y="{cy+6}" text-anchor="middle" fill="#fff" '
        f'font-size="15" font-weight="bold">{total}</text>',
        "</svg>",
    ]
    return "\n".join(out)


def _svg_heatmap(title, row_labels, col_labels, matrix, width=700):
    if not row_labels or not col_labels:
        return ""
    ml, mt, mb = 160, 80, 30
    col_w = max(40, (width - ml - 20) // len(col_labels))
    row_h = 24
    height = mt + len(row_labels) * row_h + mb
    flat = [v for row in matrix for v in row]
    maxv = max(flat + [1])

    def cell_color(v):
        t = v / maxv
        return f"rgb({int(30+t*120)},{int(30+t*80)},{int(180+t*75)})"

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="background:#1a1a2e;border-radius:8px;font-family:\'Segoe UI\',sans-serif">',
        f'<text x="{width//2}" y="22" text-anchor="middle" fill="#c0b0ff" '
        f'font-size="14" font-weight="bold">{_esc(title)}</text>',
    ]
    for j, col in enumerate(col_labels):
        cx2 = ml + j * col_w + col_w // 2
        out.append(
            f'<text x="{cx2}" y="{mt-8}" text-anchor="middle" fill="#aaa" '
            f'font-size="10" transform="rotate(-35,{cx2},{mt-8})">'
            f'{_esc(str(col)[:14])}</text>'
        )
    for i, rl in enumerate(row_labels):
        y = mt + i * row_h
        out.append(
            f'<text x="{ml-6}" y="{y+row_h//2+4}" text-anchor="end" '
            f'fill="#ddd" font-size="11">{_esc(str(rl)[:22])}</text>'
        )
        for j, val in enumerate(matrix[i]):
            x = ml + j * col_w
            out.append(
                f'<rect x="{x+1}" y="{y+1}" width="{col_w-2}" '
                f'height="{row_h-2}" fill="{cell_color(val)}" rx="2"/>'
            )
            if val > 0:
                out.append(
                    f'<text x="{x+col_w//2}" y="{y+row_h//2+4}" '
                    f'text-anchor="middle" fill="#fff" font-size="10">{val}</text>'
                )
    out.append("</svg>")
    return "\n".join(out)


def _svg_line_chart(title, series, width=700, top_n=6):
    top = sorted(series.items(), key=lambda x: -sum(x[1].values()))[:top_n]
    if not top:
        return ""
    all_months = sorted({m for _, d in top for m in d})
    if len(all_months) < 2:
        return ""
    ml, mr, mt, mb = 60, 30, 50, 60
    cw = width - ml - mr
    ch = 200
    height = mt + ch + mb
    max_val = max(v for _, d in top for v in d.values()) or 1

    def px(mi): return ml + mi / (len(all_months) - 1) * cw
    def py(val): return mt + ch - (val / max_val) * ch

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'style="background:#1a1a2e;border-radius:8px;font-family:\'Segoe UI\',sans-serif">',
        f'<text x="{width//2}" y="28" text-anchor="middle" fill="#c0b0ff" '
        f'font-size="14" font-weight="bold">{_esc(title)}</text>',
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ch}" stroke="#333"/>',
        f'<line x1="{ml}" y1="{mt+ch}" x2="{ml+cw}" y2="{mt+ch}" stroke="#333"/>',
    ]
    for i, (entity, data) in enumerate(top):
        col = PALETTE[i % len(PALETTE)]
        points = " ".join(
            f"{px(j):.1f},{py(data.get(m, 0)):.1f}"
            for j, m in enumerate(all_months)
        )
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{col}" '
            f'stroke-width="2" stroke-linejoin="round"/>'
        )
        last_val = data.get(all_months[-1], 0)
        out.append(
            f'<text x="{px(len(all_months)-1)+4:.1f}" '
            f'y="{py(last_val):.1f}" fill="{col}" font-size="10">'
            f'{_esc(entity[:16])}</text>'
        )
    for j, m in enumerate(all_months):
        if j % max(1, len(all_months) // 8) == 0:
            out.append(
                f'<text x="{px(j):.1f}" y="{mt+ch+18}" text-anchor="middle" '
                f'fill="#888" font-size="9" transform="rotate(-30,{px(j):.1f},{mt+ch+18})">'
                f'{_esc(m)}</text>'
            )
    out.append("</svg>")
    return "\n".join(out)


# -- Chart builders -----------------------------------------------------------

def _make_charts(entity_metrics, gossip_rows, velocity, top_commenters,
                 quality_gossip_rows=None):
    top = sorted(entity_metrics.values(), key=lambda x: x.get("avg_sentiment", 0))[-15:]
    sentiment_chart = ""
    if top:
        labels = [e["entity"] for e in top]
        values = [round(e.get("avg_sentiment", 0.0), 3) for e in top]
        sentiment_chart = _svg_hbar(
            "Community Sentiment by Entity", labels, values,
            [_sentiment_color(v) for v in values],
            x_label="<- negative   neutral   positive ->",
            x_min=-1.0, x_max=1.0,
        )

    top_m = sorted(entity_metrics.values(), key=lambda x: -x.get("total_mentions", 0))[:12]
    all_channels = sorted({ch for e in top_m for ch in e.get("by_channel", {})})
    heatmap = ""
    if all_channels and top_m:
        matrix = [[e.get("by_channel", {}).get(ch, 0) for ch in all_channels] for e in top_m]
        heatmap = _svg_heatmap(
            "Entity Mention Frequency by Channel",
            [e["entity"] for e in top_m], all_channels, matrix,
        )

    counts = Counter(r.get("gossip_type", "unknown") for r in gossip_rows)
    type_pie = ""
    if counts:
        pairs = sorted(counts.items(), key=lambda x: -x[1])
        labels, values = zip(*pairs)
        type_pie = _svg_donut("Gossip by Type", list(labels), list(values))

    conf_counts = Counter(r.get("confidence", "low") for r in gossip_rows)
    conf_chart = ""
    order = ["high", "medium", "low"]
    conf_values = [float(conf_counts.get(c, 0)) for c in order]
    if any(conf_values):
        conf_chart = _svg_hbar(
            "Gossip Items by Confidence Level", order, conf_values,
            ["#2ecc71", "#f39c12", "#e74c3c"],
            x_min=0.0, x_max=max(conf_values) * 1.15,
        )

    velocity_chart = _svg_line_chart("Entity Mention Velocity Over Time", velocity) if velocity else ""

    commenter_chart = ""
    if top_commenters:
        tc = top_commenters[:15]
        labels = [c.get("author_name", "?")[:20] for c in tc]
        values = [float(c.get("channel_count", 0)) for c in tc]
        commenter_chart = _svg_hbar(
            "Top Cross-Channel Commenters", labels, values,
            [PALETTE[i % len(PALETTE)] for i in range(len(labels))],
            x_min=0, x_max=max(values + [1]) * 1.15,
        )

    quality_chart = ""
    scored_rows = [r for r in (quality_gossip_rows or [])
                   if r.get("evidence_quality_score") is not None]
    if scored_rows:
        tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
        for r in scored_rows:
            s = r["evidence_quality_score"]
            t = "A" if s >= 0.65 else "B" if s >= 0.45 else "C" if s >= 0.25 else "D"
            tier_counts[t] += 1
        tiers = list(tier_counts.keys())
        tcounts = [float(v) for v in tier_counts.values()]
        if any(tcounts):
            quality_chart = _svg_hbar(
                "Gossip Items by Evidence Quality Tier", tiers, tcounts,
                ["#2ecc71", "#a8e6cf", "#f39c12", "#e74c3c"],
                x_min=0, x_max=max(tcounts) * 1.15,
            )

    return sentiment_chart, velocity_chart, heatmap, type_pie, conf_chart, commenter_chart, quality_chart


# -- Report normalisation -----------------------------------------------------

def _coerce_list(items, text_key="description"):
    result = []
    for item in (items or []):
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str):
            result.append({text_key: item})
    return result


def _normalise_analysis(a: dict) -> dict:
    aliases = {
        "reputation_rankings": ["power_rankings", "reputation_ranking", "rankings"],
        "top_drama_items": ["top_narratives", "drama_items", "narratives"],
        "relationship_map": ["key_relationships", "relationships"],
        "persona_vs_reality": ["persona_gaps", "persona_reality_gaps", "irl_vs_persona"],
        "community_trends": ["collaboration_intel", "trends", "community_dynamics"],
        "notable_quotes": ["raw_gossip_highlights", "highlights", "top_quotes"],
    }
    for canonical, variants in aliases.items():
        if canonical not in a or not a[canonical]:
            for variant in variants:
                if variant in a and a[variant]:
                    a[canonical] = a[variant]
                    break
    list_sections = {
        "reputation_rankings": "key_reason",
        "top_drama_items": "description",
        "relationship_map": "description",
        "persona_vs_reality": "claimed_discrepancy",
        "notable_quotes": "text",
        "corroborated_claims": "claim",
    }
    for section, text_key in list_sections.items():
        if section in a:
            a[section] = _coerce_list(a[section], text_key)
    return a


# -- HTML generation ----------------------------------------------------------

CSS = """\
:root{--bg:#0f0f1a;--surface:#1a1a2e;--border:#2a2a4a;--text:#e8e8f0;
      --muted:#9090b0;--accent:#7c4dff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
     font-family:'Segoe UI',system-ui,sans-serif;line-height:1.7;padding:0 1rem}
.container{max-width:1000px;margin:0 auto;padding:2rem 0 5rem}
h1{font-size:2rem;color:var(--accent);margin-bottom:.3rem}
h2{font-size:1.35rem;color:var(--accent);margin:2.5rem 0 1rem;
   border-bottom:1px solid var(--border);padding-bottom:.4rem}
h3{font-size:1.05rem;color:#c0b0ff;margin:1.4rem 0 .4rem}
p{margin:.5rem 0}
em{color:var(--muted)} strong{color:#fff}
code{background:var(--border);padding:.1em .4em;border-radius:3px;
     font-size:.88em;color:#90d0ff}
hr{border:none;border-top:1px solid var(--border);margin:2rem 0}
blockquote{border-left:3px solid var(--accent);padding:.6rem 1rem;
           margin:1rem 0;background:var(--surface);border-radius:4px;
           color:var(--muted);font-style:italic}
li{margin:.3rem 0 .3rem 1.5rem} ul{margin:.4rem 0}
.meta{color:var(--muted);font-size:.9rem;margin-bottom:1.5rem}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem;margin:2rem 0}
.chart-full{grid-column:1/-1}
.chart{background:var(--surface);border-radius:8px;padding:1rem;
       border:1px solid var(--border);overflow-x:auto}
.chart svg{max-width:100%;height:auto}
@media(max-width:700px){.charts{grid-template-columns:1fr}}
"""


def _inline_md(s: str) -> str:
    links = []
    def save_link(m):
        links.append((m.group(1), m.group(2)))
        return f"__LINK{len(links)-1}__"
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', save_link, s)
    s = _esc(s)
    for i, (text, url) in enumerate(links):
        s = s.replace(f"__LINK{i}__",
                      f'<a href="{_esc(url)}" target="_blank">{_esc(text)}</a>')
    s = re.sub(r'[*][*](.+?)[*][*]', lambda m: f'<strong>{m.group(1)}</strong>', s)
    s = re.sub(r'[*](.+?)[*]', lambda m: f'<em>{m.group(1)}</em>', s)
    s = re.sub(r'`(.+?)`', lambda m: f'<code>{m.group(1)}</code>', s)
    return s


def _md_to_html(md: str) -> str:
    html, in_ul = [], False
    for line in md.split("\n"):
        s = line.strip()
        def close_ul():
            nonlocal in_ul
            if in_ul: html.append("</ul>"); in_ul = False
        if   s.startswith("### "): close_ul(); html.append(f"<h3>{_inline_md(s[4:])}</h3>")
        elif s.startswith("## "):  close_ul(); html.append(f"<h2>{_inline_md(s[3:])}</h2>")
        elif s.startswith("# "):   close_ul(); html.append(f"<h1>{_inline_md(s[2:])}</h1>")
        elif s.startswith("> "):   close_ul(); html.append(f"<blockquote>{_inline_md(s[2:])}</blockquote>")
        elif s.startswith("- "):
            if not in_ul: html.append("<ul>"); in_ul = True
            html.append(f"<li>{_inline_md(s[2:])}</li>")
        elif s == "---":  close_ul(); html.append("<hr>")
        elif s == "":     close_ul()
        else:             close_ul(); html.append(f"<p>{_inline_md(s)}</p>")
    if in_ul: html.append("</ul>")
    return "\n".join(html)


def _yt_video_link(video_id, label=""):
    if not video_id:
        return ""
    url = f"https://youtube.com/watch?v={video_id}"
    return f"[{label or video_id}]({url})"


def _yt_comment_link(comment_id, video_id="", comment_video_map=None):
    if not comment_id:
        return ""
    if not video_id and comment_video_map:
        video_id = comment_video_map.get(comment_id, "")
    if video_id:
        url = f"https://youtube.com/watch?v={video_id}&lc={comment_id}"
    else:
        url = f"https://youtube.com/watch?lc={comment_id}"
    return f"[comment]({url})"


def _build_markdown(row: dict) -> str:
    a = row["analysis"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    out: list[str] = []
    def ln(*parts):
        out.extend(parts)
        out.append("")
    cnames = row.get("channel_names", {})
    ch_display = ", ".join(cnames.get(ch, ch) for ch in row["channels"])
    ln(f"# YouTube Community Gossip Report",
       f"*Generated: {ts} | Channels: {ch_display}*",
       f"*Date range: {row.get('date_range_start','?')} -> {row.get('date_range_end','?')}*",
       "---")
    ln("## Executive Summary", a.get("executive_summary", "_No summary._"), "---")

    ln("## Reputation Rankings")
    for r in a.get("reputation_rankings", []):
        trend = r.get("trend", "")
        emoji = {"rising": "(+)", "falling": "(-)", "stable": "(=)"}.get(trend, "")
        entity = r.get("entity") or r.get("name") or "?"
        sent = r.get("net_sentiment") or r.get("sentiment") or 0
        reason = r.get("key_reason") or r.get("reason") or ""
        try:
            sent_str = f"{float(sent):.2f}"
        except (TypeError, ValueError):
            sent_str = str(sent)
        ln(f"### {entity}  {emoji}  Sentiment: {sent_str}", reason)
    ln("---")

    ln("## Top Drama Items")
    drama_items = sorted(
        a.get("top_drama_items", []),
        key=lambda x: -(x.get("_evidence_quality") or 0.0),
    )
    for item in drama_items:
        corr = "[OK] Corroborated" if item.get("corroborated") else "[!] Single source"
        conf = (item.get("confidence") or "low").upper()
        subjects = item.get("subjects") or []
        subjects_str = ", ".join(subjects) if isinstance(subjects, list) else str(subjects)
        desc = item.get("description") or item.get("summary") or ""
        ln(f"### {item.get('title', 'Untitled')}", f"*{corr} | Confidence: {conf}*")
        if subjects_str:
            ln(f"**Subjects:** {subjects_str}")
        eq = item.get("_evidence_quality")
        if eq is not None:
            tier = "A" if eq >= 0.65 else "B" if eq >= 0.45 else "C" if eq >= 0.25 else "D"
            warn = " ⚠ Low evidence quality — single low-credibility source" if tier == "D" else ""
            pct = int(eq * 100)
            ln(f"*Evidence quality: Tier {tier} ({pct}%){warn}*")
        ln(desc)
    ln("---")

    ln("## Relationship Map")
    for rel in a.get("relationship_map", []):
        rtype = rel.get("relationship_type", "")
        ea = rel.get("entity_a") or (rel.get("parties", ["?"])[0] if rel.get("parties") else "?")
        eb = rel.get("entity_b") or (rel.get("parties", ["?", "?"])[1] if len(rel.get("parties", [])) > 1 else "?")
        desc = rel.get("description") or ""
        conf = rel.get("confidence", "")
        ln(f"- [{rtype}] **{ea}** <-> **{eb}** -- {desc}" + (f" [{conf}]" if conf else ""))
    ln("---")

    ln("## Persona vs Reality")
    for p in a.get("persona_vs_reality", []):
        entity = p.get("entity") or "?"
        disc = p.get("claimed_discrepancy") or ""
        ln(f"### {entity}", disc)
    ln("---")

    ln("## Community Trends")
    trends = a.get("community_trends", [])
    if isinstance(trends, list):
        for t in trends:
            if isinstance(t, str):
                ln(f"- {t}")
            elif isinstance(t, dict):
                title = t.get("trend") or t.get("title") or ""
                desc = t.get("description") or ""
                if title: ln(f"### {title}")
                if desc: ln(desc)
    ln("---")

    ln("## Power Asymmetries")
    for pa in a.get("cross_mention_asymmetries", [])[:10]:
        ln(f"- **{pa.get('entity', '')}** mentioned by "
           f"{', '.join(pa.get('mentioned_by', []))}")
    ln("---")

    ln("## Corroborated Claims")
    for c in a.get("corroborated_claims", [])[:15]:
        subjects = ", ".join(c.get("subjects") or [])
        occ = c.get("occurrences", 0)
        ln(f"### {(c.get('gossip_type') or '').upper()} -- {subjects}",
           f"*Seen in {occ} videos*")
        for i, claim in enumerate(c.get("claims", []), 1):
            if claim: ln(f"{i}. {claim}")
    ln("---")

    ln("## Top Cross-Channel Commenters")
    for tc in a.get("top_commenters", [])[:10]:
        ln(f"- **{tc.get('author_name', '?')}** -- "
           f"{tc.get('channel_count', 0)} channels, "
           f"{tc.get('comment_count', 0)} comments")
    ln("---")

    ln("## Notable Quotes")
    cvm = row.get("comment_video_map", {})
    for q in a.get("notable_quotes", [])[:10]:
        text = q.get("text") or q.get("quote") or ""
        comment_id = q.get("comment_id") or ""
        video_id = q.get("video_id") or ""
        sig = q.get("significance") or ""
        clink = _yt_comment_link(comment_id, video_id, cvm) if comment_id else ""
        vlink = _yt_video_link(video_id, "video") if video_id else ""
        ref = clink or vlink
        ln(f'> *"{text}"*', f"> -- {ref} {sig}" if (ref or sig) else "")

    return "\n".join(out)


def generate_report_html(conn, analysis_id: int) -> str:
    """Generate a self-contained HTML gossip report and return it."""
    row_data = conn.execute(
        "SELECT * FROM analysis_results WHERE id = ?", (analysis_id,)
    ).fetchone()
    if not row_data:
        raise ValueError(f"Analysis {analysis_id} not found.")

    row = dict(row_data)
    row["analysis"] = _normalise_analysis(_safe_json_loads(row.pop("analysis_json"), {}))
    row["channels"] = _safe_json_loads(row.get("channels_included", "[]"), [])
    row["channel_names"] = {
        r["channel_id"]: r["channel_name"]
        for r in conn.execute("SELECT channel_id, channel_name FROM channels")
    }
    row["comment_video_map"] = {
        r["comment_id"]: r["video_id"]
        for r in conn.execute("SELECT comment_id, video_id FROM comments")
    }

    a = row["analysis"]
    entity_metrics = a.get("entity_metrics", {})
    velocity = a.get("comment_velocity", {})
    top_commenters = a.get("top_commenters", [])

    gossip_rows = [
        dict(r) for r in conn.execute(
            "SELECT gossip_type, confidence, comment_likes_total, "
            "evidence_quality_score, claim FROM gossip_items"
        )
    ]

    # Build claim → evidence_quality_score map for drama item enrichment
    quality_map: dict[str, float] = {}
    for r in gossip_rows:
        if r.get("evidence_quality_score") is not None and r.get("claim"):
            quality_map[r["claim"][:80]] = r["evidence_quality_score"]

    # Enrich top_drama_items with evidence quality scores (best-effort claim matching)
    for item in a.get("top_drama_items", []):
        desc = (item.get("description") or item.get("summary") or "")[:80]
        title = (item.get("title") or "")[:80]
        # Try matching against stored claim text
        eq = quality_map.get(desc) or quality_map.get(title)
        if eq is None:
            # Partial substring search
            for claim_key, score in quality_map.items():
                if claim_key and (claim_key[:40] in desc or desc[:40] in claim_key):
                    eq = score
                    break
        item["_evidence_quality"] = eq

    md = _build_markdown(row)
    sentiment, velocity_c, heatmap, type_pie, conf_c, commenter_c, quality_c = _make_charts(
        entity_metrics, gossip_rows, velocity, top_commenters,
        quality_gossip_rows=gossip_rows,
    )

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    cnames = row.get("channel_names", {})
    channels = _esc(", ".join(cnames.get(ch, ch) for ch in row["channels"]))
    dr_s = _esc(row.get("date_range_start", "?"))
    dr_e = _esc(row.get("date_range_end", "?"))

    def cdiv(svg, full=False):
        if not svg: return ""
        cls = "chart chart-full" if full else "chart"
        return f'<div class="{cls}">{svg}</div>\n'

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        f'<title>Gossip Report -- {ts}</title>\n'
        f'<style>{CSS}</style>\n</head>\n<body>\n'
        '<div class="container">\n'
        '<h1>YouTube Community Gossip Report</h1>\n'
        f'<p class="meta">Generated: {ts} | Channels: {channels}'
        f' | {dr_s} -> {dr_e}</p>\n'
        '<div class="charts">\n'
        + cdiv(sentiment, full=True)
        + cdiv(velocity_c, full=True)
        + cdiv(heatmap, full=True)
        + cdiv(type_pie)
        + cdiv(conf_c)
        + cdiv(quality_c)
        + cdiv(commenter_c, full=True)
        + '</div>\n'
        + _md_to_html(md) + '\n'
        + '</div>\n</body>\n</html>'
    )
