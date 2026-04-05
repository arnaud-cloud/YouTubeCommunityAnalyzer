"""Settings routes — API keys, LLM config, gossip parameters."""

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from core.db import get_db, get_all_settings, set_setting

bp = Blueprint("settings", __name__)

# All settings keys with their defaults
SETTINGS_KEYS = {
    "youtube_api_key": "",
    "anthropic_api_key": "",
    "ollama_base_url": "http://localhost:11434",
    "llm_temperature": "0.2",
    "llm_summarize_backend": "ollama",
    "llm_summarize_anthropic_model": "claude-haiku-4-5",
    "llm_summarize_ollama_model": "mistral-nemo:12b",
    "llm_summarize_max_tokens": "4096",
    "llm_analyze_backend": "anthropic",
    "llm_analyze_anthropic_model": "claude-sonnet-4-6",
    "llm_analyze_ollama_model": "mistral-nemo:12b",
    "llm_analyze_max_tokens": "16000",
    "max_comments_per_video": "500",
    "max_videos_per_channel": "",
    "fetch_replies": "true",
    "date_filter_after": "",
    "gossip_confidence_threshold": "low",
    "entity_aliases": "{}",
    "report_top_n_entities": "18",
}


@bp.route("/", methods=["GET", "POST"])
def settings_page():
    conn = get_db(current_app.config["DB_PATH"])

    if request.method == "POST":
        for key in SETTINGS_KEYS:
            value = request.form.get(key, "").strip()
            set_setting(conn, key, value)
        flash("Settings saved.", "success")
        conn.close()
        return redirect(url_for("settings.settings_page"))

    current = get_all_settings(conn)
    conn.close()

    # Merge defaults with current
    settings = {k: current.get(k, v) for k, v in SETTINGS_KEYS.items()}
    return render_template("settings.html", settings=settings)
