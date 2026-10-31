-- Atlas Scholarship Intelligence: SQLite schema

CREATE TABLE IF NOT EXISTS scholarships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,          -- sha256 of canonical official URL
    name TEXT, provider TEXT,
    source_type TEXT, official_strength REAL,
    official_url TEXT NOT NULL, application_url TEXT, application_url_status INTEGER,
    amount TEXT, eligibility_text TEXT, academic_requirements TEXT, education_level TEXT,
    income_limit TEXT, age_criteria TEXT, gender_criteria TEXT, category_criteria TEXT,
    domicile TEXT, institution_requirements TEXT,
    opening_date TEXT, closing_date TEXT,
    documents_required TEXT, selection_process TEXT, renewal_requirements TEXT,
    status TEXT,                               -- ACTIVE | EXPIRING_SOON | EXPIRED | REVIEW_REQUIRED | NO_LONGER_VERIFIABLE
    verification TEXT,                         -- VERIFIED | REVIEW_REQUIRED
    confidence REAL, score_breakdown TEXT,     -- JSON: per-check weight / score / points / detail
    closed_signal TEXT,
    first_seen TEXT, last_seen TEXT, last_verified TEXT, last_changed TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    content_hash TEXT,
    is_fixture INTEGER DEFAULT 0
);

-- Every extracted value with the verbatim quote that supports it. Old rows are superseded, never deleted.
CREATE TABLE IF NOT EXISTS field_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id INTEGER NOT NULL REFERENCES scholarships(id),
    field TEXT NOT NULL, value TEXT, quote TEXT, source_url TEXT, method TEXT,
    grounded INTEGER, captured_at TEXT, run_id INTEGER, superseded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev_sch ON field_evidence(scholarship_id, field);

-- Append-only audit log: DISCOVERED / CHANGED / STATUS / UNAVAILABLE
CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id INTEGER NOT NULL REFERENCES scholarships(id),
    change_type TEXT, field TEXT, old_value TEXT, new_value TEXT,
    detected_at TEXT, source_url TEXT, evidence TEXT, run_id INTEGER
);

CREATE TABLE IF NOT EXISTS score_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id INTEGER NOT NULL REFERENCES scholarships(id),
    run_id INTEGER, at TEXT, confidence REAL, verification TEXT, status TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id INTEGER NOT NULL REFERENCES scholarships(id),
    run_id INTEGER, fetched_at TEXT, http_status INTEGER, content_hash TEXT, text_blob BLOB
);

CREATE TABLE IF NOT EXISTS candidates (       -- discovery frontier
    url TEXT PRIMARY KEY,
    depth INTEGER, discovered_via TEXT, source_type TEXT, official_strength REAL,
    state TEXT DEFAULT 'new',                  -- new | done | failed | skipped
    priority REAL DEFAULT 0, attempts INTEGER DEFAULT 0, next_retry TEXT,
    recrawl INTEGER DEFAULT 0,                 -- seeds & listing pages are re-crawled every run
    first_seen TEXT, last_checked TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, stats TEXT
);
