#!/usr/bin/env python3
"""Kalvid AI command line — everything the dashboard does, without a browser.

  python cli.py status
  python cli.py client add "Acme" --cap 50 --job-cap 10
  python cli.py persona add 1 "Rania" --ref https://... --notes "24yo, curly hair"
  python cli.py job add 1 "She unboxes the serum and says \"...\"" --duration 8
  python cli.py job preview 1
  python cli.py job draft 1
  python cli.py job approve 1 [--override you@team]
  python cli.py ledger 1
"""
from __future__ import annotations

import argparse
import sys

from app import db, jobs, ledger
from app.api import system_status
from app.config import settings
from app.rates import UnverifiedRate, rate_table
from app.router import NoProviderAvailable


def _p(msg=""):
    print(msg, flush=True)


def cmd_doctor(a):
    """Preflight: verify credentials and prices without spending anything."""
    from app.doctor import run_all
    checks = run_all()
    for c in checks:
        _p(f"  [{c.mark}] {c.name:<28} {c.detail}")
    blocking = [c for c in checks if not c.ok and c.blocking]
    warns = [c for c in checks if not c.ok and not c.blocking]
    _p()
    if blocking:
        _p(f"{len(blocking)} blocking issue(s) — a live render would fail or be refused.")
        sys.exit(1)
    _p("No blocking issues." + (f" {len(warns)} warning(s)." if warns else ""))


def cmd_setup_storage(a):
    """Create the Supabase bucket and prove a full upload -> signed URL round trip."""
    import tempfile
    from pathlib import Path as _Path

    from app import storage

    if not storage.available():
        _p("Supabase is not configured — check SUPABASE_* in .env")
        sys.exit(1)
    _p(f"bucket '{settings.supabase_bucket}': {storage.ensure_bucket(public=a.public)}")

    probe = "._preflight/roundtrip.txt"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("kalvid storage preflight")
        tmp = _Path(fh.name)
    try:
        storage.upload(tmp, probe)
        url = storage.signed_url(probe, ttl=60)
        import httpx
        ok = httpx.get(url, timeout=20.0).text == "kalvid storage preflight"
        _p(f"upload + signed URL round trip: {'OK' if ok else 'FAILED — content mismatch'}")
        if not ok:
            sys.exit(1)
    finally:
        storage.delete_object(probe)
        tmp.unlink(missing_ok=True)
    _p("storage is ready")


def cmd_db_status(a):
    from app import db
    _p(f"backend : {db.BACKEND}")
    if db.BACKEND == "postgres":
        dsn = settings.postgres_dsn or ""
        _p(f"host    : {dsn.split('@')[-1][:60] if dsn else 'NOT CONFIGURED'}")
    else:
        _p(f"file    : {settings.db_path}")
    try:
        db.init_db()
        for t in ("clients", "personas", "jobs", "generations", "budget_events"):
            n = db.query_one(f"SELECT COUNT(*) AS c FROM {t}")["c"]
            _p(f"  {t:<15} {n} rows")
    except Exception as exc:
        _p(f"could not read: {exc}")
        sys.exit(1)


def cmd_db_schema(a):
    """Print the DDL for the chosen backend, ready to paste into the SQL editor."""
    from pathlib import Path as _Path

    from app import db
    name = "schema_pg.sql" if (a.postgres or db.BACKEND == "postgres") else "schema.sql"
    print((_Path(__file__).parent / "app" / name).read_text())


def cmd_db_init(a):
    from app import db
    db.init_db()
    _p(f"schema applied to {db.BACKEND}")
    cmd_db_status(a)


