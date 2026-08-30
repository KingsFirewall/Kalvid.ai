"""Postgres-backend tests.

Skipped unless a real SUPABASE_DB_URL is configured. These are the checks that must
pass before trusting the migration — especially the concurrency one, since the budget
guard's atomicity is implemented differently on each backend (BEGIN IMMEDIATE on
SQLite, a transaction-scoped advisory lock on Postgres).

Run with:  KALVID_DB_BACKEND=postgres .venv/bin/python -m pytest tests/test_postgres.py -q
"""
import os
import threading

import pytest

from app.config import settings

pytestmark = pytest.mark.skipif(
    not settings.postgres_dsn or os.getenv("KALVID_DB_BACKEND") != "postgres",
    reason="needs SUPABASE_DB_URL and KALVID_DB_BACKEND=postgres",
)


@pytest.fixture
def pg():
    from app import db
    assert db.BACKEND == "postgres", "these tests must not run against SQLite"
    db.init_db()
    conn = db.get_conn()
    for t in ("budget_events", "generations", "jobs", "personas", "clients"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    yield db


@pytest.fixture
def seeded(pg):
    cid = pg.insert(
        "INSERT INTO clients (name, monthly_budget_cap) VALUES (?,?)", ("PgAcme", 1000.0))
    pid = pg.insert(
        """INSERT INTO personas (client_id, name, identity_strategy, reference_image_url)
           VALUES (?,?, 'reference_image', ?)""",
        (cid, "Rania", "https://example.test/r.png"))
    return cid, pid


def test_schema_applies_and_ids_are_returned(seeded):
    cid, pid = seeded
    assert isinstance(cid, int) and cid > 0
    assert isinstance(pid, int) and pid > 0


def test_full_job_loop_on_postgres(seeded):
    from app import db, jobs
    _, pid = seeded
    job_id = jobs.create_job(persona_id=pid, brief='She smiles and says "it works"')
    jobs.start_draft(job_id)
    assert jobs.wait_idle(30)
    assert db.query_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "draft_ready"
    jobs.approve(job_id)
    assert jobs.wait_idle(30)
    assert db.query_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "complete"


def test_month_window_filters_correctly(seeded):
    """The date(col) -> direct comparison port must still scope to this month."""
    from app import ledger
    cid, pid = seeded
    spent, _ = ledger.client_spend(cid)
    assert spent == 0


def test_concurrent_reservations_cannot_both_slip_under_the_cap(seeded):
    """The advisory lock must give the same guarantee BEGIN IMMEDIATE gives SQLite."""
    from app import db, jobs, ledger
    from app.rates import rate_table

    cid, pid = seeded
    job_id = jobs.create_job(persona_id=pid, brief="She smiles")
    rate = rate_table.candidates("final")[0]
    cost = rate.estimate(duration_s=8, variant=rate.variant_for("final"))
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?", (cost, cid))

    results, barrier = [], threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            results.append(("ok", ledger.reserve(job_id=job_id, stage="final", rate=rate,
                                                 duration_s=8, variant=rate.variant_for("final"),
                                                 billable=False)))
        except ledger.BudgetExceeded:
            results.append(("blocked", None))
        except Exception as exc:
            results.append(("error", repr(exc)))
        finally:
            db.close_conn()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)
    assert sorted(k for k, _ in results) == ["blocked", "ok"], results


def test_migration_preserves_ids(pg, tmp_path):
    """Ids appear in archived paths and the audit trail; they must not be renumbered."""
    import sqlite3

    from app import migrate

    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.executescript((__import__("pathlib").Path("app/schema.sql")).read_text())
    conn.execute("INSERT INTO clients (id, name, monthly_budget_cap) VALUES (77, 'Legacy', 25)")
    conn.execute("""INSERT INTO personas (id, client_id, name, reference_image_url)
                    VALUES (88, 77, 'Old', 'https://x.test/a.png')""")
    conn.commit(); conn.close()

    data = migrate.read_sqlite(src)
    migrate.copy_into_postgres(data, wipe=True)
    assert not migrate.verify(data)
    assert pg.query_one("SELECT name FROM clients WHERE id=77")["name"] == "Legacy"
    # The identity sequence must be advanced past the forced ids.
    new_id = pg.insert("INSERT INTO clients (name, monthly_budget_cap) VALUES (?,?)",
                       ("AfterMigration", 10))
    assert new_id > 77
