-- Kalvid AI — core schema. **SQLITE ONLY.**
--
-- Do NOT paste this into the Supabase SQL editor: the PRAGMA and AUTOINCREMENT lines
-- below are SQLite syntax and Postgres rejects them.
-- The Postgres/Supabase version of this schema is app/schema_pg.sql, or run:
--     python cli.py db schema --postgres

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    contact_info        TEXT,
    monthly_budget_cap  REAL    NOT NULL DEFAULT 0,   -- USD, 0 = no spend allowed
    default_job_cap     REAL    NOT NULL DEFAULT 0,   -- USD per job, 0 = fall back to client cap
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS personas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name                TEXT    NOT NULL,
    -- How this persona's face is held stable. See app/identity.py — this is the
    -- single highest-risk part of the system, so the strategy is explicit, not implied.
    identity_strategy   TEXT    NOT NULL DEFAULT 'reference_image'
                        CHECK (identity_strategy IN ('reference_image','lora','character_id')),
    reference_image_url TEXT,           -- locked still: the canonical face
    identity_lock_id    TEXT,           -- LoRA path / provider-side character id, when applicable
    voice_profile       TEXT,
    notes               TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (client_id, name)
);

-- Identity is immutable once locked. An edit does not overwrite a face; it creates
-- the next version, and everything already rendered stays pinned to the version it
-- was made with. Without this, changing a persona's reference silently rewrites the
-- provenance of every clip ever delivered under the old one.
CREATE TABLE IF NOT EXISTS identity_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id          INTEGER NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    version             INTEGER NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','locked','superseded')),
    -- The frozen snapshot: everything that determines who she is.
    identity_strategy   TEXT    NOT NULL DEFAULT 'reference_image'
                        CHECK (identity_strategy IN ('reference_image','lora','character_id')),
    reference_image_url TEXT,           -- the identity plate for THIS version
    identity_lock_id    TEXT,
    character_sheet     TEXT,           -- descriptive metadata (JSON); supports the
                                        -- pack, never replaces it — text can't hold a face
    voice_profile       TEXT,
    notes               TEXT,
    locked_at           TEXT,
    locked_by           TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (persona_id, version)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id          INTEGER NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    -- Pinned at creation. The render uses THIS identity even if the persona has since
    -- moved to a later version.
    identity_version_id INTEGER REFERENCES identity_versions(id) ON DELETE SET NULL,
    brief               TEXT    NOT NULL,
    structured_prompt   TEXT,
    platform            TEXT    NOT NULL DEFAULT 'tiktok',
    target_duration     INTEGER NOT NULL DEFAULT 8,   -- seconds
    job_budget_cap      REAL    NOT NULL DEFAULT 0,   -- USD, 0 = inherit client default
    status              TEXT    NOT NULL DEFAULT 'created'
                        CHECK (status IN ('created','drafting','draft_ready','approved',
                                          'rendering','complete','rejected','failed')),
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The ledger's source of truth. Every paid call gets a row BEFORE it fires
-- (status='pending', cost_usd = estimate) and is reconciled after (actual cost).
-- Nothing spends money without a row here first.
CREATE TABLE IF NOT EXISTS generations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL for a standalone still: an influencer asset or an ad image belongs to a
    -- client and (usually) a persona, but not to a video job.
    job_id              INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    persona_id          INTEGER REFERENCES personas(id) ON DELETE SET NULL,
    -- Denormalised on purpose. Spend used to be found by joining out through
    -- jobs -> personas, which silently excluded any generation without a job. The
    -- cap must count every paid call, so the owning client is recorded directly.
    client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    identity_version_id INTEGER REFERENCES identity_versions(id) ON DELETE SET NULL,
    stage               TEXT    NOT NULL CHECK (stage IN ('draft','final','still','script')),
    provider            TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    request_payload     TEXT,
    provider_job_id     TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
    estimated_cost_usd  REAL    NOT NULL DEFAULT 0,
    -- NULL until the provider tells us what it actually charged. Providers bill on
    -- failure too, so a failed row keeps a real cost rather than assuming $0.
    actual_cost_usd     REAL,
    output_url          TEXT,
    error               TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    settled_at          TEXT
);

CREATE TABLE IF NOT EXISTS budget_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    generation_id       INTEGER REFERENCES generations(id) ON DELETE SET NULL,
    job_id              INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    amount_usd          REAL    NOT NULL,
    running_total       REAL    NOT NULL,
    cap_at_time         REAL    NOT NULL,
    scope               TEXT    NOT NULL DEFAULT 'client' CHECK (scope IN ('client','job')),
    blocked             INTEGER NOT NULL DEFAULT 0,
    overridden_by       TEXT,
    note                TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Saved influencer assets: generated stills and uploads, reusable as the seed for
-- any number of videos. One per persona may be flagged primary (the canonical face).
CREATE TABLE IF NOT EXISTS assets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    persona_id          INTEGER REFERENCES personas(id) ON DELETE CASCADE,
    generation_id       INTEGER REFERENCES generations(id) ON DELETE SET NULL,
    identity_version_id INTEGER REFERENCES identity_versions(id) ON DELETE SET NULL,
    -- Which canonical plate this is, per the reference-pack spec. NULL = an ordinary
    -- still, not part of the identity lock.
    plate               TEXT    CHECK (plate IN ('identity','turnaround','detail',
                                                 'expression','wardrobe','product')),
    kind                TEXT    NOT NULL DEFAULT 'image' CHECK (kind IN ('image','video')),
    source              TEXT    NOT NULL DEFAULT 'generated'
                        CHECK (source IN ('generated','uploaded')),
    url                 TEXT    NOT NULL,
    prompt              TEXT,
    label               TEXT,
    is_primary          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_gen_job     ON generations(job_id);
CREATE INDEX IF NOT EXISTS idx_gen_client  ON generations(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assets_per  ON assets(persona_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assets_cli  ON assets(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_iv_persona   ON identity_versions(persona_id, version);
CREATE INDEX IF NOT EXISTS idx_gen_status  ON generations(status);
CREATE INDEX IF NOT EXISTS idx_gen_created ON generations(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_persona ON jobs(persona_id);
CREATE INDEX IF NOT EXISTS idx_be_client   ON budget_events(client_id, created_at);
