# YouTube Community Analyzer — User Manual

## Table of Contents

1. [Overview](#1-overview)
2. [First-Time Setup](#2-first-time-setup)
3. [Communities](#3-communities)
4. [Tracker Dashboard](#4-tracker-dashboard)
5. [Daily Collection (Headless)](#5-daily-collection-headless)
6. [Gossip Pipeline](#6-gossip-pipeline)
7. [Settings Reference](#7-settings-reference)
8. [Import from HobbyTracker](#8-import-from-hobbytracker)
9. [Database Reference](#9-database-reference)
10. [Recommended Workflows](#10-recommended-workflows)
11. [Troubleshooting](#11-troubleshooting)
12. [Gossip Theme Tracker](#12-gossip-theme-tracker)

---

## 1. Overview

**YouTube Community Analyzer** is a local web application that:

- **Tracks** channel growth metrics (subscribers, views, videos) over time
- **Collects** YouTube comments across entire communities of channels
- **Extracts** gossip, claims, and entity mentions from comments using an LLM
- **Synthesizes** cross-channel findings into a narrative report

It stores everything in a local SQLite database and runs entirely on your machine (except when calling cloud APIs).

**Tech stack:** Python · Flask · SQLite · YouTube Data API v3 · Anthropic Claude or Ollama (local LLM)

---

## 2. First-Time Setup

### 2.1 Install Dependencies

```bash
pip install -r requirements.txt
```

### 2.2 Start the App

```bash
python app.py
# Options:
python app.py --port 8080          # custom port (default 5000)
python app.py --db mydb.db         # custom database file
python app.py --debug              # enable Flask debug mode (auto-reloads templates)
```

Open `http://127.0.0.1:5000` in your browser.

### 2.3 Configure Your YouTube API Key

Go to **Settings** (top navigation) and enter your **YouTube API key** in the first field.

To get a key:
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → Enable **YouTube Data API v3**
3. Create credentials → API key
4. Paste it in Settings → Save

> **Quota:** The free tier gives 10,000 units/day. A typical collection run costs 3–8 units per channel plus ~1 unit per 50 videos.

---

## 3. Communities

A **community** is a named group of YouTube channels you want to track and analyze together (e.g. "French Non-Duality", "English Non-Duality").

### 3.1 Create a Community

From the home page, click **+ New Community**, enter a name and optional description.

### 3.2 Add Channels

Inside a community's edit page, use the **Add Channel** field. You can enter:
- A handle: `@ChannelName`
- A channel ID: `UCxxxxxxxxxxxxxxxxxxxxx`

The app resolves it via the YouTube API and adds it to the community.

### 3.3 Remove a Channel

Click **Remove** next to any channel in the community edit page. This only unlinks the channel from the community; all collected data is preserved.

### 3.4 Bulk Channel Assignment

**Community → Manage** (`/community/manage`) shows a matrix of all channels × all communities. Use the checkboxes to assign/unassign channels to multiple communities at once. Changes are saved immediately via AJAX.

---

## 4. Tracker Dashboard

**URL:** `/tracker/<community_id>`

The tracker dashboard visualizes channel growth metrics over time.

### 4.1 Stat Tiles (Top Row)

Six tiles display aggregate metrics for the selected channels:

| Tile | Multi-channel | Single channel |
|------|--------------|----------------|
| **Channels** | Count of channels | **Avg Views / Video** |
| **Total Subscribers** | Sum across channels | Same |
| **Total Views** | Sum across channels | Same |
| **Total Videos** | Sum across channels | Same |
| **Channel Age** | Avg age (oldest video) | `YYYY · X yr Y mo` |
| **Avg Videos / Month** | Total from all channels ÷ 6 | That channel's rate |

> Channel Age is based on the **oldest video** in the database, not the account creation date, to avoid inflation from old personal accounts.

### 4.2 Channel Selector (Sidebar)

Use the checkboxes to show/hide individual channels on all charts. **All** and **None** buttons are available. Clicking a single channel name selects only that channel and switches the first tile to "Avg Views / Video".

### 4.3 Charts

| Chart | Description |
|-------|-------------|
| **Subscribers Over Time** | Line chart per channel |
| **Total Views Over Time** | Line chart per channel |
| **Video Count Over Time** | Line chart per channel |
| **Avg Views / Video Over Time** | Line chart per channel |
| **Growth Leaders** | Top N channels ranked by 6 metrics (views growth %, subscriber growth %, avg views/video growth %, engagement rate %, views/subscriber, comment rate %) |
| **Top Videos by Views** | Horizontal bar — top 20 videos across selected channels |
| **Video Duration Distribution** | Doughnut chart bucketed by length |
| **Most Engaging Videos** | Top 15 by like/view ratio |

> **Growth Leaders** require at least **2 snapshots** per channel to appear. New channels will show after the second nightly collection.

### 4.4 Manual Collection Trigger

The **Collect Now** button at the bottom of the dashboard triggers a fresh collection for the community in the background. This is equivalent to running `run_daily.py` for that community.

---

## 5. Daily Collection (Headless)

### 5.1 run_daily.py

Runs the channel metrics collector without the web interface. Designed to be scheduled via Windows Task Scheduler.

```bash
python run_daily.py                    # prioritized daily run
python run_daily.py --db custom.db     # use a different database
python run_daily.py --backfill         # full backfill (fetches ALL videos for all channels)
```

**Log output:** written to `collector.log` in the project directory (and stdout).

### 5.2 Collection Priority Order

The default daily run uses smart prioritization to maximize coverage within the 10,000-unit daily quota:

1. **New channels (no data yet)** — collected first, sorted by subscriber count **ascending** (smallest channels are cheapest to backfill)
2. **Existing channels** — collected next, sorted by **stalest snapshot first**
3. On a `quotaExceeded` error from YouTube, collection stops gracefully and logs how many channels were skipped

### 5.3 Windows Task Scheduler Setup

Use `run_daily.bat` as the scheduled action:

```
Action:  C:\path\to\YouTubeCommunityAnalyzer\run_daily.bat
Trigger: Daily at 02:00 AM
```

The `.bat` file:
```batch
@echo off
cd /d "%~dp0"
python run_daily.py >> collector.log 2>&1
```

All output is appended to `collector.log`.

### 5.4 Backfill

Run `--backfill` when you add many new channels and want to fetch their complete video history in one go:

```bash
python run_daily.py --backfill
```

> This is quota-intensive. A channel with 500 videos costs ~20 units to backfill.

---

## 6. Gossip Pipeline

**URL:** `/gossip/<community_id>`

The gossip pipeline collects YouTube comments and runs LLM analysis to extract discussions, claims, and entity mentions across the community.

### 6.1 Pipeline Steps

| Step | Name | LLM? | Description |
|------|------|------|-------------|
| 1 | **Collect** | No | Fetches comments from YouTube API |
| 2 | **Summarize** | Yes (configurable) | Extracts gossip items per video |
| 3 | **Aggregate** | No | Computes cross-channel metrics |
| 4 | **Analyze** | Yes (configurable) | Narrative LLM synthesis |
| 5 | **Report** | No | Generates HTML report |

### 6.2 Three Run Modes

From the gossip page, three buttons are available when no run is active:

#### Collect Comments
Runs **Step 1 only**. Fetches new YouTube comments into the database. No LLM calls, no API cost beyond the YouTube quota. Safe to run frequently.

#### Run Local Steps
Runs all steps that are configured to use a **local (Ollama) backend**. Stops before any step configured to use the Anthropic cloud API. Logs an amber notice when it stops.

- Example: if `llm_summarize_backend = ollama` and `llm_analyze_backend = anthropic`, it runs Steps 1 → 2 → 3, then stops before Step 4.
- Use this for **daily runs** with a local model.

#### Run Full Analysis
Runs all **5 steps** in sequence, including cloud LLM steps. Use this for **weekly deep analysis** with Claude.

### 6.3 Queuing

If **Collect Comments** is already running, you can still click **Run Local Steps** or **Run Full Analysis** — the new run is accepted as a queued job. It waits (polling every 5 seconds) until collection finishes, then continues automatically.

- A `⏳ Run #X queued` indicator appears below the active run card.
- Queue buttons disappear once a job is already queued.
- Blocked if a full/local pipeline (past the collecting step) is already running.

### 6.4 Progress Log

While a run is active, a dark terminal panel displays a live log updated every 3 seconds:

| Line color | Meaning |
|-----------|---------|
| **Blue** `▶` | Channel header (channel name + video count) |
| White | Per-video line: `@handle  12/188  Video Title  45 fetched  3 new` |
| **Green** count | New comments found for that video |
| **Gray** count | No new comments (already collected) |
| **Bright green** `✓` | Channel done + total new comments |
| **Orange** `⚡` | YouTube quota usage summary |
| **Amber** `ℹ` | Info messages (e.g. "Stopped before analyze — backend is 'anthropic'") |

You can navigate away and return — the log accumulates in the database and is fully visible on return.

### 6.5 Run History

The history table shows the last 20 runs. The **Report** column:
- Shows **View Report** if a full analysis completed
- Shows **comments only** if the run was collect-only or stopped before LLM analysis
- Shows **Failed** (hover for error message) if something went wrong

### 6.6 Server Restart During a Run

If the server is restarted while a run is active, the background thread is killed. On next startup, any stuck runs are automatically marked as **failed** with the message "Server was restarted". Data collected up to that point is preserved in the database. You can immediately start a new run.

### 6.7 Collection Settings

Configured in Settings:

| Setting | Default | Effect |
|---------|---------|--------|
| `max_comments_per_video` | 500 | Cap per video |
| `max_videos_per_channel` | (all) | Limit how many videos are processed |
| `fetch_replies` | true | Include reply threads |
| `date_filter_after` | (none) | Only process videos published after this date (YYYY-MM-DD) |

---

## 7. Settings Reference

**URL:** `/settings`

### YouTube API

| Key | Description |
|-----|-------------|
| `youtube_api_key` | Your Google Cloud YouTube Data API v3 key |

### Anthropic (Cloud LLM)

| Key | Default | Description |
|-----|---------|-------------|
| `anthropic_api_key` | — | Your Anthropic API key (`sk-ant-...`) |

### Ollama (Local LLM)

| Key | Default | Description |
|-----|---------|-------------|
| `ollama_base_url` | `http://localhost:11434` | Ollama server URL |

### LLM — Summarize Step

| Key | Default | Description |
|-----|---------|-------------|
| `llm_summarize_backend` | `ollama` | `ollama` or `anthropic` |
| `llm_summarize_ollama_model` | `mistral-nemo:12b` | Ollama model to use |
| `llm_summarize_anthropic_model` | `claude-haiku-4-5` | Claude model to use |
| `llm_summarize_max_tokens` | `4096` | Max output tokens per LLM call |
| `llm_summarize_buffer_chars` | `120000` | Max input characters per batch |

### LLM — Analyze Step

| Key | Default | Description |
|-----|---------|-------------|
| `llm_analyze_backend` | `anthropic` | `anthropic` or `ollama` |
| `llm_analyze_anthropic_model` | `claude-sonnet-4-6` | Claude model to use |
| `llm_analyze_ollama_model` | `mistral-nemo:12b` | Ollama model to use |
| `llm_analyze_max_tokens` | `16000` | Max output tokens per call |

### LLM — Common

| Key | Default | Description |
|-----|---------|-------------|
| `llm_temperature` | `0.2` | Sampling temperature (0.0–2.0). Lower = more deterministic |

### Gossip Analysis

| Key | Default | Description |
|-----|---------|-------------|
| `gossip_confidence_threshold` | `low` | Minimum confidence to include gossip items: `low`, `medium`, or `high` |
| `report_top_n_entities` | `18` | Number of top entities shown in the report charts |
| `entity_aliases` | `{}` | JSON map for deduplicating entity names: `{"Canonical Name": ["alias1", "alias2"]}` |

---

## 8. Import from HobbyTracker

If you have historical data in the older **HobbyTracker** project (flat JSONL/JSON files), use this script to import it into the database.

### 8.1 Expected HobbyTracker Structure

```
HobbyTracker/
  channels.txt                      ← one JSON object per line: {id, title, handle}
  data/
    <channel_id>/
      channel_snapshots.jsonl       ← daily channel stats
      video_catalog.json            ← latest video metadata
      video_snapshots.jsonl         ← daily per-video stats
```

### 8.2 Run the Import

```bash
# Preview what would be imported (no writes)
python import_from_hobbytracker.py --dry-run

# Import (default: looks for ../HobbyTracker relative to this project)
python import_from_hobbytracker.py

# Specify a custom path
python import_from_hobbytracker.py --hobbytracker C:\path\to\HobbyTracker

# Use a custom database
python import_from_hobbytracker.py --db C:\path\to\custom.db
```

### 8.3 Notes

- **Safe to re-run**: uses `INSERT OR IGNORE` everywhere — no duplicates created.
- Channels do **not** need to be in any community to be imported; data lands in the database and can be linked later via `/community/manage`.
- After import, run `python run_daily.py` once to fetch the latest snapshots for all imported channels.

---

## 9. Database Reference

The SQLite database (`community_analyzer.db` by default) contains the following tables:

| Table | Purpose |
|-------|---------|
| `communities` | Named groups of channels |
| `community_channels` | Many-to-many: which channels belong to which community |
| `channels` | YouTube channel metadata (name, handle, published_at, thumbnail, etc.) |
| `channel_snapshots` | Daily channel stats: subscriber_count, view_count, video_count |
| `videos` | Video metadata: title, published_at, duration, tags, captions |
| `video_snapshots` | Daily per-video stats: view_count, like_count, comment_count |
| `comments` | YouTube comments: author, text, published_at, is_reply, like_count |
| `video_summaries` | Per-video LLM gossip extraction output |
| `gossip_items` | Individual extracted claims/discussions |
| `entity_mentions` | Entity occurrences with sentiment and canonical name |
| `aggregation_results` | Cross-channel aggregated metrics (JSON) |
| `analysis_results` | LLM narrative synthesis output (JSON) |
| `gossip_runs` | Pipeline execution log: status, progress_detail, progress_log |
| `settings` | Key-value configuration store |

Schema migrations run automatically on startup — new columns are added to existing tables as needed.

---

## 10. Recommended Workflows

### 10.1 Daily Metrics Collection (Automated)

Set up Windows Task Scheduler to run `run_daily.bat` every night at 2 AM. This:
- Collects fresh channel snapshots for all communities
- Prioritizes new channels (no data yet) smallest-first
- Stops gracefully if the YouTube quota is exceeded
- Appends to `collector.log`

### 10.2 Daily Comment Collection + Local Summarization

From the gossip page, click **Collect Comments** (or queue **Run Local Steps** while a collect is running):

- **Run Local Steps** collects comments AND runs Ollama-backed steps automatically
- Configure `llm_summarize_backend = ollama` and `llm_analyze_backend = anthropic`
- Local steps run for free; cloud analysis is saved for once a week

### 10.3 Weekly Deep Analysis (Claude)

Once a week, click **Run Full Analysis** on each community. This:
1. Collects any new comments since the last run
2. Runs LLM summarization (Ollama or Claude)
3. Aggregates cross-channel metrics
4. Runs Claude narrative synthesis
5. Generates a full HTML report

### 10.4 Hybrid LLM Setup (Recommended)

```
llm_summarize_backend:  ollama       ← daily, free, local
llm_summarize_model:    mistral-nemo:12b
llm_analyze_backend:    anthropic    ← weekly, cloud, higher quality
llm_analyze_model:      claude-sonnet-4-6
```

- **Run Local Steps** daily → collects + summarizes locally
- **Run Full Analysis** weekly → adds the Claude analysis + report

### 10.5 First-Time Channel Onboarding

1. Add channels to a community via `/community/<id>/edit`
2. Run `python run_daily.py --backfill` once to fetch full video history
3. Click **Collect Comments** to harvest all comments (can take a long time for large channels)
4. Click **Run Full Analysis** to generate the first report

---

## 11. Troubleshooting

### "No YouTube API key configured"
Go to Settings and enter a valid YouTube Data API v3 key.

### "Quota exceeded" during daily collection
The free YouTube API quota is 10,000 units/day. The daily runner stops automatically and logs which channels were skipped. They will be collected the next day. To reduce usage: lower `max_videos_per_channel` in Settings, or run collection less frequently.

### Growth Leaders panel is empty
This panel requires at least **2 snapshots** per channel. New channels appear in Growth Leaders after the second nightly collection run.

### LLM call fails / Ollama not responding
- Ensure Ollama is running: `ollama serve`
- Verify the model is pulled: `ollama pull mistral-nemo:12b`
- Check `ollama_base_url` in Settings matches your Ollama address

### Gossip run stuck / still showing as active after restart
Stuck runs are automatically marked as **failed** when the app restarts. Start a new run from the gossip page.

### Channel shows wrong age
Channel Age is based on the **oldest video currently in the database**, not the YouTube account creation date. As more videos are collected (especially via `--backfill`), the age will update to reflect the true oldest video.

### Import from HobbyTracker: channel not linked to a community
The import script only adds data to the database. Use **Community → Manage** to assign imported channels to the appropriate communities.

---

## 12. Gossip Theme Tracker

**URL:** `/themes/<community_id>`

### 12.1 What is a Theme?

A **theme** is a persistent, named narrative thread that spans multiple videos and channels — for example "The Alice vs Bob rivalry", "X's fake persona allegations", or "The failed collab drama". Instead of seeing gossip scattered across individual video reports, the Theme Tracker lets you follow a story from its first whisper to its most recent flare-up.

Each theme shows:
- A human-readable title and a brief description of the narrative arc (when LLM is enabled)
- The people/channels involved
- A monthly activity chart showing when the community was talking about it most
- The original comments that first surfaced the story
- The most recent comments still discussing it

### 12.2 Prerequisites

Themes are built on top of gossip items. Before using this feature you need:

1. **At least one completed gossip pipeline run** for the community — specifically Steps 1 + 2 (collect + summarize). The theme tracker reads from the `gossip_items` table, which the summarize step populates.
2. **No additional API key** is required for the basic rule-based view. An Anthropic or Ollama key is only needed for LLM-enhanced titles and descriptions (see §12.4).

If no pipeline has ever run, the Themes page shows an empty state with a prompt to run the pipeline first.

### 12.3 How to Access

**From the navigation bar:** A **Themes** link appears in the top nav alongside *Gossip* and *Tracker*. It opens a community selector, or goes directly to the browse page if accessed from within a community context.

**From the Gossip Runs page:** Once any pipeline run has completed, a **View Themes** button appears next to the report link. It goes directly to the Themes browse page for that community.

**Direct URL:** `/themes/<community_id>`

### 12.4 Computing Themes

Themes are calculated on demand — they are not computed automatically when the app starts.

#### Automatic (after every pipeline run)

When a full gossip pipeline run completes, a fast rule-based recompute is triggered automatically in the background. It adds only a few seconds and requires no LLM call. Themes are immediately browsable once the run finishes.

#### Manual recompute

A **Recompute Themes** button sits in the top-right corner of the browse page. Click it at any time. A progress indicator shows while it runs (typically 5–30 seconds).

Reasons to recompute manually:
- You want LLM-enhanced titles and descriptions (automatic recompute skips this)
- You changed entity aliases in Settings and want the new canonical names applied
- You imported historical comments outside the normal pipeline

#### Rule-based vs. LLM-enhanced

| Mode | Titles | Cluster merging | Cost |
|------|--------|-----------------|------|
| Rule-based (default) | Auto-generated: "Alice & Bob — drama" | Exact match only | Free, no API calls |
| LLM-enhanced (manual) | Human-readable: "The Alice vs Bob Feud" | Semantic deduplication of near-identical clusters | Uses the **analyze** LLM backend (Anthropic or Ollama, as configured in Settings) |

The LLM pass uses the same model configured for narrative synthesis (`llm_analyze_backend` / `llm_analyze_anthropic_model`).

### 12.5 The Browse Page

**URL:** `/themes/<community_id>`

**Most Recently Active strip** — At the top, a horizontally scrollable row of 5–8 cards shows the themes with the freshest community activity. Each card includes a mini sparkline (last 12 months of comment counts) to spot which stories are heating up vs. fading.

**Filter bar** — Narrow the grid by gossip type (drama · relationship · collaboration · reputation · irl\_vs\_persona · trend) or by typing a subject name.

**Theme card grid** — All themes for the community, sorted by most recently active first. Each card shows:

| Element | What it means |
|---------|--------------|
| Gossip type badge | Color-coded category |
| Title | Human name (LLM) or auto-generated |
| Subject pills | The people or channels the theme is about |
| Sparkline | Monthly activity over the theme's lifetime |
| Date range | "First seen: Mar 2024 · Last seen: Nov 2024" |
| Evidence count | Total backing comments |

Clicking a card opens the Theme Detail page.

### 12.6 The Theme Detail Page

**URL:** `/themes/detail/<theme_id>`

**Header** — Title, LLM-generated description (if available), subject pills, gossip type badge.

**Stats row** — First seen date · Last seen date · Total evidence comments · Channels involved.

**Activity Timeline** — A full monthly bar chart for the entire lifespan of the theme. Answers: *When did this story peak? Did it resurface after dying down? Is it still active?* Uses the same dark-theme SVG style as the main gossip reports.

**Origin Story** — The 5 earliest evidence comments in chronological order. Each entry shows the date, channel, author, like count, full comment text (expandable), and the gossip claim the pipeline extracted from it. Answers: *How did this story start, and where?*

**Recent Activity** — The 10 most recent evidence comments, newest first. Same format as Origin Story. Answers: *What is the community saying about it right now?*

**Full Timeline** — A collapsible section with all gossip items in chronological order, grouped by month. Each month lists extracted claims and comment excerpts. Useful for tracing the complete arc of a long-running story.

### 12.7 Persistence Across Restarts

**Yes — themes are fully persistent.**

Themes are stored in the `themes` table in the same SQLite database as all other app data (`community_analyzer.db`). They survive:
- App restarts and server reboots
- New pipeline runs (themes are only overwritten when you recompute them explicitly or a pipeline run finishes)

The only way to lose theme data is to delete the database file entirely, which would also delete all comments, gossip items, and reports.

#### What recomputing does to existing data

Recomputing themes replaces all themes for the current community. Themes for other communities are unaffected. The source data (gossip_items, comments) is never modified.

### 12.8 Troubleshooting

**Themes page is empty after a pipeline run.**
The automatic post-pipeline recompute may have been interrupted. Click **Recompute Themes** manually.

**Titles look like "Alice & Bob — drama" instead of something descriptive.**
You are in rule-based mode. Click **Recompute Themes** with an LLM backend configured in Settings.

**Two themes appear to be about the same story.**
In rule-based mode, clusters are grouped by exact subject name match. If the LLM extracted slightly different names for the same person across videos, they become separate clusters. An LLM-enhanced recompute detects the semantic overlap and merges them.

**I changed entity aliases in Settings but themes still show old names.**
Aliases are applied only when themes are recomputed. Click **Recompute Themes**.

**Will recomputing cost me money?**
Rule-based recompute (default, triggered after every pipeline run): no API calls, no cost.
LLM-enhanced recompute: uses the analyze LLM backend. If set to Anthropic, Claude will be billed per-token at the rate for your configured model (same model used for narrative synthesis).
