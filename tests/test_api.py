"""HTTP surface: the gate must hold through the API too, not just the Python calls."""
import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.main import app


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded(c):
    cl = c.post("/api/clients", json={"name": "Acme", "monthly_budget_cap": 50,
                                      "default_job_cap": 10}).json()
    p = c.post("/api/personas", json={
        "client_id": cl["id"], "name": "Rania",
        "identity_strategy": "reference_image",
        "reference_image_url": "https://example.test/r.png",
        "notes": "24yo, curly dark hair"}).json()
    j = c.post("/api/jobs", json={"persona_id": p["id"],
                                  "brief": 'She smiles and says "it works"',
                                  "target_duration": 8}).json()
    return cl, p, j


def test_persona_without_identity_lock_is_refused(c):
    cl = c.post("/api/clients", json={"name": "NoLock", "monthly_budget_cap": 10}).json()
    r = c.post("/api/personas", json={"client_id": cl["id"], "name": "Ghost",
                                      "identity_strategy": "reference_image"})
    assert r.status_code == 422
    assert "reference_image_url" in r.json()["detail"]


def test_approve_before_draft_is_409(c, seeded):
    _, _, job = seeded
    r = c.post(f"/api/jobs/{job['id']}/approve", json={})
    assert r.status_code == 409
    assert "draft is required" in r.json()["detail"]


def test_preview_shows_true_cost_even_in_dry_run(c, seeded):
    """A dry run that reported $0 estimates would rehearse nothing."""
    _, _, job = seeded
    p = c.post(f"/api/jobs/{job['id']}/preview").json()
    assert p["final"]["estimate_usd"] > 0, "must show what it WOULD cost"
    assert p["final"]["charged_usd"] == 0, "but must not charge in dry run"
    assert p["final"]["estimate_usd"] > p["draft"]["estimate_usd"], \
        "the draft must be the cheap one — that is the whole point of the gate"


def test_full_flow_over_http(c, seeded):
    _, _, job = seeded
    jid = job["id"]

    assert c.post(f"/api/jobs/{jid}/draft", json={}).status_code == 200
    assert jobs.wait_idle(20)
    assert c.get(f"/api/jobs/{jid}").json()["status"] == "draft_ready"

    assert c.post(f"/api/jobs/{jid}/approve", json={}).status_code == 200
    assert jobs.wait_idle(20)

    final = c.get(f"/api/jobs/{jid}").json()
    assert final["status"] == "complete"
    assert len(final["generations"]) == 2


def test_reject_stops_all_further_spend(c, seeded):
    _, _, job = seeded
    jid = job["id"]
    assert c.post(f"/api/jobs/{jid}/reject", json={"reason": "client killed it"}).status_code == 200
    assert c.get(f"/api/jobs/{jid}").json()["status"] == "rejected"
    # Neither action is reachable on a rejected job.
    assert c.post(f"/api/jobs/{jid}/draft", json={}).status_code == 409
    assert c.post(f"/api/jobs/{jid}/approve", json={}).status_code == 409


def test_status_reports_unverified_rates(c):
    """Every non-mock rate lacking a verified price must be named."""
    from app.rates import rate_table
    st = c.get("/api/status").json()
    assert st["dry_run"] is True
    expected = {r.key for r in rate_table.all()
                if not r.verified and r.provider != "mock"}
    assert set(st["unverified_rates"]) == expected
    assert expected, "fixture expects at least one unverified rate to exist"


def test_dashboard_pages_render(c, seeded):
    cl, _, job = seeded
    for path in ("/", f"/jobs/{job['id']}", f"/clients/{cl['id']}", "/rates"):
        assert c.get(path).status_code == 200, path
