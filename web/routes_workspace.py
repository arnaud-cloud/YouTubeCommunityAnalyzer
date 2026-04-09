"""Workspace routes — unified community workspace and creator views."""

import json
import os
import threading

from flask import (
    Blueprint, render_template, current_app, jsonify, redirect,
    url_for, flash, request,
)
from core.db import get_db, get_community_channel_ids

bp = Blueprint("workspace", __name__)

# In-memory job tracker for background collect operations
_collect_jobs: dict[int, dict] = {}  # community_id → {status, detail, channel, ...}


def _format_bytes(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


CREATOR_SORT_COLS = [
    ("quality_score",           "Quality Score"),
    ("llm_tone_score",          "Substance (T)"),
    ("vocab_richness_score",    "Vocab (V)"),
    ("llm_defensiveness_score", "Defensiveness (D)"),
    ("like_ratio_score",        "Like%"),
    ("comment_count",           "Comments"),
    ("total_likes",             "Total Likes"),
]
_VALID_CREATOR_SORTS = {c[0] for c in CREATOR_SORT_COLS}


@bp.route("/<int:community_id>")
def community_workspace(community_id):
    conn = get_db(current_app.config["DB_PATH"])
    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.hub"))

    # Basic community stats
    channel_ids = get_community_channel_ids(conn, community_id)
    total_channels = len(channel_ids)

    total_subs = 0
    if channel_ids:
        ph = ",".join("?" * len(channel_ids))
        row = conn.execute(f"""
            SELECT SUM(cs.subscriber_count) AS total_subs
            FROM channel_snapshots cs
            WHERE cs.channel_id IN ({ph})
            AND cs.snapshot_date = (
                SELECT MAX(cs2.snapshot_date) FROM channel_snapshots cs2
                WHERE cs2.channel_id = cs.channel_id
            )
        """, channel_ids).fetchone()
        total_subs = row["total_subs"] or 0 if row else 0

    # DB size for this community
    db_path = current_app.config["DB_PATH"]
    db_total = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    db_community = 0
    if channel_ids:
        ph = ",".join("?" * len(channel_ids))
        row = conn.execute(
            f"SELECT COALESCE(SUM(LENGTH(text)), 0) AS text_bytes FROM comments WHERE channel_id IN ({ph})",
            channel_ids,
        ).fetchone()
        db_community = int(row["text_bytes"] * 1.5) if row else 0

    # Pipeline status (extended)
    from .routes_gossip import _get_pipeline_status, _get_cost_estimates, _get_preset_availability
    pipeline_status = _get_pipeline_status(conn, community_id)
    cost_estimates = _get_cost_estimates(conn, community_id)
    presets = _get_preset_availability(conn, community_id, cost_estimates)

    # Tracker collection status
    tracker_status = {"state": "never", "timestamp": None, "detail": ""}
    if channel_ids:
        ph = ",".join("?" * len(channel_ids))
        row = conn.execute(
            f"SELECT MAX(snapshot_date) AS last FROM channel_snapshots WHERE channel_id IN ({ph})",
            channel_ids,
        ).fetchone()
        if row and row["last"]:
            tracker_status = {"state": "fresh", "timestamp": row["last"], "detail": f"{total_channels} channels"}

    # Commenter scoring status
    scoring_status = {"state": "never", "timestamp": None, "detail": ""}
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM commenter_scores WHERE community_id = ?",
        (community_id,),
    ).fetchone()
    if row and row["cnt"] > 0:
        scoring_status = {"state": "fresh", "timestamp": None, "detail": f"{row['cnt']} scored"}

    # Tone scoring status
    tone_status = {"state": "never", "timestamp": None, "detail": ""}
    if row and row["cnt"] > 0:
        tone_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM commenter_scores WHERE community_id = ? AND llm_tone_score IS NOT NULL",
            (community_id,),
        ).fetchone()
        if tone_row and tone_row["cnt"] > 0:
            tone_status = {"state": "fresh", "timestamp": None, "detail": f"{tone_row['cnt']} tone-scored"}

    # Active run
    active_run = conn.execute("""
        SELECT * FROM gossip_runs
        WHERE community_id = ? AND status NOT IN ('complete', 'failed', 'pending')
        ORDER BY id DESC LIMIT 1
    """, (community_id,)).fetchone()

    queued_runs = conn.execute("""
        SELECT * FROM gossip_runs
        WHERE community_id = ? AND status = 'pending'
        ORDER BY id
    """, (community_id,)).fetchall()

    # Run history
    history = conn.execute("""
        SELECT gr.*, ar.llm_backend
        FROM gossip_runs gr
        LEFT JOIN analysis_results ar ON gr.analysis_id = ar.id
        WHERE gr.community_id = ?
        ORDER BY gr.id DESC LIMIT 20
    """, (community_id,)).fetchall()

    # Channels list (for sidebar / basic display)
    channels = []
    if channel_ids:
        ph = ",".join("?" * len(channel_ids))
        channels = conn.execute(f"""
            SELECT ch.channel_id, ch.channel_name, ch.handle, ch.thumbnail_url,
                   cs.subscriber_count
            FROM channels ch
            LEFT JOIN channel_snapshots cs ON ch.channel_id = cs.channel_id
                AND cs.snapshot_date = (SELECT MAX(snapshot_date) FROM channel_snapshots WHERE channel_id = ch.channel_id)
            WHERE ch.channel_id IN ({ph})
            ORDER BY cs.subscriber_count DESC NULLS LAST
        """, channel_ids).fetchall()

    # Creator credibility scores (commenter_scores filtered to channel owners)
    creators_sort = request.args.get("csort", "quality_score")
    creators_dir = request.args.get("cdir", "desc")
    if creators_sort not in _VALID_CREATOR_SORTS:
        creators_sort = "quality_score"
    if creators_dir not in ("asc", "desc"):
        creators_dir = "desc"

    creators = []
    if channel_ids:
        ph = ",".join("?" * len(channel_ids))
        sort_expr = f"cs.{creators_sort} {'DESC' if creators_dir == 'desc' else 'ASC'} NULLS LAST"
        creators = conn.execute(f"""
            SELECT cs.*, ch.channel_name, ch.handle, ch.thumbnail_url,
                   snap.subscriber_count
            FROM commenter_scores cs
            JOIN channels ch ON cs.author_channel_id = ch.channel_id
            LEFT JOIN channel_snapshots snap ON ch.channel_id = snap.channel_id
                AND snap.snapshot_date = (SELECT MAX(snapshot_date) FROM channel_snapshots WHERE channel_id = ch.channel_id)
            WHERE cs.community_id = ? AND cs.author_channel_id IN ({ph})
            ORDER BY {sort_expr}
        """, [community_id] + list(channel_ids)).fetchall()

    # Themes (for themes tab)
    themes = conn.execute("""
        SELECT * FROM themes
        WHERE community_id = ?
        ORDER BY last_seen_at DESC
    """, (community_id,)).fetchall()

    # Commenter scores (for commenters tab)
    commenters = conn.execute("""
        SELECT * FROM commenter_scores
        WHERE community_id = ?
        ORDER BY quality_score DESC
    """, (community_id,)).fetchall()

    # Cross-channel superfans (from latest aggregation)
    superfans = []
    agg_row = conn.execute("""
        SELECT top_commenters_json FROM aggregation_results
        WHERE community_id = ? ORDER BY id DESC LIMIT 1
    """, (community_id,)).fetchone()
    if agg_row and agg_row["top_commenters_json"]:
        raw_superfans = json.loads(agg_row["top_commenters_json"])
        sf_aids = [sf.get("author_channel_id", "") for sf in raw_superfans if sf.get("author_channel_id")]

        # Batch-fetch commenter scores
        score_map = {}
        if sf_aids:
            sfph = ",".join("?" * len(sf_aids))
            for row in conn.execute(f"""
                SELECT author_channel_id, quality_score, tier, channel_spread_score,
                       llm_tone_score, llm_politeness_score, llm_constructiveness_score, llm_depth_score
                FROM commenter_scores
                WHERE community_id = ? AND author_channel_id IN ({sfph})
            """, [community_id] + sf_aids).fetchall():
                score_map[row["author_channel_id"]] = dict(row)

        # Batch-fetch evidence counts: get all gossip evidence IDs and all superfan comment IDs
        evidence_counts = {}
        if sf_aids:
            # All comment IDs by superfans
            sfph = ",".join("?" * len(sf_aids))
            author_comments_map = {}
            for row in conn.execute(f"""
                SELECT author_channel_id, comment_id FROM comments
                WHERE author_channel_id IN ({sfph})
            """, sf_aids).fetchall():
                author_comments_map.setdefault(row["author_channel_id"], set()).add(row["comment_id"])

            # All gossip evidence comment IDs for this community
            if channel_ids:
                cph = ",".join("?" * len(channel_ids))
                ev_rows = conn.execute(f"""
                    SELECT evidence_comment_ids FROM gossip_items
                    WHERE channel_id IN ({cph})
                """, channel_ids).fetchall()

                for aid in sf_aids:
                    count = 0
                    acids = author_comments_map.get(aid, set())
                    if acids:
                        for ev_row in ev_rows:
                            if ev_row["evidence_comment_ids"]:
                                try:
                                    eids = json.loads(ev_row["evidence_comment_ids"])
                                    if any(eid in acids for eid in eids):
                                        count += 1
                                except (json.JSONDecodeError, TypeError):
                                    pass
                    evidence_counts[aid] = count

        for sf in raw_superfans:
            aid = sf.get("author_channel_id", "")
            if aid in score_map:
                sf.update(score_map[aid])
            sf["evidence_count"] = evidence_counts.get(aid, 0)
            # Derive role tag
            spread = sf.get("channel_spread_score", 0) or 0
            ch_count = sf.get("channel_count", 0) or 0
            ec = sf["evidence_count"]
            if ec >= 3:
                sf["role"] = "Insider"
            elif total_channels > 0 and ch_count / total_channels >= 0.6 and spread >= 0.5:
                sf["role"] = "Bridge"
            else:
                sf["role"] = "Amplifier"
        superfans = raw_superfans

    # Reports (for reports tab)
    reports = {
        "gossip_report": None,
        "executive_summary": None,
        "top_insights": None,
    }
    # Latest gossip report (from completed run with analysis)
    rpt_row = conn.execute("""
        SELECT gr.id, gr.completed_at, gr.analysis_id
        FROM gossip_runs gr
        WHERE gr.community_id = ? AND gr.status = 'complete' AND gr.analysis_id IS NOT NULL
        ORDER BY gr.id DESC LIMIT 1
    """, (community_id,)).fetchone()
    if rpt_row:
        reports["gossip_report"] = {"run_id": rpt_row["id"], "analysis_id": rpt_row["analysis_id"], "date": rpt_row["completed_at"]}

    exec_row = conn.execute("""
        SELECT id, created_at, report_type FROM executive_reports
        WHERE community_id = ? AND report_type = 'executive_summary'
        ORDER BY created_at DESC LIMIT 1
    """, (community_id,)).fetchone()
    if exec_row:
        reports["executive_summary"] = {"id": exec_row["id"], "date": exec_row["created_at"]}

    insights_row = conn.execute("""
        SELECT id, created_at, report_type FROM executive_reports
        WHERE community_id = ? AND report_type = 'top_insights'
        ORDER BY created_at DESC LIMIT 1
    """, (community_id,)).fetchone()
    if insights_row:
        reports["top_insights"] = {"id": insights_row["id"], "date": insights_row["created_at"]}

    # All communities for the top-bar community switcher
    all_communities = conn.execute("SELECT id, name FROM communities ORDER BY name").fetchall()

    conn.close()

    return render_template("community_workspace.html",
        community=community,
        total_channels=total_channels,
        total_subs=total_subs,
        db_total_display=_format_bytes(db_total),
        db_community_display=_format_bytes(db_community),
        db_size_bytes=db_total,
        db_size_display=_format_bytes(db_total),
        pipeline_status=pipeline_status,
        tracker_status=tracker_status,
        scoring_status=scoring_status,
        tone_status=tone_status,
        cost_estimates=cost_estimates,
        presets=presets,
        active_run=active_run,
        queued_runs=queued_runs,
        history=history,
        channels=channels,
        creators=creators,
        creator_sort_cols=CREATOR_SORT_COLS,
        creators_sort=creators_sort,
        creators_dir=creators_dir,
        themes=themes,
        commenters=commenters,
        superfans=superfans,
        reports=reports,
        all_communities=all_communities,
    )


