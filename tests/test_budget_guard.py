"""The cap must actually hold — including under concurrency and on unverified prices."""
import threading

import pytest

from app import db, jobs, ledger
from app.rates import UnverifiedRate, rate_table


def final_rate():
    """The configured final-stage rate, whatever model is routed there today."""
    return rate_table.candidates("final")[0]


def _job(persona_id, **kw):
    return jobs.create_job(persona_id=persona_id, brief="She smiles to camera", **kw)


def _spend(client_id, amount, job_id, status="succeeded"):
    """Write a settled generation directly, to set up a spend history."""
    gen_id = db.insert(
        """INSERT INTO generations (job_id, stage, provider, model, status,
                                    estimated_cost_usd, actual_cost_usd)
           VALUES (?,'final','fal','minimax/h3-max/image-to-video',?,?,?)""",
        (job_id, status, amount, amount),
    )
    return gen_id


def test_cap_blocks_and_logs_the_block(client_id, persona_id):
    job_id = _job(persona_id)
    rate = final_rate()
    cost = rate.estimate(duration_s=8, variant=rate.variant_for('final'))
    # Cap set so the existing spend leaves no room for one more render.
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?", (cost, client_id))
    _spend(client_id, cost * 0.9, job_id)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(job_id=job_id, stage="final", rate=rate,
                       duration_s=8, billable=False)

    blocked = db.query_one(
        "SELECT * FROM budget_events WHERE blocked=1 AND client_id=?", (client_id,))
    assert blocked is not None, "a blocked call must leave an audit row"
    assert blocked["cap_at_time"] == cost


def test_explicit_override_is_allowed_and_attributed(client_id, persona_id):
    job_id = _job(persona_id)
    rate = final_rate()
    cost = rate.estimate(duration_s=8, variant=rate.variant_for("final"))
    # Cap deliberately below one render, so the reservation can only pass by override.
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?", (cost / 2, client_id))

    gen_id = ledger.reserve(job_id=job_id, stage="final", rate=rate, duration_s=8,
                            billable=False, override_by="kingsfirewall")
    ev = db.query_one(
        "SELECT * FROM budget_events WHERE generation_id=? AND overridden_by IS NOT NULL",
        (gen_id,))
    assert ev["overridden_by"] == "kingsfirewall"
    assert "OVERRIDE" in ev["note"]


def test_pending_reservations_count_against_the_cap(client_id, persona_id):
    """The hole this closes: two calls in flight both 'fitting' under the same cap."""
    job_id = _job(persona_id)
    rate = final_rate()
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?",
               (rate.estimate(duration_s=8, variant=rate.variant_for('final')), client_id))   # room for exactly one

    ledger.reserve(job_id=job_id, stage="final", rate=rate, duration_s=8, billable=False)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(job_id=job_id, stage="final", rate=rate, duration_s=8,
                       billable=False)


def test_concurrent_reservations_cannot_both_slip_under_the_cap(client_id, persona_id):
    job_id = _job(persona_id)
    rate = final_rate()
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?",
               (rate.estimate(duration_s=8, variant=rate.variant_for('final')), client_id))   # room for exactly one

    results, barrier = [], threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            results.append(("ok", ledger.reserve(job_id=job_id, stage="final", rate=rate,
                                                 duration_s=8, billable=False)))
        except ledger.BudgetExceeded:
            results.append(("blocked", None))
        except Exception as exc:
            results.append(("error", repr(exc)))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=15)

    kinds = sorted(k for k, _ in results)
    assert kinds == ["blocked", "ok"], f"expected exactly one to win, got {results}"


def test_cancelled_reservation_frees_the_budget(client_id, persona_id):
    job_id = _job(persona_id)
    rate = final_rate()
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?",
               (rate.estimate(duration_s=8, variant=rate.variant_for('final')), client_id))   # room for exactly one

    gen_id = ledger.reserve(job_id=job_id, stage="final", rate=rate, duration_s=8,
                            billable=False)
    ledger.release(gen_id, "provider never called")
    # Budget is free again, so a second reservation now fits.
    ledger.reserve(job_id=job_id, stage="final", rate=rate, duration_s=8, billable=False)


def test_per_job_cap_is_enforced_alongside_client_cap(client_id, persona_id):
    rate = final_rate()
    cost = rate.estimate(duration_s=8, variant=rate.variant_for("final"))
    db.execute("UPDATE clients SET monthly_budget_cap=1000.0 WHERE id=?", (client_id,))
    job_id = _job(persona_id, job_budget_cap=cost / 2)   # generous client, tight job
    with pytest.raises(ledger.BudgetExceeded, match="job budget"):
        ledger.reserve(job_id=job_id, stage="final", rate=rate, duration_s=8,
                       billable=False)


def test_unverified_price_refuses_a_billable_call(client_id, persona_id):
    """A placeholder price makes the cap a lie, so a real call must not ride on one."""
    job_id = _job(persona_id)
    rate = next(r for r in rate_table.all() if not r.verified)
    with pytest.raises(UnverifiedRate):
        ledger.reserve(job_id=job_id, stage="final", rate=rate, duration_s=8,
                       billable=True)


def test_a_lapsed_promotional_price_reads_as_unverified():
    """A promo that has ended must stop backing live calls until it is re-checked."""
    import dataclasses
    from datetime import date, timedelta

    rate = final_rate()
    assert rate.verified, "the configured final rate should currently be verified"
    lapsed = dataclasses.replace(rate, price_expires=date.today() - timedelta(days=1))
    assert not lapsed.verified
    with pytest.raises(UnverifiedRate):
        rate_table.require_billable(lapsed)


def test_cost_drift_is_flagged_for_reverification(client_id, persona_id):
    job_id = _job(persona_id)
    gen_id = db.insert(
        """INSERT INTO generations (job_id, stage, provider, model, status,
                                    estimated_cost_usd)
           VALUES (?,'final','fal','minimax/h3-max/image-to-video','running', 1.00)""",
        (job_id,),
    )
    report = ledger.settle(gen_id, status="succeeded", actual_cost_usd=3.00)
    assert report["drift_warning"] and report["drift_pct"] == 200.0
    ev = db.query_one("SELECT note FROM budget_events WHERE generation_id=?", (gen_id,))
    assert "COST DRIFT" in ev["note"]
