"""
migrate.py — One-time import of existing data from HobbyTracker + YoutubeGossipCollector.

Imports:
  1. HobbyTracker JSONL files -> channels, channel_snapshots, videos, video_snapshots
  2. Gossip Collector SQLite -> comments, video_summaries, gossip_items, entity_mentions,
     aggregation_results, analysis_results
  3. config.yaml + .env -> settings table

Creates a default community with the union of all channels from both projects.

Usage:
    python migrate.py
    python migrate.py --hobby-tracker ./HobbyTracker
    python migrate.py --gossip-db "./Youtube Gossip Collector/gossip.db"
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core.db import get_db


def migrate_hobby_tracker(conn, hobby_dir: Path):
    """Import HobbyTracker JSONL data into the unified database."""
    channels_file = hobby_dir / "channels.txt"
    data_dir = hobby_dir / "data"

    if not channels_file.exists():
        print(f"  [skip] No channels.txt found at {channels_file}")
        return []

    # Read channels list
    channel_ids = []
    with open(channels_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                ch = json.loads(line)
                conn.execute(
                    "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle) VALUES (?, ?, ?)",
                    (ch["id"], ch["title"], ch.get("handle", "")),
                )
                channel_ids.append(ch["id"])
            except (json.JSONDecodeError, KeyError) as e:
                print(f"  [warn] Bad line in channels.txt: {e}")

    conn.commit()
    print(f"  Imported {len(channel_ids)} channels from channels.txt")

    if not data_dir.exists():
        print(f"  [skip] No data/ directory found at {data_dir}")
        return channel_ids

    # Process each channel's data directory
    total_snapshots = 0
    total_videos = 0
    total_vid_snaps = 0

    for cid_dir in data_dir.iterdir():
        if not cid_dir.is_dir():
            continue
        cid = cid_dir.name

        # Channel snapshots
        ch_snap_file = cid_dir / "channel_snapshots.jsonl"
        if ch_snap_file.exists():
            with open(ch_snap_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                        conn.execute(
                            """INSERT OR IGNORE INTO channel_snapshots
                                   (channel_id, snapshot_date, subscriber_count,
                                    video_count, view_count, hidden_subscriber, raw_json)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (
                                cid, s.get("snapshot_date", ""),
                                int(s.get("subscriber_count", 0)),
                                int(s.get("video_count", 0)),
                                int(s.get("view_count", 0)),
                                int(s.get("hidden_subscriber", False)),
                                json.dumps(s),
                            ),
                        )
                        total_snapshots += 1
                    except (json.JSONDecodeError, KeyError):
                        pass

        # Video catalog
        cat_file = cid_dir / "video_catalog.json"
        if cat_file.exists():
            with open(cat_file, encoding="utf-8") as f:
                try:
                    catalog = json.load(f)
                    if isinstance(catalog, dict):
                        catalog = list(catalog.values())
                    for v in catalog:
                        conn.execute(
                            """INSERT INTO videos
                                   (video_id, channel_id, title, description, published_at,
                                    duration, tags, category_id, definition, has_captions,
                                    topic_categories, thumbnail_url, privacy_status,
                                    comment_count, collected_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                               ON CONFLICT(video_id) DO UPDATE SET
                                   title = excluded.title,
                                   description = excluded.description,
                                   duration = excluded.duration,
                                   tags = excluded.tags,
                                   category_id = excluded.category_id,
                                   definition = excluded.definition,
                                   has_captions = excluded.has_captions,
                                   topic_categories = excluded.topic_categories,
                                   thumbnail_url = excluded.thumbnail_url""",
                            (
                                v.get("video_id", ""), cid,
                                v.get("title", ""),
                                v.get("description", "")[:1000],
                                v.get("published_at", ""),
                                v.get("duration", "PT0S"),
                                json.dumps(v.get("tags", [])),
                                v.get("category_id", ""),
                                v.get("definition", ""),
                                int(v.get("has_captions", False)),
                                json.dumps(v.get("topic_categories", [])),
                                v.get("thumbnail_url", ""),
                                v.get("privacy_status", ""),
                                int(v.get("comment_count", 0)),
                            ),
                        )
                        total_videos += 1
                except json.JSONDecodeError:
                    print(f"  [warn] Bad JSON in {cat_file}")

        # Video snapshots
        vid_snap_file = cid_dir / "video_snapshots.jsonl"
        if vid_snap_file.exists():
            with open(vid_snap_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                        conn.execute(
                            """INSERT OR IGNORE INTO video_snapshots
                                   (video_id, channel_id, snapshot_date,
                                    view_count, like_count, comment_count)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                s.get("video_id", ""), cid,
                                s.get("snapshot_date", ""),
                                int(s.get("view_count", 0)),
                                int(s.get("like_count", 0)),
                                int(s.get("comment_count", 0)),
                            ),
                        )
                        total_vid_snaps += 1
                    except (json.JSONDecodeError, KeyError):
                        pass

    conn.commit()
    print(f"  Imported {total_snapshots} channel snapshots, "
          f"{total_videos} videos, {total_vid_snaps} video snapshots")
    return channel_ids


def migrate_gossip_db(conn, gossip_db_path: Path):
    """Import data from the existing gossip collector database."""
    if not gossip_db_path.exists():
        print(f"  [skip] Gossip DB not found at {gossip_db_path}")
        return []

    old = sqlite3.connect(str(gossip_db_path))
    old.row_factory = sqlite3.Row

    channel_ids = []

    # Channels
    try:
        rows = old.execute("SELECT * FROM channels").fetchall()
        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle) VALUES (?, ?, ?)",
                (r["channel_id"], r["channel_name"], r["handle"]),
            )
            channel_ids.append(r["channel_id"])
        print(f"  Imported {len(rows)} channels from gossip DB")
    except Exception as e:
        print(f"  [warn] Could not import channels: {e}")

    # Videos (only insert if not already present from HobbyTracker)
    try:
        rows = old.execute("SELECT * FROM videos").fetchall()
        for r in rows:
            conn.execute(
                """INSERT OR IGNORE INTO videos
                       (video_id, channel_id, title, published_at, comment_count, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["video_id"], r["channel_id"], r["title"],
                 r["published_at"], r["comment_count"], r["collected_at"]),
            )
        print(f"  Imported {len(rows)} videos from gossip DB")
    except Exception as e:
        print(f"  [warn] Could not import videos: {e}")

    # Comments
    try:
        rows = old.execute("SELECT * FROM comments").fetchall()
        count = 0
        for r in rows:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO comments
                           (comment_id, video_id, channel_id, author_name,
                            author_channel_id, text, like_count, published_at,
                            is_reply, parent_id, collected_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r["comment_id"], r["video_id"], r["channel_id"],
                     r["author_name"], r["author_channel_id"], r["text"],
                     r["like_count"], r["published_at"], r["is_reply"],
                     r["parent_id"], r["collected_at"]),
                )
                count += 1
            except Exception:
                pass
        print(f"  Imported {count} comments from gossip DB")
    except Exception as e:
        print(f"  [warn] Could not import comments: {e}")

    # Video summaries
    try:
        rows = old.execute("SELECT * FROM video_summaries").fetchall()
        for r in rows:
            conn.execute(
                """INSERT OR IGNORE INTO video_summaries
                       (video_id, channel_id, summary_json, comment_count,
                        gossip_count, processed_at, llm_backend)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r["video_id"], r["channel_id"], r["summary_json"],
                 r["comment_count"], r["gossip_count"],
                 r["processed_at"], r["llm_backend"]),
            )
        print(f"  Imported {len(rows)} video summaries from gossip DB")
    except Exception as e:
        print(f"  [warn] Could not import video_summaries: {e}")

    # Gossip items
    try:
        rows = old.execute("SELECT * FROM gossip_items").fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO gossip_items
                       (video_id, channel_id, gossip_type, subjects, claim,
                        evidence_comment_ids, confidence, external_refs,
                        comment_likes_total, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["video_id"], r["channel_id"], r["gossip_type"],
                 r["subjects"], r["claim"], r["evidence_comment_ids"],
                 r["confidence"], r["external_refs"],
                 r["comment_likes_total"], r["created_at"]),
            )
        print(f"  Imported {len(rows)} gossip items from gossip DB")
    except Exception as e:
        print(f"  [warn] Could not import gossip_items: {e}")

    # Entity mentions
    try:
        rows = old.execute("SELECT * FROM entity_mentions").fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO entity_mentions
                       (entity_name, canonical_name, video_id, channel_id,
                        mention_count, sentiment_score, comment_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r["entity_name"], r["canonical_name"], r["video_id"],
                 r["channel_id"], r["mention_count"],
                 r["sentiment_score"], r["comment_date"]),
            )
        print(f"  Imported {len(rows)} entity mentions from gossip DB")
    except Exception as e:
        print(f"  [warn] Could not import entity_mentions: {e}")

    # Aggregation results
    try:
        rows = old.execute("SELECT * FROM aggregation_results").fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO aggregation_results
                       (channels_included, date_range_start, date_range_end,
                        entity_metrics_json, gossip_corpus_json, corroborated_json,
                        asymmetries_json, comment_velocity_json, top_commenters_json,
                        total_videos, total_gossip_items, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["channels_included"], r["date_range_start"],
                 r["date_range_end"], r["entity_metrics_json"],
                 r["gossip_corpus_json"], r["corroborated_json"],
                 r["asymmetries_json"], r["comment_velocity_json"],
                 r["top_commenters_json"], r["total_videos"],
                 r["total_gossip_items"], r["created_at"]),
            )
        print(f"  Imported {len(rows)} aggregation results from gossip DB")
    except Exception as e:
        print(f"  [warn] Could not import aggregation_results: {e}")

    # Analysis results
    try:
        rows = old.execute("SELECT * FROM analysis_results").fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO analysis_results
                       (aggregation_id, analysis_json, channels_included,
                        date_range_start, date_range_end, created_at, llm_backend)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r["aggregation_id"], r["analysis_json"],
                 r["channels_included"], r["date_range_start"],
                 r["date_range_end"], r["created_at"], r["llm_backend"]),
            )
        print(f"  Imported {len(rows)} analysis results from gossip DB")
    except Exception as e:
        print(f"  [warn] Could not import analysis_results: {e}")

    conn.commit()
    old.close()
    return channel_ids


def migrate_config(conn, gossip_dir: Path, hobby_dir: Path):
    """Import settings from config.yaml, .env files."""
    from core.db import set_setting

    # Try .env files
    env_paths = [
        gossip_dir / ".env",
        hobby_dir / "env.txt",
    ]
    for env_path in env_paths:
        if env_path.exists():
            load_dotenv(dotenv_path=str(env_path))

    yt_key = os.getenv("YOUTUBE_API_KEY", "")
    anth_key = os.getenv("ANTHROPIC_API_KEY", "")

    if yt_key:
        set_setting(conn, "youtube_api_key", yt_key)
        print(f"  Imported YouTube API key")
    if anth_key:
        set_setting(conn, "anthropic_api_key", anth_key)
        print(f"  Imported Anthropic API key")

    # config.yaml
    config_path = gossip_dir / "config.yaml"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        llm = cfg.get("llm", {})
        set_setting(conn, "ollama_base_url", llm.get("ollama_base_url", "http://localhost:11434"))
        set_setting(conn, "llm_temperature", str(llm.get("temperature", 0.2)))

        summarize = llm.get("summarize", {})
        set_setting(conn, "llm_summarize_backend", summarize.get("backend", "ollama"))
        set_setting(conn, "llm_summarize_anthropic_model", summarize.get("anthropic_model", "claude-haiku-4-5"))
        set_setting(conn, "llm_summarize_ollama_model", summarize.get("ollama_model", "mistral-nemo:12b"))
        set_setting(conn, "llm_summarize_max_tokens", str(summarize.get("max_tokens", 4096)))

        analyze = llm.get("analyze", {})
        set_setting(conn, "llm_analyze_backend", analyze.get("backend", "anthropic"))
        set_setting(conn, "llm_analyze_anthropic_model", analyze.get("anthropic_model", "claude-sonnet-4-6"))
        set_setting(conn, "llm_analyze_ollama_model", analyze.get("ollama_model", "mistral-nemo:12b"))
        set_setting(conn, "llm_analyze_max_tokens", str(analyze.get("max_tokens", 16000)))

        yt_cfg = cfg.get("youtube", {})
        set_setting(conn, "max_comments_per_video", str(yt_cfg.get("max_comments_per_video", 500)))
        mvpc = yt_cfg.get("max_videos_per_channel")
        set_setting(conn, "max_videos_per_channel", str(mvpc) if mvpc else "")
        set_setting(conn, "fetch_replies", str(yt_cfg.get("fetch_replies", True)).lower())
        set_setting(conn, "date_filter_after", yt_cfg.get("date_filter_after", "") or "")

        gossip_cfg = cfg.get("gossip", {})
        set_setting(conn, "gossip_confidence_threshold", gossip_cfg.get("confidence_threshold", "low"))

        aliases = cfg.get("entity_aliases") or {}
        set_setting(conn, "entity_aliases", json.dumps(aliases))

        report_cfg = cfg.get("report", {})
        set_setting(conn, "report_top_n_entities", str(report_cfg.get("top_n_entities", 18)))

        print(f"  Imported settings from config.yaml")


def create_default_community(conn, all_channel_ids: list[str]):
    """Create a default community with all imported channels."""
    unique_ids = list(set(all_channel_ids))
    if not unique_ids:
        return

    conn.execute(
        "INSERT OR IGNORE INTO communities (name, description) VALUES (?, ?)",
        ("Nonduality", "French and English nonduality/awakening YouTube community"),
    )
    conn.commit()

    community_id = conn.execute(
        "SELECT id FROM communities WHERE name = ?", ("Nonduality",)
    ).fetchone()["id"]

    for cid in unique_ids:
        conn.execute(
            "INSERT OR IGNORE INTO community_channels (community_id, channel_id) VALUES (?, ?)",
            (community_id, cid),
        )
    conn.commit()
    print(f"  Created community 'Nonduality' with {len(unique_ids)} channels")


def main():
    parser = argparse.ArgumentParser(description="Migrate data into Community Analyzer")
    parser.add_argument("--hobby-tracker", default="HobbyTracker",
                        help="Path to HobbyTracker project directory")
    parser.add_argument("--gossip-dir", default="Youtube Gossip Collector",
                        help="Path to Youtube Gossip Collector project directory")
    parser.add_argument("--gossip-db", default=None,
                        help="Path to gossip.db (default: <gossip-dir>/gossip.db)")
    parser.add_argument("--db", default=None,
                        help="Path to output database")
    args = parser.parse_args()

    base = Path(__file__).parent
    hobby_dir = base / args.hobby_tracker
    gossip_dir = base / args.gossip_dir
    gossip_db_path = Path(args.gossip_db) if args.gossip_db else gossip_dir / "gossip.db"

    print("YouTube Community Analyzer — Migration")
    print("=" * 50)

    conn = get_db(args.db)

    print("\n[1/4] Importing HobbyTracker data...")
    hobby_channels = migrate_hobby_tracker(conn, hobby_dir)

    print("\n[2/4] Importing Gossip Collector data...")
    gossip_channels = migrate_gossip_db(conn, gossip_db_path)

    print("\n[3/4] Importing settings...")
    migrate_config(conn, gossip_dir, hobby_dir)

    print("\n[4/4] Creating default community...")
    create_default_community(conn, hobby_channels + gossip_channels)

    conn.close()
    print("\n" + "=" * 50)
    print("Migration complete! Run 'python app.py' to start the web interface.")


if __name__ == "__main__":
    main()
