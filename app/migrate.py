"""Move the ledger from SQLite to Postgres (Supabase), preserving ids.

Ids are preserved because generations.id and job ids appear in the dashboard, in
budget_events, and in archived output paths. A migration that renumbered them would
silently break the audit trail.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db
from .config import settings

# Parent tables first — foreign keys must resolve as we go.
TABLES = ["clients", "personas", "jobs", "generations", "budget_events"]

COLUMNS = {
    "clients": ["id", "name", "contact_info", "monthly_budget_cap", "default_job_cap",
                "created_at"],
    "personas": ["id", "client_id", "name", "identity_strategy", "reference_image_url",
                 "identity_lock_id", "voice_profile", "notes", "created_at"],
    "jobs": ["id", "persona_id", "brief", "structured_prompt", "platform",
             "target_duration", "job_budget_cap", "status", "created_at", "updated_at"],
    "generations": ["id", "job_id", "stage", "provider", "model", "request_payload",
                    "provider_job_id", "status", "estimated_cost_usd",
                    "actual_cost_usd", "output_url", "error", "created_at", "settled_at"],
    "budget_events": ["id", "client_id", "generation_id", "job_id", "amount_usd",
                      "running_total", "cap_at_time", "scope", "blocked",
                      "overridden_by", "note", "created_at"],
}


def read_sqlite(path: Path) -> dict[str, list[dict]]:
    if not path.exists():
        return {t: [] for t in TABLES}
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = {}
    for table in TABLES:
        try:
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS[table])} FROM {table} ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            rows = []
        out[table] = [dict(r) for r in rows]
    conn.close()
    return out


def copy_into_postgres(data: dict[str, list[dict]], *, wipe: bool = False) -> dict[str, int]:
    if db.BACKEND != "postgres":
        raise RuntimeError("target backend is not postgres — check SUPABASE_DB_URL")
    db.init_db()
    conn = db.get_conn()
    counts = {}

    if wipe:
        # Children first, so foreign keys stay satisfied during the delete.
        for table in reversed(TABLES):
            conn.execute(f"DELETE FROM {table}")

    for table in TABLES:
        rows = data.get(table, [])
        counts[table] = len(rows)
        if not rows:
            continue
        cols = COLUMNS[table]
        placeholders = ", ".join(["%s"] * len(cols))
        # id is GENERATED ALWAYS, so an explicit value needs OVERRIDING SYSTEM VALUE.
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) OVERRIDING SYSTEM VALUE "
               f"VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING")
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])

    # Identity sequences must be advanced past the ids we forced in, or the next
    # insert collides with an existing row.
    for table in TABLES:
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1), true)")
    conn.commit()
    return counts


def verify(data: dict[str, list[dict]]) -> list[str]:
    """Confirm every source row landed."""
    problems = []
    conn = db.get_conn()
    for table in TABLES:
        expected = len(data.get(table, []))
        actual = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        if actual < expected:
            problems.append(f"{table}: expected >= {expected} rows, found {actual}")
    return problems


def run(sqlite_path: Path | None = None, *, wipe: bool = False) -> tuple[dict, list[str]]:
    data = read_sqlite(sqlite_path or Path(settings.db_path))
    counts = copy_into_postgres(data, wipe=wipe)
    return counts, verify(data)
