-- YouTube Community Analyzer — Unified Database Schema

-- ═══ COMMUNITY LAYER ══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS communities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Legacy table — kept for backward compat; new code writes community_sources.
CREATE TABLE IF NOT EXISTS community_channels (
    community_id    INTEGER NOT NULL,
    channel_id      TEXT NOT NULL,
    added_at        TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (community_id, channel_id),
    FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);

-- Multi-platform source registry
CREATE TABLE IF NOT EXISTS community_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id    INTEGER NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'youtube',  -- 'youtube' | 'reddit' | ...
    source_id       TEXT NOT NULL,                    -- channel_id for YT, 'r/name' for Reddit
    display_name    TEXT DEFAULT '',
    config_json     TEXT DEFAULT '{}',
    added_at        TEXT DEFAULT (datetime('now')),
    UNIQUE(community_id, source_type, source_id),
    FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE
);

-- ═══ CHANNELS (shared by tracker + gossip) ════════════════════════════════════

CREATE TABLE IF NOT EXISTS channels (
    channel_id          TEXT PRIMARY KEY,
    channel_name        TEXT NOT NULL,
    handle              TEXT,
    description         TEXT DEFAULT '',
    custom_url          TEXT DEFAULT '',
    country             TEXT DEFAULT '',
    published_at        TEXT,
    thumbnail_url       TEXT DEFAULT '',
    keywords            TEXT DEFAULT '',
    topic_categories    TEXT DEFAULT '[]',
    added_at            TEXT DEFAULT (datetime('now'))
);

-- ═══ TRACKER DATA ═════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS channel_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id          TEXT NOT NULL,
    snapshot_date       TEXT NOT NULL,
    subscriber_count    INTEGER DEFAULT 0,
    video_count         INTEGER DEFAULT 0,
    view_count          INTEGER DEFAULT 0,
    hidden_subscriber   INTEGER DEFAULT 0,
    raw_json            TEXT,
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id),
    UNIQUE(channel_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    title               TEXT,
    description         TEXT DEFAULT '',
    published_at        TEXT,
    duration            TEXT DEFAULT 'PT0S',
    tags                TEXT DEFAULT '[]',
    category_id         TEXT DEFAULT '',
    definition          TEXT DEFAULT '',
    has_captions        INTEGER DEFAULT 0,
    topic_categories    TEXT DEFAULT '[]',
    thumbnail_url       TEXT DEFAULT '',
    privacy_status      TEXT DEFAULT '',
    comment_count       INTEGER DEFAULT 0,
    collected_at        TEXT,
    source_type         TEXT DEFAULT 'youtube',
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);

CREATE TABLE IF NOT EXISTS video_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id            TEXT NOT NULL,
    channel_id          TEXT NOT NULL,
    snapshot_date       TEXT NOT NULL,
    view_count          INTEGER DEFAULT 0,
    like_count          INTEGER DEFAULT 0,
    comment_count       INTEGER DEFAULT 0,
    FOREIGN KEY (video_id) REFERENCES videos(video_id),
    UNIQUE(video_id, snapshot_date)
);

