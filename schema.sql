-- YouTube Community Analyzer — Unified Database Schema

-- ═══ COMMUNITY LAYER ══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS communities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS community_channels (
    community_id    INTEGER NOT NULL,
    channel_id      TEXT NOT NULL,
    added_at        TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (community_id, channel_id),
    FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
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
    comment_id          TEXT PRIMARY KEY,
    video_id            TEXT NOT NULL,
    channel_id          TEXT NOT NULL,
    author_name         TEXT,
    author_channel_id   TEXT,
    text                TEXT,
    like_count          INTEGER DEFAULT 0,
    published_at        TEXT,
    is_reply            INTEGER DEFAULT 0,
    parent_id           TEXT,
    collected_at        TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

CREATE TABLE IF NOT EXISTS video_summaries (
    video_id            TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    summary_json        TEXT,
    comment_count       INTEGER,
    gossip_count        INTEGER,
    processed_at        TEXT DEFAULT (datetime('now')),
    llm_backend         TEXT,
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
