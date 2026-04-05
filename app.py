"""
YouTube Community Analyzer — Flask entry point.

Usage:
    python app.py
    python app.py --port 8080
    python app.py --db path/to/custom.db
"""

import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from web import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YouTube Community Analyzer")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--db", default=None, help="Path to SQLite database")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    app = create_app(db_path=args.db)
    app.run(host="127.0.0.1", port=args.port, debug=args.debug)
