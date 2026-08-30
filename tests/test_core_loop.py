"""The properties the whole system exists to guarantee."""
import pytest

from app import db, jobs, ledger


def _job(persona_id, brief="She holds up the serum and smiles", **kw):
    return jobs.create_job(persona_id=persona_id, brief=brief, **kw)


# ------------------------------------------------ the draft-then-final gate

def test_cannot_reach_final_without_a_draft(persona_id):
    job_id = _job(persona_id)
    with pytest.raises(jobs.TransitionError, match="reviewed draft is required"):
        jobs.approve(job_id)
    assert db.query_one("SELECT COUNT(*) n FROM generations WHERE stage='final'")["n"] == 0


def test_full_loop_draft_then_approve_then_final(persona_id):
    job_id = _job(persona_id)

    jobs.start_draft(job_id)
    assert jobs.wait_idle(20)
    assert db.query_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "draft_ready"

    jobs.approve(job_id)
    assert jobs.wait_idle(20)
    assert db.query_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "complete"

    rows = db.query("SELECT stage, status FROM generations WHERE job_id=? ORDER BY id", (job_id,))
    assert [(r["stage"], r["status"]) for r in rows] == [
        ("draft", "succeeded"), ("final", "succeeded")
    ]


def test_double_approve_fires_only_one_render(persona_id):
    job_id = _job(persona_id)
    jobs.start_draft(job_id); jobs.wait_idle(20)

    jobs.approve(job_id)
    with pytest.raises(jobs.TransitionError, match="already approved|reviewed draft"):
        jobs.approve(job_id)          # the double click

    jobs.wait_idle(20)
    n = db.query_one("SELECT COUNT(*) n FROM generations WHERE job_id=? AND stage='final'",
                     (job_id,))["n"]
    assert n == 1, "a double-clicked Approve must not pay for two renders"


def test_redraft_is_allowed_and_stays_cheap(persona_id):
    job_id = _job(persona_id)
    jobs.start_draft(job_id); jobs.wait_idle(20)
    jobs.start_draft(job_id); jobs.wait_idle(20)     # human asks for another draft
    rows = db.query("SELECT stage FROM generations WHERE job_id=?", (job_id,))
    assert [r["stage"] for r in rows] == ["draft", "draft"]


# ------------------------------------------------ the ledger

def test_every_call_is_logged_before_it_fires(persona_id, monkeypatch):
    """A reservation row must exist at submit time, not after."""
    seen = {}
    from app.providers.mock import MockProvider
    original = MockProvider.submit

    def spy(self, req):
        seen["rows_at_submit"] = db.query_one(
            "SELECT COUNT(*) n FROM generations")["n"]
        return original(self, req)

    monkeypatch.setattr(MockProvider, "submit", spy)
    job_id = _job(persona_id)
    jobs.start_draft(job_id); jobs.wait_idle(20)
    assert seen["rows_at_submit"] == 1


def test_failed_generation_still_costs_and_is_logged(persona_id):
    job_id = _job(persona_id, brief="She speaks to camera FAIL_TEST")
    jobs.start_draft(job_id); jobs.wait_idle(20)

    gen = db.query_one("SELECT * FROM generations WHERE job_id=?", (job_id,))
    assert gen["status"] == "failed"
    assert gen["actual_cost_usd"] is not None, "a failed call must record a real cost"
    assert db.query_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "failed"


def test_no_automatic_retry_after_failure(persona_id):
    job_id = _job(persona_id, brief="She speaks to camera FAIL_TEST")
    jobs.start_draft(job_id); jobs.wait_idle(20)
    jobs.wait_idle(5)
    n = db.query_one("SELECT COUNT(*) n FROM generations WHERE job_id=?", (job_id,))["n"]
    assert n == 1, "a failure must go back to a human, never auto-refire"


def test_provider_cost_of_none_falls_back_to_estimate_not_zero(client_id, persona_id):
    """A silent provider must never be recorded as a free call."""
    job_id = _job(persona_id)
    gen_id = db.insert(
        """INSERT INTO generations (job_id, client_id, stage, provider, model, status,
                                    estimated_cost_usd)
           VALUES (?,?,'final','fal','FINAL_VIDEO_MODEL','running', 4.0)""",
        (job_id, client_id),
    )
    ledger.settle(gen_id, status="succeeded", actual_cost_usd=None, output_url="x")
    assert db.query_one("SELECT actual_cost_usd c FROM generations WHERE id=?",
                        (gen_id,))["c"] == 4.0
