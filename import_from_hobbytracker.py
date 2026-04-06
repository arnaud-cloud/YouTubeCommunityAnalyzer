"""
import_from_hobbytracker.py — Import historical data from the HobbyTracker project.

HobbyTracker stores data as flat files under data/<channel_id>/:
  channel_snapshots.jsonl  — one JSON line per daily channel-level snapshot
  video_catalog.json       — full video metadata (latest state per video)
  video_snapshots.jsonl    — one JSON line per daily per-video snapshot

This script reads those files and inserts the data into this project's SQLite DB.
Channels must already exist in community_channels (added via the web UI) to be linked
to a community, but data is imported regardless so nothing is lost.

Usage:
    python import_from_hobbytracker.py
    python import_from_hobbytracker.py --hobbytracker C:\\path\\to\\HobbyTracker
    python import_from_hobbytracker.py --db path\\to\\custom.db
    python import_from_hobbytracker.py --dry-run
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("import_ht")

from core.db import get_db

DEFAULT_HT_DIR = Path(__file__).parent.parent / "HobbyTracker"


def load_channels_txt(ht_dir: Path) -> list[dict]:
    """Read channels.txt — one JSON object per line."""
    path = ht_dir / "channels.txt"
    if not path.exists():
        log.error(f"channels.txt not found at {path}")
        return []
    channels = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    channels.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning(f"Skipping malformed line in channels.txt: {line[:80]}")
    return channels


def import_channel(conn, channel_meta: dict, data_dir: Path, dry_run: bool) -> dict:
    """
    Import all historical data for one channel.
    Returns counts of rows imported.
    """
    cid = channel_meta["id"]
    title = channel_meta.get("title", cid)
    cdir = data_dir / cid

    counts = {"channel_snapshots": 0, "videos": 0, "video_snapshots": 0}

    if not cdir.exists():
        log.warning(f"  {title}: data directory not found, skipping")
        return counts

    # ── 1. Channel snapshots ───────────────────────────────────────────────────
    snap_path = cdir / "channel_snapshots.jsonl"
    channel_row_written = False

    if snap_path.exists():
        snapshots = []
        with open(snap_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snapshots.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        # Upsert channels table from the latest snapshot
        if snapshots:
            latest = max(snapshots, key=lambda s: s.get("snapshot_date", ""))
            if not dry_run:
                conn.execute(
                    """INSERT INTO channels
                           (channel_id, channel_name, handle, description, custom_url,
                            country, published_at, thumbnail_url, keywords, topic_categories)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(channel_id) DO UPDATE SET
                           channel_name = excluded.channel_name,
                           handle = excluded.handle,
                           description = excluded.description,
                           custom_url = excluded.custom_url,
                           country = excluded.country,
                           published_at = excluded.published_at,
                           thumbnail_url = excluded.thumbnail_url,
                           keywords = excluded.keywords,
                           topic_categories = excluded.topic_categories""",
                    (
                        cid,
                        latest.get("title", title),
                        latest.get("custom_url", channel_meta.get("handle", "")),
                        latest.get("description", ""),
                        latest.get("custom_url", ""),
                        latest.get("country", ""),
                        latest.get("published_at", ""),
                        latest.get("thumbnail_medium", latest.get("thumbnail_default", "")),
                        latest.get("keywords", ""),
                        json.dumps(latest.get("topic_categories", [])),
                    ),
                )
                channel_row_written = True

        for s in snapshots:
            snap_date = s.get("snapshot_date", "")
            if not snap_date:
                continue
            if not dry_run:
                conn.execute(
                    """INSERT OR IGNORE INTO channel_snapshots
                           (channel_id, snapshot_date, subscriber_count,
                            video_count, view_count, hidden_subscriber, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        cid,
                        snap_date,
                        s.get("subscriber_count", 0),
                        s.get("video_count", 0),
                        s.get("view_count", 0),
                        int(bool(s.get("hidden_subscriber", False))),
                        json.dumps(s),
                    ),
                )
            counts["channel_snapshots"] += 1
    else:
        log.warning(f"  {title}: no channel_snapshots.jsonl")

    # Ensure the channel row exists even if no snapshots
    if not channel_row_written and not dry_run:
        conn.execute(
            "INSERT OR IGNORE INTO channels (channel_id, channel_name, handle) VALUES (?, ?, ?)",
            (cid, title, channel_meta.get("handle", "")),
        )

    # ── 2. Video catalog ───────────────────────────────────────────────────────
    cat_path = cdir / "video_catalog.json"
    if cat_path.exists():
        with open(cat_path, encoding="utf-8") as f:
            catalog = json.load(f)
        if isinstance(catalog, dict):
            catalog = list(catalog.values())

        for v in catalog:
            if not dry_run:
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
                           thumbnail_url = excluded.thumbnail_url,
                           privacy_status = excluded.privacy_status,
                           comment_count = excluded.comment_count,
                           collected_at = datetime('now')""",
                    (
                        v["video_id"],
                        v.get("channel_id", cid),
                        v.get("title", ""),
                        v.get("description", "")[:1000],
                        v.get("published_at", ""),
                        v.get("duration", "PT0S"),
                        json.dumps(v.get("tags", [])),
                        v.get("category_id", ""),
                        v.get("definition", ""),
                        int(bool(v.get("has_captions", False))),
                        json.dumps(v.get("topic_categories", [])),
                        v.get("thumbnail_url", ""),
                        v.get("privacy_status", ""),
                        v.get("comment_count", 0),
                    ),
                )
            counts["videos"] += 1
    else:
        log.warning(f"  {title}: no video_catalog.json")

    # ── 3. Video snapshots ─────────────────────────────────────────────────────
    vs_path = cdir / "video_snapshots.jsonl"
    if vs_path.exists():
        with open(vs_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    s = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not dry_run:
                    conn.execute(
                        """INSERT OR IGNORE INTO video_snapshots
                               (video_id, channel_id, snapshot_date,
                                view_count, like_count, comment_count)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            s["video_id"],
                            cid,
                            s["snapshot_date"],
                            s.get("view_count", 0),
                            s.get("like_count", 0),
                            s.get("comment_count", 0),
                        ),
                    )
                counts["video_snapshots"] += 1
    else:
        log.warning(f"  {title}: no video_snapshots.jsonl")

    if not dry_run:
        conn.commit()

    return counts


