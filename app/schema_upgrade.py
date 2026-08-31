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


# --------------------------------------------------------------- identity versions

_ADD_COLUMNS = [
    ("jobs",        "identity_version_id", "REFERENCES identity_versions(id) ON DELETE SET NULL"),
    ("generations", "identity_version_id", "REFERENCES identity_versions(id) ON DELETE SET NULL"),
    ("assets",      "identity_version_id", "REFERENCES identity_versions(id) ON DELETE SET NULL"),
    ("assets",      "plate",               None),
]


def _has_column(conn, table: str, column: str) -> bool:
    if db.BACKEND == "postgres":
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=%s AND column_name=%s", (table, column)).fetchone()
        return row is not None
    return column in _sqlite_columns(conn, table)


def _table_exists(conn, table: str) -> bool:
    if db.BACKEND == "postgres":
        return conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=%s",
            (table,)).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def _upgrade_identity_versions(conn) -> bool:
    """Give every existing persona a locked v1 and pin existing work to it.

    Adding columns is the easy half. The half that matters is the backfill: a job
    rendered last week must keep pointing at the identity it actually used, so it is
    pinned to v1 here rather than left NULL to silently inherit whatever the persona
    becomes later.
    """
    if not _table_exists(conn, "personas") or not _table_exists(conn, "identity_versions"):
        return False

    changed = False
    for table, column, ref in _ADD_COLUMNS:
        if not _table_exists(conn, table) or _has_column(conn, table, column):
            continue
        coltype = "BIGINT" if db.BACKEND == "postgres" else "INTEGER"
        decl = f"{column} {coltype}" if column.endswith("_id") else f"{column} TEXT"
        # The FK is deliberately omitted on SQLite: ALTER TABLE ADD COLUMN cannot add
        # one to an existing table, and rebuilding three tables to gain a constraint
        # the application already maintains is not worth the risk to the ledger.
        if ref and db.BACKEND == "postgres":
            decl += " " + ref
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")
        changed = True

    personas = conn.execute(
        db.translate("SELECT * FROM personas WHERE id NOT IN "
                     "(SELECT persona_id FROM identity_versions)"), ()).fetchall()
    for row in personas:
        log.info("locking identity v1 for persona %s (%s)", row["id"], row["name"])
        sql = ("""INSERT INTO identity_versions
                   (persona_id, version, status, identity_strategy, reference_image_url,
                    identity_lock_id, voice_profile, notes, locked_at, locked_by)
                  VALUES (?,1,'locked',?,?,?,?,?, CURRENT_TIMESTAMP, 'migration')""")
        args = (row["id"], row["identity_strategy"], row["reference_image_url"],
                row["identity_lock_id"], row["voice_profile"], row["notes"])
        if db.BACKEND == "postgres":
            vid = conn.execute(db.translate(sql) + " RETURNING id", args).fetchone()["id"]
        else:
            vid = conn.execute(sql, args).lastrowid
        for table, join in (("jobs", "persona_id"), ("assets", "persona_id")):
            conn.execute(db.translate(
                f"UPDATE {table} SET identity_version_id=? "
                f"WHERE {join}=? AND identity_version_id IS NULL"), (vid, row["id"]))
        conn.execute(db.translate(
            "UPDATE generations SET identity_version_id=? "
            "WHERE persona_id=? AND identity_version_id IS NULL"), (vid, row["id"]))
        changed = True

    conn.commit()
    return changed


def upgrade() -> bool:
    """Shape changes that must happen BEFORE the schema script runs.

    The generations rebuild is here because schema.sql creates an index on
    generations(client_id), which fails outright against the old table.
    """
    conn = db.get_conn()
    return _upgrade_postgres(conn) if db.BACKEND == "postgres" else _upgrade_sqlite(conn)


# The set of stages a generations row may carry. Widening this needs a migration on
# both backends — SQLite cannot alter a CHECK, so the table is rebuilt.
STAGES = ("draft", "final", "still", "script")


def _upgrade_stage_check(conn) -> bool:
    """Widen generations.stage to the current STAGES list."""
    if not _table_exists(conn, "generations"):
        return False

    if db.BACKEND == "postgres":
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conname='generations_stage_check'").fetchone()
        if row and all(f"'{st}'" in row["def"] for st in STAGES):
            return False
        allowed = ",".join(f"'{st}'" for st in STAGES)
        conn.execute("ALTER TABLE generations DROP CONSTRAINT IF EXISTS generations_stage_check")
        conn.execute(f"ALTER TABLE generations ADD CONSTRAINT generations_stage_check "
                     f"CHECK (stage IN ({allowed}))")
        conn.commit()
        return True

    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='generations'"
    ).fetchone()
    if ddl is None or all(f"'{st}'" in ddl["sql"] for st in STAGES):
        return False

    log.info("widening generations.stage to %s", ", ".join(STAGES))
    allowed = ",".join(f"'{st}'" for st in STAGES)
    # Rebuild with the CHECK replaced, preserving every column and row. FKs are off
    # for the swap so budget_events.generation_id is not cascaded away.
    import re
    # The stored DDL may or may not carry IF NOT EXISTS, and quoting varies, so
    # rewrite the table name by pattern rather than by exact string.
    new_ddl = re.sub(r'CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?["\[`]?generations["\]`]?',
                     "CREATE TABLE generations_stageup", ddl["sql"], count=1,
                     flags=re.I)
    new_ddl = re.sub(r"CHECK\s*\(stage IN \([^)]*\)\)", f"CHECK (stage IN ({allowed}))",
                     new_ddl, count=1)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(generations)").fetchall()]
    collist = ", ".join(cols)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(new_ddl)
        conn.execute(f"INSERT INTO generations_stageup ({collist}) "
                     f"SELECT {collist} FROM generations")
        conn.execute("DROP TABLE generations")
        conn.execute("ALTER TABLE generations_stageup RENAME TO generations")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute("DROP TABLE IF EXISTS generations_stageup")
        conn.commit()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return True


def upgrade_after_schema() -> bool:
    """Backfills that need the new tables to already exist."""
    conn = db.get_conn()
    a = _upgrade_stage_check(conn)
    b = _upgrade_identity_versions(conn)
    return a or b
