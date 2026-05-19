-- Privacy Compliance Toolkit — schema
-- Written for SQLite; kept PostgreSQL-compatible (no SQLite-only syntax in core tables).
-- For PG: replace `INTEGER PRIMARY KEY AUTOINCREMENT` with `BIGSERIAL PRIMARY KEY`,
-- and `TEXT CHECK(...)` style enums map cleanly.

-- ---------------------------------------------------------------------------
-- frameworks: one row per loaded framework version
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS frameworks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,                -- e.g. "GDPR"
    version         TEXT NOT NULL,                -- e.g. "2016/679"
    source          TEXT NOT NULL,                -- file path or URL the data came from
    source_hash     TEXT NOT NULL,                -- sha256 of the source file at load time
    loaded_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, version)
);

-- ---------------------------------------------------------------------------
-- articles: one row per article / control / requirement in a framework
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    framework_id    INTEGER NOT NULL REFERENCES frameworks(id) ON DELETE CASCADE,
    category        TEXT NOT NULL,
    requirement     TEXT NOT NULL,
    body            TEXT NOT NULL,
    reference       TEXT NOT NULL,                -- canonical citation, e.g. "GDPR Art. 6"
    body_hash       TEXT NOT NULL,                -- sha256(body) — used by citation verifier
    UNIQUE (framework_id, reference)
);

CREATE INDEX IF NOT EXISTS idx_articles_framework ON articles (framework_id);
CREATE INDEX IF NOT EXISTS idx_articles_reference ON articles (reference);

-- ---------------------------------------------------------------------------
-- audit_log: written BEFORE any data access by the logging gateway.
-- Append-only by convention; no UPDATE/DELETE statements anywhere in code.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    actor           TEXT NOT NULL,                -- component name, e.g. "mcp.get_article"
    action          TEXT NOT NULL,                -- "read" | "write" | "search" | "deny"
    resource        TEXT NOT NULL,                -- e.g. "articles:GDPR Art. 6"
    status          TEXT NOT NULL,                -- "ok" | "denied" | "error"
    request_id      TEXT,                         -- correlation id across calls
    metadata_json   TEXT                          -- arbitrary JSON, never PII
);

CREATE INDEX IF NOT EXISTS idx_audit_ts       ON audit_log (ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor    ON audit_log (actor);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log (resource);

-- ---------------------------------------------------------------------------
-- review_queue: outputs below the confidence threshold get parked here for
-- human sign-off (trust pyramid). v1 wires this up; table exists in v0 so
-- migrations stay additive.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    request_id      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    confidence      REAL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- "pending" | "approved" | "rejected"
    reviewed_by     TEXT,
    reviewed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue (status);
