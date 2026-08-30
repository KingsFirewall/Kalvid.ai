-- Kalvid AI — Postgres schema. **THIS is the one to run on Supabase.**
-- Paste into the Supabase SQL editor, or apply it with: python cli.py db init
-- Mirrors app/schema.sql. Kept deliberately portable: booleans are stored as SMALLINT
-- 0/1 and timestamps default to CURRENT_TIMESTAMP, so the application SQL above
-- app/db.py is identical on both backends.

CREATE TABLE IF NOT EXISTS clients (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                TEXT        NOT NULL UNIQUE,
    contact_info        TEXT,
    monthly_budget_cap  DOUBLE PRECISION NOT NULL DEFAULT 0,
    default_job_cap     DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS personas (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id           BIGINT      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name                TEXT        NOT NULL,
    identity_strategy   TEXT        NOT NULL DEFAULT 'reference_image'
                        CHECK (identity_strategy IN ('reference_image','lora','character_id')),
    reference_image_url TEXT,
    identity_lock_id    TEXT,
    voice_profile       TEXT,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (client_id, name)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    persona_id          BIGINT      NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    brief               TEXT        NOT NULL,
    structured_prompt   TEXT,
    platform            TEXT        NOT NULL DEFAULT 'tiktok',
    target_duration     INTEGER     NOT NULL DEFAULT 8,
    job_budget_cap      DOUBLE PRECISION NOT NULL DEFAULT 0,
    status              TEXT        NOT NULL DEFAULT 'created'
                        CHECK (status IN ('created','drafting','draft_ready','approved',
                                          'rendering','complete','rejected','failed')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The ledger's source of truth. Every paid call gets a row BEFORE it fires
-- (status='pending', estimated cost) and is reconciled after (actual cost).
CREATE TABLE IF NOT EXISTS generations (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id              BIGINT      NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage               TEXT        NOT NULL CHECK (stage IN ('draft','final')),
    provider            TEXT        NOT NULL,
    model               TEXT        NOT NULL,
    request_payload     TEXT,
    provider_job_id     TEXT,
    status              TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
    estimated_cost_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    actual_cost_usd     DOUBLE PRECISION,
    output_url          TEXT,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS budget_events (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id           BIGINT      NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    generation_id       BIGINT      REFERENCES generations(id) ON DELETE SET NULL,
    job_id              BIGINT      REFERENCES jobs(id) ON DELETE SET NULL,
    amount_usd          DOUBLE PRECISION NOT NULL,
    running_total       DOUBLE PRECISION NOT NULL,
    cap_at_time         DOUBLE PRECISION NOT NULL,
    scope               TEXT        NOT NULL DEFAULT 'client' CHECK (scope IN ('client','job')),
    blocked             SMALLINT    NOT NULL DEFAULT 0,
    overridden_by       TEXT,
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gen_job      ON generations(job_id);
CREATE INDEX IF NOT EXISTS idx_gen_status   ON generations(status);
CREATE INDEX IF NOT EXISTS idx_gen_created  ON generations(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_persona ON jobs(persona_id);
CREATE INDEX IF NOT EXISTS idx_be_client    ON budget_events(client_id, created_at);

-- v1 is an internal tool with no client logins; nothing should reach these tables
-- through Supabase's public anon key. RLS is enabled with NO permissive policy, so
-- the anon/authenticated roles get nothing. The service role bypasses RLS, which is
-- what the app uses.
ALTER TABLE clients       ENABLE ROW LEVEL SECURITY;
ALTER TABLE personas      ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs          ENABLE ROW LEVEL SECURITY;
ALTER TABLE generations   ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_events ENABLE ROW LEVEL SECURITY;
