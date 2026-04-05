"""
run_daily.py — Headless daily tracker collection.

Collects channel stats and video snapshots for all communities.
Designed to be run by Windows Task Scheduler (via run_daily.bat).

No Flask dependency — uses core modules directly.

Usage:
    python run_daily.py
    python run_daily.py --db path/to/custom.db
    python run_daily.py --backfill
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "collector.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("run_daily")

from core.db import get_db, get_setting
from core.youtube_api import build_youtube
from core.tracker import collect_all_communities


def main():
    parser = argparse.ArgumentParser(description="Daily tracker collection")
    parser.add_argument("--db", default=None, help="Path to database")
    parser.add_argument("--backfill", action="store_true",
                        help="Full backfill (all videos, not just recent)")
    args = parser.parse_args()

    conn = get_db(args.db)
    api_key = get_setting(conn, "youtube_api_key")
    if not api_key:
        log.error("No YouTube API key configured. Run the web app and set it in Settings.")
        conn.close()
        return

    log.info("Starting daily collection...")
    youtube = build_youtube(api_key)
    collect_all_communities(conn, youtube, backfill=args.backfill)
    conn.close()
    log.info("Daily collection complete.")


if __name__ == "__main__":
    main()