def cmd_db_migrate(a):
    """Copy the SQLite ledger into Postgres, preserving ids."""
    from pathlib import Path as _Path

    from app import db, migrate

    if db.BACKEND != "postgres":
        _p("target backend is sqlite — set SUPABASE_DB_URL in .env first")
        sys.exit(1)
    src = _Path(a.source or settings.db_path)
    data = migrate.read_sqlite(src)
    _p(f"source: {src}")
    for t, rows in data.items():
        _p(f"  {t:<15} {len(rows)} rows")
    if not a.yes:
        if input("\nCopy these into Supabase Postgres? [y/N] ").strip().lower() not in ("y", "yes"):
            _p("cancelled")
            return
    counts = migrate.copy_into_postgres(data, wipe=a.wipe)
    problems = migrate.verify(data)
    _p("\ncopied: " + ", ".join(f"{t}={n}" for t, n in counts.items()))
    if problems:
        _p("VERIFY FAILED:")
        for pb in problems:
            _p(f"  {pb}")
        sys.exit(1)
    _p("verified — every source row is present")


def cmd_status(a):
    st = system_status()
    _p(f"mode         : {'DRY RUN (mock, $0)' if st['dry_run'] else 'LIVE — BILLABLE'}")
    _p("providers    : " + ", ".join(f"{k}={'ok' if v else 'not configured'}"
                                      for k, v in st["providers"].items()))
    _p(f"supabase     : {'configured' if st['supabase_storage'] else 'not configured'}")
    _p(f"in flight    : {st['in_flight']}")
    if st["unverified_rates"]:
        _p(f"UNVERIFIED   : {', '.join(st['unverified_rates'])}")
    for w in st["warnings"]:
        _p(f"  ! {w}")


def cmd_client_add(a):
    cid = db.insert(
        """INSERT INTO clients (name, contact_info, monthly_budget_cap, default_job_cap)
           VALUES (?,?,?,?)""", (a.name, a.contact or "", a.cap, a.job_cap))
    _p(f"client {cid}: {a.name}  cap ${a.cap:.2f}/mo, per-job ${a.job_cap:.2f}")


def cmd_client_list(a):
    for c in db.query("SELECT * FROM clients ORDER BY name"):
        spent, pending = ledger.client_spend(c["id"])
        _p(f"{c['id']:>3}  {c['name']:<24} ${spent:>7.2f} / ${c['monthly_budget_cap']:.2f}"
           f"  (${pending:.2f} in flight)")


def cmd_persona_add(a):
    pid = db.insert(
        """INSERT INTO personas (client_id, name, identity_strategy,
               reference_image_url, identity_lock_id, notes)
           VALUES (?,?,?,?,?,?)""",
        (a.client_id, a.name, a.strategy, a.ref, a.lock_id, a.notes or ""))
    _p(f"persona {pid}: {a.name} ({a.strategy})")


def cmd_persona_list(a):
    for p in db.query("""SELECT p.*, c.name cn FROM personas p
                           JOIN clients c ON c.id=p.client_id ORDER BY c.name, p.name"""):
        _p(f"{p['id']:>3}  {p['cn']:<18} {p['name']:<16} {p['identity_strategy']}")


def cmd_job_add(a):
    jid = jobs.create_job(persona_id=a.persona_id, brief=a.brief, platform=a.platform,
                          target_duration=a.duration, job_budget_cap=a.cap)
    job = db.query_one("SELECT structured_prompt FROM jobs WHERE id=?", (jid,))
    import json
    _p(f"job {jid} created\n\n{json.loads(job['structured_prompt'])['prompt']}\n")


def cmd_job_list(a):
    for j in db.query("""SELECT j.id, j.status, j.brief, p.name pn, c.name cn,
                                (SELECT COALESCE(SUM(COALESCE(actual_cost_usd,
                                                              estimated_cost_usd)),0)
                                   FROM generations g
                                  WHERE g.job_id=j.id AND g.status!='cancelled') spend
                           FROM jobs j JOIN personas p ON p.id=j.persona_id
                           JOIN clients c ON c.id=p.client_id ORDER BY j.id DESC"""):
        _p(f"{j['id']:>3}  {j['status']:<12} ${j['spend']:>6.2f}  "
           f"{j['cn']}/{j['pn']}  {j['brief'][:48]}")


