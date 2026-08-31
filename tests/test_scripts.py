"""Claude writes the dialogue; the deterministic layer keeps the camera.

Two properties are worth pinning. First, the split: adding a model to the pipeline must
not make the visual prompt non-deterministic, or a re-draft stops being a comparison.
Second, the seam: the line an operator approved has to be the line the video model is
told to say, character for character.
"""
import pytest

from app import db, jobs, ledger, scripts
from app.prompts import structure


def _script(**kw):
    kw.setdefault("scene", "she opens the parcel on a bright kitchen counter")
    return scripts.generate(**kw)


def test_dry_run_writes_a_script_without_a_key_or_a_charge(client_id, persona_id):
    out = _script(persona_id=persona_id)
    assert out["dialogue"] and out["visual_direction"]
    assert out["cost_usd"] == 0.0
    assert out["mock"] is True, "dry run must be obviously fake, not silently plausible"


def test_a_script_is_a_metered_call_like_everything_else(client_id, persona_id):
    _script(persona_id=persona_id)
    row = db.query_one("SELECT * FROM generations WHERE stage='script'")
    assert row is not None, "an untracked paid call is the thing this system prevents"
    assert row["client_id"] == client_id
    assert row["job_id"] is None
    assert row["status"] == "succeeded"


def test_the_spoken_line_survives_verbatim_into_the_render_prompt(client_id, persona_id):
    """THE seam. prompts.structure() extracts quoted text unchanged."""
    out = _script(persona_id=persona_id)
    brief = scripts.to_brief(out)
    sp = structure(brief, persona_name="Rania", duration_s=8)
    assert sp.spoken_line == out["dialogue"].strip().strip('"'), (
        "the line the operator approved must reach the model unaltered")


def test_the_visual_prompt_stays_deterministic(client_id, persona_id):
    """Two different scripts must not produce two different camera setups."""
    a = structure(scripts.to_brief(_script(persona_id=persona_id)),
                  persona_name="Rania", duration_s=8)
    b = structure('She does something else entirely and says "a totally different line"',
                  persona_name="Rania", duration_s=8)
    for field in ("shot_type", "camera", "lens", "lighting", "background", "grade"):
        assert getattr(a, field) == getattr(b, field), (
            f"{field} differed between scripts — the visual layer must not vary")


def test_the_same_brief_still_yields_the_same_prompt(client_id, persona_id):
    brief = scripts.to_brief(_script(persona_id=persona_id))
    assert structure(brief, persona_name="Rania").to_dict()["prompt"] == \
           structure(brief, persona_name="Rania").to_dict()["prompt"]


def test_an_empty_scene_is_refused_before_it_costs_anything(client_id, persona_id):
    with pytest.raises(scripts.ScriptError):
        scripts.generate(persona_id=persona_id, scene="   ")
    assert db.query_one("SELECT COUNT(*) n FROM generations WHERE stage='script'")["n"] == 0


def test_an_unknown_persona_is_refused(client_id):
    with pytest.raises(KeyError):
        scripts.generate(persona_id=9999, scene="anything")


def test_script_spend_counts_against_the_client_cap(client_id, persona_id):
    """Cheap is not free — the rate is real even when dry run bills $0."""
    from app.rates import rate_table
    rate = rate_table.get(scripts.RATE_KEY)
    db.execute("UPDATE clients SET monthly_budget_cap=? WHERE id=?",
               (rate.usd / 2, client_id))
    scope = ledger.scope_for_client(client_id, persona_id)
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(stage="script", scope=scope, rate=rate, duration_s=0.0,
                       billable=False)


def test_token_cost_is_computed_from_measured_usage():
    """Claude bills per token; the flat rate is only the reservation estimate."""
    from app.rates import rate_table
    rate = rate_table.get(scripts.RATE_KEY)
    assert rate.token_billed
    # 1M in + 1M out at $5 / $25.
    assert rate.token_cost(1_000_000, 1_000_000) == pytest.approx(30.0)
    assert rate.token_cost(500, 400) == pytest.approx(500/1e6*5 + 400/1e6*25)


def test_a_script_pins_the_identity_it_was_written_for(client_id, persona_id):
    jobs.create_job(persona_id=persona_id, brief="seed the identity")
    _script(persona_id=persona_id)
    row = db.query_one("SELECT identity_version_id FROM generations WHERE stage='script'")
    assert row["identity_version_id"] is not None
