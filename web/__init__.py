"""
Flask application factory for YouTube Community Analyzer.
"""

from pathlib import Path

from flask import Flask

from core.db import get_db, DEFAULT_DB


def create_app(db_path=None):
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent.parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent.parent / "static"),
    )
    app.config["DB_PATH"] = str(db_path or DEFAULT_DB)
    app.secret_key = "youtube-community-analyzer-local"

    # Ensure DB is initialised; clean up any runs left stuck by a previous crash/restart
    conn = get_db(app.config["DB_PATH"])
    stuck = conn.execute(
        "UPDATE gossip_runs SET status='failed', error_message='Server was restarted', "
        "completed_at=datetime('now') "
        "WHERE status NOT IN ('complete','failed')"
    ).rowcount
    conn.commit()
    if stuck:
        import logging
        logging.getLogger(__name__).warning(
            f"Marked {stuck} stuck gossip run(s) as failed (server restart)."
        )
    conn.close()

    from .routes_main import bp as main_bp
    from .routes_community import bp as community_bp
    from .routes_tracker import bp as tracker_bp
    from .routes_gossip import bp as gossip_bp
    from .routes_settings import bp as settings_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(community_bp, url_prefix="/community")
    app.register_blueprint(tracker_bp, url_prefix="/tracker")
    app.register_blueprint(gossip_bp, url_prefix="/gossip")
    app.register_blueprint(settings_bp, url_prefix="/settings")

    return app