-- ═══ GOSSIP DATA ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS comments (
    comment_id              TEXT PRIMARY KEY,
    video_id                TEXT NOT NULL,
    channel_id              TEXT NOT NULL,
    author_name             TEXT,
    author_channel_id       TEXT,
    text                    TEXT,
    like_count              INTEGER DEFAULT 0,
    published_at            TEXT,
    is_reply                INTEGER DEFAULT 0,
    parent_id               TEXT,
    collected_at            TEXT DEFAULT (datetime('now')),
    source_type             TEXT DEFAULT 'youtube',
    engagement_normalized   REAL,
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

CREATE TABLE IF NOT EXISTS video_summaries (
    video_id                    TEXT PRIMARY KEY,
    channel_id                  TEXT NOT NULL,
    summary_json                TEXT,
    comment_count               INTEGER,
    gossip_count                INTEGER,
    processed_at                TEXT DEFAULT (datetime('now')),
    llm_backend                 TEXT,
    last_comment_published_at   TEXT,
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

CREATE TABLE IF NOT EXISTS gossip_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id            TEXT NOT NULL,
    channel_id          TEXT NOT NULL,
    gossip_type         TEXT,
    subjects            TEXT,
    claim               TEXT,
    evidence_comment_ids TEXT,
    confidence          TEXT,
    external_refs       TEXT,
    comment_likes_total INTEGER DEFAULT 0,
    created_at          TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

CREATE TABLE IF NOT EXISTS entity_mentions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_name         TEXT NOT NULL,
    canonical_name      TEXT,
    video_id            TEXT NOT NULL,
    channel_id          TEXT NOT NULL,
    mention_count       INTEGER DEFAULT 1,
    sentiment_score     REAL,
    comment_date        TEXT,
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

CREATE TABLE IF NOT EXISTS aggregation_results (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id          INTEGER,
    channels_included     TEXT,
    date_range_start      TEXT,
    date_range_end        TEXT,
    entity_metrics_json   TEXT,
    gossip_corpus_json    TEXT,
    corroborated_json     TEXT,
    asymmetries_json      TEXT,
    comment_velocity_json TEXT,
    top_commenters_json   TEXT,
    total_videos          INTEGER,
    total_gossip_items    INTEGER,
    created_at            TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (community_id) REFERENCES communities(id)
);

CREATE TABLE IF NOT EXISTS analysis_results (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregation_id        INTEGER NOT NULL,
    community_id          INTEGER,
    analysis_json         TEXT,
    channels_included     TEXT,
    date_range_start      TEXT,
    date_range_end        TEXT,
    created_at            TEXT DEFAULT (datetime('now')),
    llm_backend           TEXT,
    FOREIGN KEY (aggregation_id) REFERENCES aggregation_results(id),
    FOREIGN KEY (community_id) REFERENCES communities(id)
);

-- ═══ PIPELINE RUNS ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS gossip_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id        INTEGER NOT NULL,
    status              TEXT DEFAULT 'pending',
    current_step        TEXT DEFAULT '',
    progress_detail     TEXT DEFAULT '',
    started_at          TEXT DEFAULT (datetime('now')),
    completed_at        TEXT,
    analysis_id         INTEGER,
    error_message       TEXT,
    FOREIGN KEY (community_id) REFERENCES communities(id),
    FOREIGN KEY (analysis_id) REFERENCES analysis_results(id)
);

-- ═══ GOSSIP THEMES ════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS themes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id    INTEGER NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    gossip_type     TEXT,
    subjects        TEXT NOT NULL DEFAULT '[]',
    gossip_item_ids TEXT NOT NULL DEFAULT '[]',
    first_seen_at   TEXT,
    last_seen_at    TEXT,
    activity_json   TEXT DEFAULT '{}',
    total_evidence  INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    llm_backend     TEXT,
    FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_themes_community  ON themes(community_id);
CREATE INDEX IF NOT EXISTS idx_themes_last_seen  ON themes(last_seen_at);

-- ═══ COMMENTER CREDIBILITY SCORES ════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS commenter_scores (
    community_id         INTEGER NOT NULL,
    author_channel_id    TEXT NOT NULL,
    author_name          TEXT,
    quality_score        REAL NOT NULL,
    tier                 TEXT NOT NULL,          -- 'A' | 'B' | 'C' | 'D'
    avg_engagement_norm  REAL,
    channel_spread_score REAL,
    like_ratio_score     REAL,
    factual_anchor_score REAL,
    avg_length_score     REAL,
    reply_penalty        REAL,
    reply_ratio          REAL,
    comment_count        INTEGER,
    channel_count        INTEGER,
    total_likes          INTEGER,
    computed_at          TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (community_id, author_channel_id),
    FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_commenter_scores_tier
    ON commenter_scores(community_id, tier);

-- ═══ EXECUTIVE REPORTS (cached LLM-generated summaries) ══════════════════════

CREATE TABLE IF NOT EXISTS executive_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    community_id    INTEGER NOT NULL,
    report_type     TEXT NOT NULL DEFAULT 'executive_summary',
    min_evidence    INTEGER NOT NULL DEFAULT 0,
    report_html     TEXT NOT NULL,
    report_json     TEXT,
    llm_backend     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_exec_reports_community ON executive_reports(community_id);

-- ═══ SETTINGS ═════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS settings (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL
);

-- ═══ INDEXES ══════════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_channel_snapshots_cid    ON channel_snapshots(channel_id);
CREATE INDEX IF NOT EXISTS idx_channel_snapshots_date   ON channel_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_video_snapshots_vid      ON video_snapshots(video_id);
CREATE INDEX IF NOT EXISTS idx_video_snapshots_date     ON video_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_videos_channel           ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_comments_video           ON comments(video_id);
CREATE INDEX IF NOT EXISTS idx_comments_author          ON comments(author_channel_id);
CREATE INDEX IF NOT EXISTS idx_comments_date            ON comments(published_at);
CREATE INDEX IF NOT EXISTS idx_gossip_type              ON gossip_items(gossip_type);
CREATE INDEX IF NOT EXISTS idx_gossip_channel           ON gossip_items(channel_id);
CREATE INDEX IF NOT EXISTS idx_entity_canonical         ON entity_mentions(canonical_name);
CREATE INDEX IF NOT EXISTS idx_entity_channel           ON entity_mentions(channel_id);
CREATE INDEX IF NOT EXISTS idx_gossip_runs_community    ON gossip_runs(community_id);
CREATE INDEX IF NOT EXISTS idx_community_sources_cid    ON community_sources(community_id);
-- idx_comments_source_type is created in _migrate() after the column is added
