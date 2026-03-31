-- Agent Analytics Schema
-- Run: sqlite3 state/agent_analytics.db < state/schema.sql

-- Table 1: runs (one row per agent run — replaces usage_log.xlsx)
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    run_id TEXT UNIQUE,
    mode TEXT NOT NULL,
    version TEXT DEFAULT 'v2',
    model TEXT NOT NULL,
    url TEXT,
    app_name TEXT,
    turns_used INTEGER,
    turns_max INTEGER,
    input_tokens INTEGER,
    cached_tokens INTEGER,
    uncached_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cache_hit_pct REAL,
    uncached_cost REAL,
    cached_cost REAL,
    output_cost REAL,
    real_cost REAL,
    nocache_cost REAL,
    savings REAL,
    savings_pct REAL,
    real_cost_inr REAL,
    duration_sec REAL,
    status TEXT,
    rolling_summary TEXT,
    notes TEXT
);

-- Table 2: turns (one row per turn within a run — replaces turns_*.json)
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    input_tokens INTEGER,
    cached_tokens INTEGER,
    uncached_tokens INTEGER,
    output_tokens INTEGER,
    new_tokens INTEGER,
    cache_hit_ratio REAL,
    thought TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- Table 3: actions (one row per tool call within a turn)
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    tool TEXT NOT NULL,
    target TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- Table 4: fields (one row per field discovered/tested — from StateTracker)
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    section_name TEXT,
    field_name TEXT NOT NULL,
    uid TEXT,
    action TEXT,
    xpath TEXT,
    status TEXT,
    value TEXT,
    retries INTEGER DEFAULT 0,
    error TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- Table 5: failures (from StateTracker)
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    section_name TEXT,
    field_name TEXT,
    action TEXT,
    error TEXT,
    diagnosis TEXT,
    evidence TEXT,
    failure_timestamp TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- Table 6: test_cases (Pass 2 per-test-case results)
CREATE TABLE IF NOT EXISTS test_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    tc_number INTEGER,
    field_name TEXT,
    selector TEXT,
    test_value TEXT,
    expected_result TEXT,
    actual_result TEXT,
    status TEXT,
    priority TEXT,
    description TEXT,
    notes TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

-- Indexes for fast Grafana queries
CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp);
CREATE INDEX IF NOT EXISTS idx_runs_mode ON runs(mode);
CREATE INDEX IF NOT EXISTS idx_runs_model ON runs(model);
CREATE INDEX IF NOT EXISTS idx_turns_run_id ON turns(run_id);
CREATE INDEX IF NOT EXISTS idx_actions_run_id ON actions(run_id);
CREATE INDEX IF NOT EXISTS idx_actions_tool ON actions(tool);
CREATE INDEX IF NOT EXISTS idx_fields_run_id ON fields(run_id);
CREATE INDEX IF NOT EXISTS idx_test_cases_run_id ON test_cases(run_id);