def cmd_job_preview(a):
    from app.api import preview_job
    preview = preview_job(a.job_id)
    gate = preview.pop("gate", None)
    for stage, v in preview.items():
        if "error" in v:
            _p(f"{stage:<6}: ERROR {v['error']}")
            continue
        _p(f"{stage:<6}: ${v['estimate_usd']:.4f} via {v['model']}"
           f"{'  (charged $0 — dry run)' if not v['billable'] else ''}"
           f"{'  [PRICE UNVERIFIED]' if not v['rate_verified'] else ''}")
        for b in v["budget"]:
            flag = "  *** WOULD EXCEED ***" if b["would_exceed"] else ""
            _p(f"        {b['scope']} cap ${b['cap']:.2f}, ${b['remaining']:.2f} left{flag}")
    if gate:
        _p(f"\ngate  : {'' if gate['effective'] else '! '}{gate['note']}")


def _run(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except (jobs.TransitionError, jobs.IdentityError, ledger.BudgetExceeded,
            UnverifiedRate, NoProviderAvailable, KeyError) as exc:
        _p(f"REFUSED: {exc}")
        sys.exit(1)


def cmd_job_draft(a):
    gen = _run(jobs.start_draft, a.job_id, override_by=a.override)
    _p(f"draft generation {gen} started")
    if a.wait:
        jobs.wait_idle(300)
        cmd_job_show(a)


def cmd_job_approve(a):
    from app.api import preview_job
    est = preview_job(a.job_id).get("final", {}).get("estimate_usd", 0)
    if not a.yes:
        reply = input(f"Approve FULL-PRICE render for job {a.job_id} (~${est:.2f})? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            _p("cancelled — nothing spent")
            return
    gen = _run(jobs.approve, a.job_id, override_by=a.override)
    _p(f"final generation {gen} started")
    if a.wait:
        jobs.wait_idle(1800)
        cmd_job_show(a)


def cmd_job_reject(a):
    _run(jobs.reject, a.job_id, a.reason or "")
    _p(f"job {a.job_id} rejected — no further spend")


def cmd_job_show(a):
    from app.api import get_job
    j = _run(get_job, a.job_id)
    _p(f"job {j['id']}  {j['status']}  {j['client_name']}/{j['persona_name']}")
    for g in j["generations"]:
        actual = f"${g['actual_cost_usd']:.4f}" if g["actual_cost_usd"] is not None else "—"
        _p(f"  gen{g['id']:<3} {g['stage']:<6} {g['status']:<10} "
           f"est ${g['estimated_cost_usd']:.4f}  actual {actual}  {g['output_url'] or ''}")
        if g["error"]:
            _p(f"        error: {g['error'][:120]}")
    _p(f"  TOTAL ${j['cost']['total_usd']:.4f}")


def cmd_ledger(a):
    from app.api import client_ledger
    d = _run(client_ledger, a.client_id)
    _p(f"{d['client']['name']}  {d['period']['start']} → {d['period']['end']}")
    _p(f"  spent ${d['spent_this_month']:.2f} / cap ${d['cap']:.2f}  "
       f"(${d['remaining']:.2f} left, ${d['pending']:.2f} in flight)")
    for g in d["generations"]:
        _p(f"  gen{g['id']:<4} job{g['job']:<4} {g['stage']:<6} {g['status']:<10} "
           f"${g['effective_cost']:.4f}  {g['created_at']}")


def cmd_rates(a):
    from datetime import date
    today = date.today()
    for r in rate_table.all():
        v = r.last_verified.isoformat() if r.verified else "NEVER (placeholder)"
        days = r.age_days(today)
        # Mock rates are pinned far in the future; a negative age is not meaningful.
        age = f"{days}d" if days is not None and days >= 0 else "—"
        flag = "" if (r.verified and not r.is_stale(today)) or r.provider == "mock" else "  <-- FIX"
        _p(f"  {r.key:<34} ${r.usd:<8.4f} {r.unit:<10} verified {v:<22} {age:<5}{flag}")


def main():
    db.init_db()
    ap = argparse.ArgumentParser(prog="kalvid", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    ss = sub.add_parser("setup-storage")
    ss.add_argument("--public", action="store_true",
                    help="make the bucket public (default: private + signed URLs)")
    ss.set_defaults(fn=cmd_setup_storage)
    sub.add_parser("rates").set_defaults(fn=cmd_rates)

    dbp = sub.add_parser("db").add_subparsers(dest="sub", required=True)
    dbp.add_parser("status").set_defaults(fn=cmd_db_status)
    dbp.add_parser("init").set_defaults(fn=cmd_db_init)
    dsc = dbp.add_parser("schema")
    dsc.add_argument("--postgres", action="store_true",
                     help="print the Postgres DDL even when the backend is sqlite")
    dsc.set_defaults(fn=cmd_db_schema)
    dm = dbp.add_parser("migrate")
    dm.add_argument("--source", help="SQLite file to read (default: KALVID_DB)")
    dm.add_argument("--wipe", action="store_true", help="clear target tables first")
    dm.add_argument("-y", "--yes", action="store_true")
    dm.set_defaults(fn=cmd_db_migrate)

    lg = sub.add_parser("ledger")
    lg.add_argument("client_id", type=int)
    lg.set_defaults(fn=cmd_ledger)

    cl = sub.add_parser("client").add_subparsers(dest="sub", required=True)
    ca = cl.add_parser("add")
    ca.add_argument("name")
    ca.add_argument("--cap", type=float, required=True)
    ca.add_argument("--job-cap", type=float, default=0.0)
    ca.add_argument("--contact")
    ca.set_defaults(fn=cmd_client_add)
    cl.add_parser("list").set_defaults(fn=cmd_client_list)

    pe = sub.add_parser("persona").add_subparsers(dest="sub", required=True)
    pa = pe.add_parser("add")
    pa.add_argument("client_id", type=int)
    pa.add_argument("name")
    pa.add_argument("--strategy", default="reference_image")
    pa.add_argument("--ref")
    pa.add_argument("--lock-id")
    pa.add_argument("--notes")
    pa.set_defaults(fn=cmd_persona_add)
    pe.add_parser("list").set_defaults(fn=cmd_persona_list)

    jb = sub.add_parser("job").add_subparsers(dest="sub", required=True)
    ja = jb.add_parser("add")
    ja.add_argument("persona_id", type=int)
    ja.add_argument("brief")
    ja.add_argument("--platform", default="tiktok")
    ja.add_argument("--duration", type=int, default=8)
    ja.add_argument("--cap", type=float, default=0.0)
    ja.set_defaults(fn=cmd_job_add)
    jb.add_parser("list").set_defaults(fn=cmd_job_list)
    for name, fn in (("preview", cmd_job_preview), ("show", cmd_job_show)):
        s = jb.add_parser(name)
        s.add_argument("job_id", type=int)
        s.set_defaults(fn=fn)
    jd = jb.add_parser("draft")
    jd.add_argument("job_id", type=int)
    jd.add_argument("--override")
    jd.add_argument("--wait", action="store_true")
    jd.set_defaults(fn=cmd_job_draft)
    jap = jb.add_parser("approve")
    jap.add_argument("job_id", type=int)
    jap.add_argument("--override")
    jap.add_argument("--wait", action="store_true")
    jap.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    jap.set_defaults(fn=cmd_job_approve)
    jr = jb.add_parser("reject")
    jr.add_argument("job_id", type=int)
    jr.add_argument("--reason")
    jr.set_defaults(fn=cmd_job_reject)

    args = ap.parse_args()
    try:
        args.fn(args)
    finally:
        jobs.shutdown(wait=False)


if __name__ == "__main__":
    main()