@bp.route("/<int:community_id>/coverage-timeline")
def coverage_timeline(community_id):
    """JSON data for the content coverage heatmap."""
    conn = get_db(current_app.config["DB_PATH"])
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        conn.close()
        return jsonify({"channels": []})

    ph = ",".join("?" * len(channel_ids))

    # Get channel names
    channels_info = {}
    for row in conn.execute(
        f"SELECT channel_id, channel_name FROM channels WHERE channel_id IN ({ph})",
        channel_ids,
    ).fetchall():
        channels_info[row["channel_id"]] = row["channel_name"]

    # Get video counts per channel per month + comment counts joined in
    data = conn.execute(f"""
        SELECT v.channel_id,
               strftime('%Y-%m', v.published_at) AS month,
               COUNT(DISTINCT v.video_id) AS videos,
               COALESCE(SUM(c.cnt), 0) AS comments
        FROM videos v
        LEFT JOIN (
            SELECT video_id, COUNT(*) AS cnt FROM comments GROUP BY video_id
        ) c ON v.video_id = c.video_id
        WHERE v.channel_id IN ({ph})
          AND v.published_at >= date('now', '-5 years')
        GROUP BY v.channel_id, month
        ORDER BY v.channel_id, month
    """, channel_ids).fetchall()

    # Group by channel
    by_channel = {}
    for row in data:
        ch_id = row["channel_id"]
        if ch_id not in by_channel:
            by_channel[ch_id] = {}
        by_channel[ch_id][row["month"]] = {
            "videos": row["videos"],
            "comments": row["comments"],
        }

    result = []
    for ch_id in channel_ids:
        result.append({
            "channel_id": ch_id,
            "channel_name": channels_info.get(ch_id, ch_id),
            "months": by_channel.get(ch_id, {}),
        })

    conn.close()
    return jsonify({"channels": result})


