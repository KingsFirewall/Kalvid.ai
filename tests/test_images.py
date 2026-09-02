"""Stills spend real money through the same guard as videos, and must be governed.

The specific failure this file exists to prevent: a still has no job, and spend used
to be found by joining out through jobs. Anything without a job was therefore
invisible to the cap and could be generated past it indefinitely.
"""
import pytest

from app import db, images, jobs, ledger
from app.rates import rate_table


# Dry run deliberately bills $0 (see router.Route), so the guard is exercised the way
# tests/test_budget_guard.py does it: with the REAL rate and billable=False, which
# reserves at the true price without ever making a live call.
def still_rate():
    return rate_table.candidates("still")[0]


def _prompt(text="a woman with curly dark hair, soft window light"):
    return text


def test_a_still_with_no_job_still_counts_against_the_client(client_id, persona_id):
    """THE regression this whole change exists for.

    Spend used to be found by joining generations -> jobs -> personas. A still has no
    job, so under the old query it contributed nothing to the monthly total and could
    be generated past the cap forever.
    """
    before, _ = ledger.client_spend(client_id)
    db.insert(
        """INSERT INTO generations (job_id, persona_id, client_id, stage, provider,
                                    model, status, estimated_cost_usd, actual_cost_usd)
           VALUES (NULL,?,?, 'still','fal','fal-ai/flux/schnell','succeeded', 0.5, 0.5)""",
        (persona_id, client_id),
    )
    after, _ = ledger.client_spend(client_id)
    assert round(after - before, 6) == 0.5, (
        "a job-less generation must be visible to the budget guard")


def test_still_rows_name_their_client_and_have_no_job(client_id, persona_id):
    images.generate(prompt=_prompt(), persona_id=persona_id, count=2)
    jobs.wait_idle(20)
    rows = db.query("SELECT * FROM generations WHERE stage='still'")
    assert len(rows) == 2, "each image is its own reservation, not one batched row"
    for r in rows:
        assert r["job_id"] is None, "a still has no video job"
        assert r["client_id"] == client_id, "but it still names the client it charges"
        assert r["persona_id"] == persona_id


def test_a_still_that_would_breach_the_cap_is_refused(client_id, persona_id):
    rate = still_rate()
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?",
               (rate.estimate() / 2, client_id))
    scope = ledger.scope_for_client(client_id, persona_id)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(stage="still", scope=scope, rate=rate, duration_s=0.0,
                       billable=False)


def test_a_blocked_still_is_written_to_the_audit_log(client_id, persona_id):
    rate = still_rate()
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?",
               (rate.estimate() / 2, client_id))
    scope = ledger.scope_for_client(client_id, persona_id)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(stage="still", scope=scope, rate=rate, duration_s=0.0,
                       billable=False)
    ev = db.query_one("SELECT * FROM budget_events WHERE blocked=1")
    assert ev is not None and ev["client_id"] == client_id
    assert ev["job_id"] is None, "a still's block names no job"


def test_an_override_is_required_and_recorded_to_exceed_the_cap(client_id, persona_id):
    rate = still_rate()
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?",
               (rate.estimate() / 2, client_id))
    scope = ledger.scope_for_client(client_id, persona_id)
    gen_id = ledger.reserve(stage="still", scope=scope, rate=rate, duration_s=0.0,
                            billable=False, override_by="kingsfirewall")
    ev = db.query_one(
        "SELECT * FROM budget_events WHERE generation_id=? AND overridden_by IS NOT NULL",
        (gen_id,))
    assert ev is not None and "OVERRIDE" in ev["note"]


def test_generated_still_becomes_a_reusable_asset(client_id, persona_id):
    images.generate(prompt=_prompt(), persona_id=persona_id, count=1)
    jobs.wait_idle(20)
    assets = images.list_assets(persona_id=persona_id)
    assert len(assets) == 1
    a = assets[0]
    assert a["source"] == "generated" and a["kind"] == "image"
    assert a["generation_id"] is not None and a["url"]


def test_keeping_a_face_requires_a_face_to_keep(client_id):
    """Refused before spending, not discovered after paying for a stranger."""
    pid = db.insert(
        """INSERT INTO personas (client_id, name, identity_strategy, identity_lock_id)
           VALUES (?,?,'character_id','char_123')""", (client_id, "Faceless"))
    with pytest.raises(images.ImageError, match="locked reference"):
        images.generate(prompt=_prompt(), persona_id=pid, keep_face=True, count=1)


def test_the_two_routes_are_different_models_at_different_prices(persona_id):
    new_face = images.preview(persona_id=persona_id, keep_face=False)
    same_face = images.preview(persona_id=persona_id, keep_face=True)
    assert new_face["model"] != same_face["model"]
    assert same_face["estimate_each_usd"] > new_face["estimate_each_usd"], (
        "keeping an identity costs more; if that inverts, re-check rates.json")
    assert same_face["keeps_face"] and not new_face["keeps_face"]


def test_promoting_an_asset_rewrites_the_column_renders_actually_read(client_id, persona_id):
    images.generate(prompt=_prompt(), persona_id=persona_id, count=1)
    jobs.wait_idle(20)
    asset = images.list_assets(persona_id=persona_id)[0]
    persona = images.set_primary(asset["id"])
    assert persona["reference_image_url"] == asset["url"], (
        "a 'primary' asset the renderer never reads would be a label that lies")


def test_the_locked_face_cannot_be_deleted_out_from_under_a_persona(client_id, persona_id):
    images.generate(prompt=_prompt(), persona_id=persona_id, count=1)
    jobs.wait_idle(20)
    asset = images.list_assets(persona_id=persona_id)[0]
    images.set_primary(asset["id"])
    with pytest.raises(images.ImageError):
        images.delete_asset(asset["id"])


def test_a_still_cannot_be_billed_to_a_client_who_does_not_own_the_persona(client_id, persona_id):
    other = db.insert(
        "INSERT INTO clients (name, monthly_budget_cap) VALUES (?,?)", ("Other", 100.0))
    with pytest.raises(ValueError, match="belongs to client"):
        ledger.scope_for_client(other, persona_id)
