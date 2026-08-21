SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS areas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    device_type TEXT NOT NULL,
    area_id TEXT NOT NULL REFERENCES areas(id),
    capabilities TEXT NOT NULL DEFAULT '[]',
    ip_address TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    configs TEXT NOT NULL DEFAULT '{}',
    activity_window_seconds INTEGER CHECK (
        activity_window_seconds IS NULL OR activity_window_seconds > 0
    ),
    metadata TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS raw_artifacts (
    id TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    byte_size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    original_filename TEXT,
    sealed INTEGER NOT NULL DEFAULT 1,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    primary_area_id TEXT NOT NULL REFERENCES areas(id),
    start_time TEXT NOT NULL,
    last_event_time TEXT,
    last_activity_at TEXT,
    minimum_end_at TEXT,
    end_time TEXT,
    state TEXT NOT NULL DEFAULT 'new',
    event_count INTEGER NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    area_id TEXT NOT NULL REFERENCES areas(id),
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_state TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT '',
    dedup_key TEXT,
    raw_payload_path TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    episode_id TEXT REFERENCES episodes(id)
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    area_id TEXT NOT NULL REFERENCES areas(id),
    timestamp TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    original_filename TEXT,
    artifact_id TEXT REFERENCES raw_artifacts(id),
    byte_size INTEGER,
    sha256 TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    event_id TEXT REFERENCES events(id),
    episode_id TEXT REFERENCES episodes(id)
);

CREATE TABLE IF NOT EXISTS ingestion_receipts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    observed_at TEXT,
    status TEXT NOT NULL DEFAULT 'accepted',
    artifact_id TEXT REFERENCES raw_artifacts(id),
    device_id TEXT NOT NULL DEFAULT '',
    area_id TEXT NOT NULL DEFAULT '',
    external_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    event_id TEXT REFERENCES events(id),
    evidence_id TEXT REFERENCES evidence(id),
    episode_id TEXT REFERENCES episodes(id)
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(device_id);
CREATE INDEX IF NOT EXISTS idx_events_episode ON events(episode_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup_key
    ON events(dedup_key)
    WHERE dedup_key IS NOT NULL AND dedup_key != '';
CREATE INDEX IF NOT EXISTS idx_evidence_timestamp ON evidence(timestamp);
CREATE INDEX IF NOT EXISTS idx_evidence_episode ON evidence(episode_id);
CREATE INDEX IF NOT EXISTS idx_receipts_event ON ingestion_receipts(event_id);
CREATE INDEX IF NOT EXISTS idx_receipts_evidence ON ingestion_receipts(evidence_id);
CREATE INDEX IF NOT EXISTS idx_receipts_episode ON ingestion_receipts(episode_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_artifacts_path ON raw_artifacts(file_path);
CREATE INDEX IF NOT EXISTS idx_episodes_area ON episodes(primary_area_id);
CREATE INDEX IF NOT EXISTS idx_episodes_state ON episodes(state);
"""