@bp.route("/<int:community_id>/network-data")
def network_data(community_id):
    """JSON data for the entity relationship network graph."""
    conn = get_db(current_app.config["DB_PATH"])
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        conn.close()
        return jsonify({"nodes": [], "edges": [], "asymmetries": []})

    ph = ",".join("?" * len(channel_ids))

    # Entity nodes from entity_mentions
    entity_rows = conn.execute(f"""
        SELECT canonical_name,
               SUM(mention_count) AS total_mentions,
               AVG(sentiment_score) AS avg_sentiment,
               COUNT(DISTINCT channel_id) AS channel_count,
               COUNT(DISTINCT video_id) AS video_count
        FROM entity_mentions
        WHERE channel_id IN ({ph})
        GROUP BY canonical_name
        ORDER BY total_mentions DESC
        LIMIT 60
    """, channel_ids).fetchall()

    nodes = []
    node_set = set()
    for r in entity_rows:
        name = r["canonical_name"]
        if not name:
            continue
        nodes.append({
            "id": name,
            "mentions": r["total_mentions"] or 0,
            "sentiment": round(r["avg_sentiment"] or 0, 3),
            "channels": r["channel_count"] or 0,
            "videos": r["video_count"] or 0,
        })
        node_set.add(name)

    # Edges from gossip_items co-occurrence (subjects arrays)
    gossip_rows = conn.execute(f"""
        SELECT subjects, gossip_type, confidence, evidence_quality_score
        FROM gossip_items
        WHERE channel_id IN ({ph})
    """, channel_ids).fetchall()

    edge_map = {}  # (a, b) -> {count, types, ...}
    for gr in gossip_rows:
        try:
            subjects = json.loads(gr["subjects"]) if gr["subjects"] else []
        except (json.JSONDecodeError, TypeError):
            continue
        # Normalize to canonical names in our node set
        matched = [s for s in subjects if s in node_set]
        for i in range(len(matched)):
            for j in range(i + 1, len(matched)):
                a, b = tuple(sorted([matched[i], matched[j]]))
                key = (a, b)
                if key not in edge_map:
                    edge_map[key] = {"source": a, "target": b, "count": 0, "types": {}}
                edge_map[key]["count"] += 1
                gtype = gr["gossip_type"] or "other"
                edge_map[key]["types"][gtype] = edge_map[key]["types"].get(gtype, 0) + 1

    edges = list(edge_map.values())
    # Determine dominant type for each edge
    for e in edges:
        if e["types"]:
            e["dominant_type"] = max(e["types"], key=e["types"].get)
        else:
            e["dominant_type"] = "other"

    # Asymmetries from latest aggregation
    asymmetries = []
    agg_row = conn.execute("""
        SELECT asymmetries_json FROM aggregation_results
        WHERE community_id = ? ORDER BY id DESC LIMIT 1
    """, (community_id,)).fetchone()
    if agg_row and agg_row["asymmetries_json"]:
        try:
            asymmetries = json.loads(agg_row["asymmetries_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Sentiment timeline per entity (monthly, using video published_at as proxy)
    sentiment_timeline = {}
    for name in node_set:
        rows = conn.execute(f"""
            SELECT strftime('%Y-%m', v.published_at) AS month,
                   AVG(em.sentiment_score) AS avg_sent,
                   SUM(em.mention_count) AS mentions
            FROM entity_mentions em
            JOIN videos v ON em.video_id = v.video_id
            WHERE em.canonical_name = ? AND em.channel_id IN ({ph})
              AND v.published_at IS NOT NULL
            GROUP BY month ORDER BY month
        """, [name] + list(channel_ids)).fetchall()
        if rows:
            sentiment_timeline[name] = [
                {"month": r["month"], "sentiment": round(r["avg_sent"] or 0, 3), "mentions": r["mentions"] or 0}
                for r in rows if r["month"]
            ]

    # Top claims per entity
    entity_claims = {}
    for name in node_set:
        claims = conn.execute(f"""
            SELECT claim, gossip_type, confidence, evidence_quality_score, comment_likes_total
            FROM gossip_items
            WHERE channel_id IN ({ph}) AND subjects LIKE ?
            ORDER BY COALESCE(evidence_quality_score, 0) DESC, comment_likes_total DESC
            LIMIT 5
        """, list(channel_ids) + [f"%{name}%"]).fetchall()
        if claims:
            entity_claims[name] = [dict(c) for c in claims]

    # Channel breakdown per entity
    entity_channels = {}
    for name in node_set:
        ch_rows = conn.execute(f"""
            SELECT em.channel_id, ch.channel_name, SUM(em.mention_count) AS mentions
            FROM entity_mentions em
            JOIN channels ch ON em.channel_id = ch.channel_id
            WHERE em.canonical_name = ? AND em.channel_id IN ({ph})
            GROUP BY em.channel_id
            ORDER BY mentions DESC
        """, [name] + list(channel_ids)).fetchall()
        if ch_rows:
            entity_channels[name] = [dict(r) for r in ch_rows]

    conn.close()
    return jsonify({
        "nodes": nodes,
        "edges": edges,
        "asymmetries": asymmetries,
        "sentiment_timeline": sentiment_timeline,
        "entity_claims": entity_claims,
        "entity_channels": entity_channels,
    })


@bp.route("/<int:community_id>/pulse-data")
def pulse_data(community_id):
    """JSON data for the community pulse timeline."""
    conn = get_db(current_app.config["DB_PATH"])
    channel_ids = get_community_channel_ids(conn, community_id)
    if not channel_ids:
        conn.close()
        return jsonify({"months": [], "videos": [], "comment_volume": [], "themes": [], "sentiment_events": []})

    ph = ",".join("?" * len(channel_ids))

    # Video releases by month (with channel info)
    video_rows = conn.execute(f"""
        SELECT v.video_id, v.title, v.channel_id, ch.channel_name,
               strftime('%Y-%m', v.published_at) AS month,
               v.published_at
        FROM videos v
        JOIN channels ch ON v.channel_id = ch.channel_id
        WHERE v.channel_id IN ({ph}) AND v.published_at IS NOT NULL
        ORDER BY v.published_at
    """, channel_ids).fetchall()

    videos_by_month = {}
    for v in video_rows:
        m = v["month"]
        if m not in videos_by_month:
            videos_by_month[m] = []
        videos_by_month[m].append({
            "title": v["title"],
            "channel": v["channel_name"],
            "channel_id": v["channel_id"],
            "date": v["published_at"][:10] if v["published_at"] else "",
        })

    # Comment volume by month
    comment_rows = conn.execute(f"""
        SELECT strftime('%Y-%m', c.published_at) AS month,
               COUNT(*) AS count
        FROM comments c
        WHERE c.channel_id IN ({ph}) AND c.published_at IS NOT NULL
        GROUP BY month ORDER BY month
    """, channel_ids).fetchall()
    comment_volume = {r["month"]: r["count"] for r in comment_rows}

    # Theme activity (from themes.activity_json)
    theme_rows = conn.execute("""
        SELECT id, title, gossip_type, activity_json, total_evidence
        FROM themes WHERE community_id = ?
        ORDER BY total_evidence DESC LIMIT 10
    """, (community_id,)).fetchall()

    themes_data = []
    for t in theme_rows:
        activity = {}
        if t["activity_json"]:
            try:
                activity = json.loads(t["activity_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        themes_data.append({
            "id": t["id"],
            "title": t["title"] or "Untitled",
            "type": t["gossip_type"],
            "activity": activity,
            "total_evidence": t["total_evidence"] or 0,
        })

    # Entity sentiment events (monthly sentiment for top entities)
    top_entities = conn.execute(f"""
        SELECT canonical_name, SUM(mention_count) AS total
        FROM entity_mentions WHERE channel_id IN ({ph})
        GROUP BY canonical_name ORDER BY total DESC LIMIT 8
    """, channel_ids).fetchall()

    sentiment_events = []
    for ent in top_entities:
        name = ent["canonical_name"]
        if not name:
            continue
        rows = conn.execute(f"""
            SELECT strftime('%Y-%m', v.published_at) AS month,
                   AVG(em.sentiment_score) AS avg_sent
            FROM entity_mentions em
            JOIN videos v ON em.video_id = v.video_id
            WHERE em.canonical_name = ? AND em.channel_id IN ({ph})
              AND v.published_at IS NOT NULL
            GROUP BY month ORDER BY month
        """, [name] + list(channel_ids)).fetchall()
        monthly = {r["month"]: round(r["avg_sent"] or 0, 3) for r in rows if r["month"]}
        if monthly:
            sentiment_events.append({"entity": name, "monthly": monthly})

    # Build unified month list
    all_months = set()
    all_months.update(videos_by_month.keys())
    all_months.update(comment_volume.keys())
    for t in themes_data:
        all_months.update(t["activity"].keys())
    for se in sentiment_events:
        all_months.update(se["monthly"].keys())
    months_sorted = sorted(all_months)

    # Channel list for legend
    channels_info = conn.execute(f"""
        SELECT channel_id, channel_name FROM channels WHERE channel_id IN ({ph})
    """, channel_ids).fetchall()

    conn.close()
    return jsonify({
        "months": months_sorted,
        "videos_by_month": videos_by_month,
        "comment_volume": comment_volume,
        "themes": themes_data,
        "sentiment_events": sentiment_events,
        "channels": [dict(c) for c in channels_info],
    })


@bp.route("/<int:community_id>/collect-range", methods=["POST"])
def collect_range(community_id):
    """Trigger a targeted date-range collection for a specific channel."""
    channel_id = request.json.get("channel_id")
    date_from = request.json.get("date_from")
    date_to = request.json.get("date_to")
    channel_name = request.json.get("channel_name", channel_id)

    if not channel_id or not date_from:
        return jsonify({"error": "channel_id and date_from are required"}), 400

    # Don't start if already collecting for this community
    existing = _collect_jobs.get(community_id, {})
    if existing.get("status") == "running":
        return jsonify({"error": "A collection is already running"}), 409

    db_path = current_app.config["DB_PATH"]
    import time
    _collect_jobs[community_id] = {
        "status": "running", "type": "gap",
        "channel_name": channel_name, "date_from": date_from,
        "detail": f"Collecting {channel_name} from {date_from}...",
        "progress_log": "", "started_at": time.time(),
    }

    def _progress(msg):
        _collect_jobs[community_id]["progress_log"] += msg + "\n"
        parts = msg.split("\t")
        if parts[0] == "video":
            _collect_jobs[community_id]["detail"] = f"{parts[1]} — video {parts[2]}: {parts[3]}"
        elif parts[0] == "done":
            _collect_jobs[community_id]["detail"] = f"{parts[1]} — {parts[2]}"

    def _run():
        from core.db import get_db as _get_db, get_setting
        from core.gossip_collect import collect_channel_comments
        from core.youtube_api import build_youtube, QuotaTracker
        conn = _get_db(db_path)
        try:
            api_key = get_setting(conn, "youtube_api_key")
            if not api_key:
                _collect_jobs[community_id]["status"] = "error"
                _collect_jobs[community_id]["detail"] = "No YouTube API key configured"
                return
            youtube = build_youtube(api_key)
            max_comments = int(get_setting(conn, "max_comments_per_video") or "500")
            fetch_replies = (get_setting(conn, "fetch_replies") or "true").lower() == "true"
            quota = QuotaTracker()
            collect_channel_comments(
                conn, youtube, channel_id,
                max_videos=None, after=date_from,
                max_comments=max_comments, fetch_replies=fetch_replies,
                quota=quota, progress_callback=_progress,
            )
            _collect_jobs[community_id]["status"] = "done"
        except Exception as e:
            _collect_jobs[community_id]["status"] = "error"
            _collect_jobs[community_id]["detail"] = str(e)
        finally:
            conn.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return jsonify({"ok": True, "message": f"Collection started for {channel_name} from {date_from}"})


@bp.route("/<int:community_id>/collect-status")
def collect_status(community_id):
    """JSON status for in-progress collect jobs."""
    job = _collect_jobs.get(community_id, {"status": "idle"})
    return jsonify(job)


@bp.route("/<int:community_id>/channel/<path:channel_id>")
def creator_view(community_id, channel_id):
    """Unified creator/channel view."""
    conn = get_db(current_app.config["DB_PATH"])

    community = conn.execute(
        "SELECT * FROM communities WHERE id = ?", (community_id,)
    ).fetchone()
    if not community:
        conn.close()
        flash("Community not found.", "error")
        return redirect(url_for("main.hub"))

    channel = conn.execute(
        "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    if not channel:
        conn.close()
        flash("Channel not found.", "error")
        return redirect(url_for("workspace.community_workspace", community_id=community_id))

    # Latest snapshot
    snapshot = conn.execute("""
        SELECT * FROM channel_snapshots
        WHERE channel_id = ? ORDER BY snapshot_date DESC LIMIT 1
    """, (channel_id,)).fetchone()

    # All snapshots for charts
    snapshots = conn.execute("""
        SELECT * FROM channel_snapshots
        WHERE channel_id = ? ORDER BY snapshot_date
    """, (channel_id,)).fetchall()

    # Videos
    videos = conn.execute("""
        SELECT v.*, vs.view_count AS latest_views, vs.like_count AS latest_likes,
               vs.comment_count AS latest_comments,
               CASE WHEN vsum.video_id IS NOT NULL THEN 1 ELSE 0 END AS has_summary,
               (SELECT COUNT(*) FROM gossip_items gi WHERE gi.video_id = v.video_id) AS gossip_count
        FROM videos v
        LEFT JOIN video_snapshots vs ON v.video_id = vs.video_id
            AND vs.snapshot_date = (SELECT MAX(snapshot_date) FROM video_snapshots WHERE video_id = v.video_id)
        LEFT JOIN video_summaries vsum ON v.video_id = vsum.video_id
        WHERE v.channel_id = ?
        ORDER BY v.published_at DESC
    """, (channel_id,)).fetchall()

    # Gossip items about this channel/entity
    channel_name = channel["channel_name"]
    gossip_items = conn.execute("""
        SELECT gi.*, v.title AS video_title, ch.channel_name AS source_channel
        FROM gossip_items gi
        JOIN videos v ON gi.video_id = v.video_id
        JOIN channels ch ON gi.channel_id = ch.channel_id
        WHERE gi.subjects LIKE ?
        ORDER BY gi.confidence DESC, gi.comment_likes_total DESC
        LIMIT 50
    """, (f"%{channel_name}%",)).fetchall()

    # Themes involving this entity
    themes = conn.execute("""
        SELECT * FROM themes
        WHERE community_id = ? AND subjects LIKE ?
        ORDER BY last_seen_at DESC
    """, (community_id, f"%{channel_name}%")).fetchall()

    # Video snapshots time series (for engagement charts)
    video_ids = [v["video_id"] for v in videos]
    video_snapshots_data = []
    if video_ids:
        # Process in batches to avoid too many SQL params
        for i in range(0, len(video_ids), 500):
            batch = video_ids[i:i+500]
            vph = ",".join("?" * len(batch))
            video_snapshots_data.extend([dict(r) for r in conn.execute(f"""
                SELECT vs.video_id, vs.channel_id, vs.snapshot_date,
                       vs.view_count, vs.like_count, vs.comment_count
                FROM video_snapshots vs
                WHERE vs.video_id IN ({vph})
                ORDER BY vs.snapshot_date
            """, batch).fetchall()])

    # Commenter score (if this channel owner comments)
    commenter_score = conn.execute("""
        SELECT * FROM commenter_scores
        WHERE community_id = ? AND author_channel_id = ?
    """, (community_id, channel_id)).fetchone()
    tone_score = dict(commenter_score) if commenter_score else None

    # All communities for topbar
    all_communities = conn.execute("SELECT id, name FROM communities ORDER BY name").fetchall()

    db_path = current_app.config["DB_PATH"]
    db_total = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    conn.close()

    return render_template("creator_view.html",
        community=community,
        channel=channel,
        snapshot=snapshot,
        snapshots=[dict(s) for s in snapshots],
        videos=[dict(v) for v in videos],
        video_snapshots=video_snapshots_data,
        gossip_items=gossip_items,
        themes=themes,
        commenter_score=commenter_score,
        tone_score=tone_score,
        all_communities=all_communities,
        db_size_bytes=db_total,
        db_size_display=_format_bytes(db_total),
    )
