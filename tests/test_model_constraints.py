"""What the configured model's real schema imposes on the rest of the system.

Schema per fal's docs for minimax/h3-max/image-to-video:
  prompt, duration (int, default 5), resolution (480P|768P), seed, image_url,
  end_image_url, prompt_expansion_mode, enable_safety_checker, sync_mode.
Notably absent: negative_prompt, width, height.
"""
import dataclasses

import pytest

from app import db, jobs
from app.providers.base import GenerationRequest
from app.providers.fal import FalProvider
from app.rates import rate_table

FINAL = "fal:minimax/h3-max/image-to-video"


# ------------------------------------------------------------ pricing shape

def test_model_bills_by_the_second_not_in_fixed_tiers():
    """The published schema takes a plain integer duration — no 6s/10s tiers.

    But it does have a floor. This test originally asserted that a 3s draft is billed
    as 3s; the first live call returned a 422 saying duration must be >= 5, so the
    assumption was wrong and the code now clamps. Billing by the second and having a
    minimum are different things, and only one of them was in the docs we read.
    """
    r = rate_table.get(FINAL)
    assert r.fixed_durations == (), "no tier list — it really is per-second"
    assert r.min_duration == 5, "…but never shorter than 5s"
    assert r.billed_duration(3) == 5, "a 3s draft is submitted and billed as 5s"
    assert r.billed_duration(7) == 7, "above the floor, per-second is per-second"
    assert r.estimate(duration_s=3) < r.estimate(duration_s=8)


def test_fixed_duration_tiers_still_bill_up_when_a_model_declares_them():
    """The mechanism remains, for models that genuinely have tiers."""
    tiered = dataclasses.replace(rate_table.get(FINAL), fixed_durations=(6, 10))
    assert tiered.billed_duration(3) == 6
    assert tiered.billed_duration(8) == 10
    assert tiered.estimate(duration_s=8) == tiered.estimate(duration_s=10)


# ------------------------------------------------------------ payload schema

def _req(stage: str, duration: float, **kw) -> GenerationRequest:
    r = rate_table.get(FINAL)
    params = r.stage_params(stage)
    return GenerationRequest(
        model=r.model, kind="video", prompt="a prompt",
        duration_s=duration, supports=r.supports,
        resolution=params.get("resolution"),
        extra={k: v for k, v in params.items() if k != "resolution"},
        **kw,
    )


def test_unsupported_fields_are_never_sent():
    """fal validates its input schema; a stray field is an error, not a no-op."""
    body = FalProvider(api_key="x")._payload(
        _req("final", 8, negative_prompt="distorted face, extra fingers"))
    assert "negative_prompt" not in body, "this model has no negative_prompt"
    assert "width" not in body and "height" not in body, "it takes resolution, not pixels"
    assert set(body) <= set(rate_table.get(FINAL).supports)


def test_draft_and_final_differ_by_resolution_tier():
    assert FalProvider(api_key="x")._payload(_req("draft", 3))["resolution"] == "480P"
    assert FalProvider(api_key="x")._payload(_req("final", 8))["resolution"] == "768P"


def test_reference_still_is_sent_as_image_url():
    body = FalProvider(api_key="x")._payload(
        _req("final", 8, reference_image_url="https://example.test/rania.png"))
    assert body["image_url"] == "https://example.test/rania.png"


def test_output_url_is_read_from_the_documented_shape():
    from app.providers.fal import _extract_url
    assert _extract_url({"video": {"url": "https://cdn.test/v.mp4",
                                   "content_type": "video/mp4"}}) == "https://cdn.test/v.mp4"


# ------------------------------------------------------------ identity

def test_persona_job_is_refused_without_a_reference_still(client_id):
    """image_url is optional to the model — omitting it silently yields a stranger."""
    persona_id = db.insert(
        """INSERT INTO personas (client_id, name, identity_strategy, identity_lock_id)
           VALUES (?,?,'lora','some/lora.safetensors')""",
        (client_id, "LoraOnly"),
    )
    job_id = jobs.create_job(persona_id=persona_id, brief="She waves")
    job = jobs._job_bundle(job_id)
    with pytest.raises(jobs.IdentityError, match="unrelated face"):
        jobs._build_request(job, stage="final", rate=rate_table.get(FINAL))


# ------------------------------------------------------------ gate economics

def test_preview_reports_a_healthy_gate_with_default_routing(client_id, persona_id):
    from app.api import preview_job
    job_id = jobs.create_job(persona_id=persona_id, brief="She smiles")
    assert preview_job(job_id)["gate"]["effective"] is True


def test_preview_flags_a_gate_that_is_not_saving_money(client_id, persona_id):
    """A draft that costs most of a final means drafting first stopped paying."""
    from app.api import preview_job

    original_rates = dict(rate_table._rates)
    original_routing = dict(rate_table._routing)
    try:
        # A tiered model makes a 3s draft cost a full 10s tier — same as the final.
        rate_table._rates["fal:tiered"] = dataclasses.replace(
            rate_table.get(FINAL), key="fal:tiered", fixed_durations=(10,))
        rate_table._routing = {"draft": ["fal:tiered"], "final": ["fal:tiered"]}
        job_id = jobs.create_job(persona_id=persona_id, brief="She smiles")
        gate = preview_job(job_id)["gate"]
        assert gate["effective"] is False
        assert "barely saving" in gate["note"]
    finally:
        rate_table._rates = original_rates
        rate_table._routing = original_routing