def main():
    parser = argparse.ArgumentParser(description="Import HobbyTracker data into this project's DB")
    parser.add_argument("--hobbytracker", default=str(DEFAULT_HT_DIR),
                        help=f"Path to HobbyTracker project directory (default: {DEFAULT_HT_DIR})")
    parser.add_argument("--db", default=None, help="Path to this project's database")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows without writing anything")
    args = parser.parse_args()

    ht_dir = Path(args.hobbytracker)
    if not ht_dir.exists():
        log.error(f"HobbyTracker directory not found: {ht_dir}")
        sys.exit(1)

    channels = load_channels_txt(ht_dir)
    if not channels:
        log.error("No channels found in channels.txt")
        sys.exit(1)

    log.info(f"Found {len(channels)} channels in HobbyTracker")
    if args.dry_run:
        log.info("DRY RUN — no data will be written")

    conn = get_db(args.db)
    data_dir = ht_dir / "data"

    total = {"channel_snapshots": 0, "videos": 0, "video_snapshots": 0}

    for ch in channels:
        log.info(f"Importing: {ch.get('title', ch['id'])} ({ch['id']})")
        counts = import_channel(conn, ch, data_dir, dry_run=args.dry_run)
        for k, v in counts.items():
            total[k] += v
        log.info(
            f"  -> {counts['channel_snapshots']} channel snapshots, "
            f"{counts['videos']} videos, "
            f"{counts['video_snapshots']} video snapshots"
        )

    conn.close()

    action = "Would import" if args.dry_run else "Imported"
    log.info(
        f"\n{action} total: "
        f"{total['channel_snapshots']} channel snapshots, "
        f"{total['videos']} videos, "
        f"{total['video_snapshots']} video snapshots"
    )


if __name__ == "__main__":
    main()
