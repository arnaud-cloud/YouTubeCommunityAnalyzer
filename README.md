# YouTube Community Analyzer

A unified tool for tracking YouTube channel metrics and analyzing community dynamics through comment gossip extraction. Combines daily channel tracking with on-demand LLM-powered comment analysis into a single web interface.

## Features

- **Multiple Communities** — Define independent communities, each with their own set of YouTube channels
- **Channel Metrics Dashboard** — Daily subscriber, view, and video count tracking with interactive Chart.js charts (subscriber growth, rankings, video performance, duration distribution, engagement ratios)
- **Gossip Analysis Pipeline** — 5-step comment analysis: collect comments, LLM gossip extraction, computational aggregation, LLM narrative synthesis, HTML report generation
- **Web GUI** — Dark-themed interface for managing communities, viewing dashboards, triggering analysis, and browsing reports
- **Unified SQLite Database** — All data in one place for long-term retro-analysis
- **Flexible LLM Backends** — Supports Anthropic Claude and local Ollama models, configurable per pipeline step
- **Daily Automation** — Headless runner for Windows Task Scheduler

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Migrate existing data (if you have HobbyTracker / YoutubeGossipCollector data)

```bash
python migrate.py
```

This imports all historical data and creates a default community.

### 3. Start the web interface

```bash
python app.py
```

Open http://127.0.0.1:5000 in your browser.

### 4. Configure API keys

Go to **Settings** and enter your:
- YouTube Data API v3 key ([Google Cloud Console](https://console.cloud.google.com/apis/credentials))
- Anthropic API key (if using Claude for analysis)

### 5. Create a community

Click **+ New Community**, name it, and add channels by their `@handle` or `UC...` channel ID.

## Usage

### Daily Tracking

The tracker collects channel stats and video metadata daily. You can:
- Click **Run Collection Now** in the tracker dashboard
- Set up Windows Task Scheduler to run `run_daily.bat` automatically

### Gossip Analysis

From the **Gossip** page for any community:
1. Click **Run Gossip Analysis**
2. Watch the pipeline progress through 5 steps
3. View the generated report with charts and findings

### Task Scheduler Setup (Windows)

1. Open Task Scheduler
2. Create Basic Task: "YouTube Community Analyzer"
3. Trigger: Daily at your preferred time
4. Action: Start a program
   - Program: `python` (or full path to python.exe)
   - Arguments: `run_daily.py`
   - Start in: `C:\path\to\YouTubeCommunityAnalyzer`

## Project Structure

```
YouTubeCommunityAnalyzer/
├── app.py                    # Flask entry point
├── schema.sql                # Database schema
├── migrate.py                # Import existing data
├── run_daily.py              # Headless daily collection
├── run_daily.bat             # Windows Task Scheduler wrapper
├── requirements.txt
├── core/                     # Business logic (no Flask dependency)
│   ├── db.py                 # Database helpers
│   ├── youtube_api.py        # YouTube Data API v3 helpers
│   ├── tracker.py            # Channel metrics collection
│   ├── llm_client.py         # LLM abstraction (Anthropic + Ollama)
│   ├── entity_resolver.py    # Entity alias resolution
│   ├── gossip_collect.py     # Step 1: Comment collection
│   ├── gossip_summarize.py   # Step 2: LLM gossip extraction
│   ├── gossip_aggregate.py   # Step 3: Computational aggregation
│   ├── gossip_analyze.py     # Step 4: LLM narrative synthesis
│   ├── gossip_report.py      # Step 5: HTML report generation
│   └── gossip_pipeline.py    # Pipeline orchestrator
├── web/                      # Flask routes
│   ├── routes_main.py        # Home page
│   ├── routes_community.py   # Community CRUD
│   ├── routes_tracker.py     # Tracker dashboard
│   ├── routes_gossip.py      # Gossip pipeline
│   └── routes_settings.py    # Settings
├── templates/                # Jinja2 templates
├── static/                   # CSS + JS
└── prompts/                  # LLM prompt templates
```

## API Quota

YouTube Data API v3 has a 10,000 unit/day free quota. Typical usage:
- Daily tracker collection (25 channels): ~200 units
- Gossip comment collection (10 channels × 50 videos): ~2,500 units

Both are well within the free tier.

## LLM Configuration

The gossip pipeline uses LLMs in two steps:

| Step | Default Backend | Default Model | Purpose |
|------|----------------|---------------|---------|
| Summarize (Step 2) | Ollama (local) | mistral-nemo:12b | Per-video gossip extraction |
| Analyze (Step 4) | Anthropic | claude-sonnet-4-6 | Cross-channel narrative synthesis |

Configure via the Settings page. Each step can use a different backend and model.
