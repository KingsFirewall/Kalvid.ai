"""In-place schema upgrades for databases created before a column existed.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a
ledger created by an earlier version keeps its old shape forever unless something
migrates it. Every function here is idempotent and safe to run on every startup.

The upgrade that matters: `generations` gained `client_id` (and a nullable
`job_id`/`persona_id`) so that a still generated for an influencer — which has no
video job — still counts against that client's monthly cap. Before this, spend was
found by joining out through jobs, and anything without a job was invisible to the
budget guard. An invisible charge is the one failure this system exists to prevent.
"""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger(__name__)

_GENERATIONS_NEW_SQLITE = """
CREATE TABLE generations_upgrade (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    persona_id          INTEGER REFERENCES personas(id) ON DELETE SET NULL,
    client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    stage               TEXT    NOT NULL CHECK (stage IN ('draft','final','still')),
    provider            TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    request_payload     TEXT,
    provider_job_id     TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
    estimated_cost_usd  REAL    NOT NULL DEFAULT 0,
    actual_cost_usd     REAL,
    output_url          TEXT,
    error               TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    settled_at          TEXT
)
"""


def _sqlite_columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _upgrade_sqlite(conn) -> bool:
    if "generations" not in {
        r["name"] for r in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        return False                     # fresh database; the schema is already current
    if "client_id" in _sqlite_columns(conn, "generations"):
        return False

    log.info("upgrading generations: adding client_id/persona_id, widening stage")
    # SQLite cannot ALTER a CHECK constraint or drop NOT NULL, so the table is
    # rebuilt. FKs are disabled for the swap so budget_events.generation_id is not
    # cascaded to NULL when the old table is dropped — the audit log must survive.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(_GENERATIONS_NEW_SQLITE)
        conn.execute(
            """INSERT INTO generations_upgrade
                   (id, job_id, persona_id, client_id, stage, provider, model,
                    request_payload, provider_job_id, status, estimated_cost_usd,
                    actual_cost_usd, output_url, error, created_at, settled_at)
               SELECT g.id, g.job_id, j.persona_id, p.client_id, g.stage, g.provider,
                      g.model, g.request_payload, g.provider_job_id, g.status,
                      g.estimated_cost_usd, g.actual_cost_usd, g.output_url, g.error,
                      g.created_at, g.settled_at
                 FROM generations g
                 JOIN jobs     j ON j.id = g.job_id
                 JOIN personas p ON p.id = j.persona_id""")
        # Any row whose job or persona was already deleted has no client to charge and
        # cannot be migrated. Count it rather than dropping it silently.
        orphans = conn.execute(
            """SELECT COUNT(*) AS n FROM generations g
                WHERE g.id NOT IN (SELECT id FROM generations_upgrade)""").fetchone()["n"]
        if orphans:
            log.warning("%s generation row(s) had no reachable client and were not "
                        "migrated (their job or persona was deleted)", orphans)
        conn.execute("DROP TABLE generations")
        conn.execute("ALTER TABLE generations_upgrade RENAME TO generations")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute("DROP TABLE IF EXISTS generations_upgrade")
        conn.commit()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return True


def _upgrade_postgres(conn) -> bool:
    existing = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='generations'").fetchall()}
    if not existing or "client_id" in existing:
        conn.commit()
        return False

    log.info("upgrading generations: adding client_id/persona_id, widening stage")
    conn.execute("ALTER TABLE generations ADD COLUMN IF NOT EXISTS persona_id BIGINT "
                 "REFERENCES personas(id) ON DELETE SET NULL")
    conn.execute("ALTER TABLE generations ADD COLUMN IF NOT EXISTS client_id BIGINT "
                 "REFERENCES clients(id) ON DELETE CASCADE")
    conn.execute("""UPDATE generations g
                       SET persona_id = j.persona_id, client_id = p.client_id
                      FROM jobs j JOIN personas p ON p.id = j.persona_id
                     WHERE j.id = g.job_id AND g.client_id IS NULL""")
    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM generations WHERE client_id IS NULL").fetchone()["n"]
    if orphans:
        # NOT NULL would fail; leave the column nullable and say so loudly rather
        # than deleting ledger rows to make a constraint apply.
        log.warning("%s generation row(s) have no reachable client; client_id left "
                    "nullable. Investigate before relying on per-client totals.", orphans)
    else:
        conn.execute("ALTER TABLE generations ALTER COLUMN client_id SET NOT NULL")
    conn.execute("ALTER TABLE generations ALTER COLUMN job_id DROP NOT NULL")
    conn.execute("ALTER TABLE generations DROP CONSTRAINT IF EXISTS generations_stage_check")
    conn.execute("ALTER TABLE generations ADD CONSTRAINT generations_stage_check "
                 "CHECK (stage IN ('draft','final','still'))")
    conn.commit()
    return True


def upgrade() -> bool:
    """Bring an existing database up to the current schema. Returns True if it changed."""
    conn = db.get_conn()
    return _upgrade_postgres(conn) if db.BACKEND == "postgres" else _upgrade_sqlite(conn)
