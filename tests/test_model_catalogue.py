"""Every routable model must be describable, priced, and correctly serialised.

These are cheap guards against the expensive kind of mistake: fal validates input
schemas strictly and bills per attempt, so a wrongly-shaped `duration` is an HTTP 400
you pay to discover. Nothing here makes a network call.
"""
import pytest

from app.providers.base import GenerationRequest
from app.providers.fal import FalProvider
from app.rates import rate_table

STAGES = ("draft", "final", "lipsync", "still", "still_identity")


def fal_rates():
    return [r for r in rate_table.all() if r.provider == "fal"]


def _payload(rate, **kw):
    req = GenerationRequest(
        model=rate.model, kind=rate.kind, prompt="a woman smiling to camera",
        supports=rate.supports, duration_format=rate.duration_format,
        reference_field=rate.reference_field,
        extra=dict(rate.stage_params(kw.pop("stage", "final"))), **kw)
    return FalProvider(api_key="test")._payload(req)


def test_every_routing_stage_has_at_least_one_usable_model():
    for stage in STAGES:
        usable = [r for r in rate_table.candidates(stage) if r.verified]
        assert usable, f"stage {stage!r} has no verified model — live calls would fail"


def test_every_fal_rate_declares_its_schema_and_a_checked_price():
    """Every rate must have been priced by a human against a citable source.

    Note what this does NOT assert: that the price is still valid today. A promo that
    lapses is an expected state the router handles by demoting that model — asserting
    `r.verified` here made the suite fail at midnight for a system working correctly.
    What must never happen is a rate nobody ever checked.
    """
    for r in fal_rates():
        assert r.supports, f"{r.key} declares no supports; the adapter would guess"
        assert r.last_verified is not None, (
            f"{r.key} has never been priced — a cap computed from it would be fiction")
        assert r.source, f"{r.key} has no source — an unciteable price is a rumour"


def test_a_lapsed_rate_is_demoted_not_fatal():
    """The catalogue exists so one expired price cannot stop production."""
    for stage in STAGES:
        candidates = rate_table.candidates(stage)
        usable = [r for r in candidates if r.verified]
        assert usable, f"stage {stage!r} has no currently-valid price"
        lapsed = [r.key for r in candidates if not r.verified and r.last_verified]
        if lapsed:
            # Informational, and the point of the test: production continues.
            print(f"  {stage}: {len(lapsed)} lapsed ({', '.join(lapsed)}), "
                  f"falling through to {usable[0].key}")


def test_the_adapter_never_sends_a_field_the_model_does_not_accept():
    """The whole point of `supports`: fal rejects unknown fields outright."""
    for r in fal_rates():
        body = _payload(r, duration_s=8.0 if r.kind == "video" else 0.0,
                        reference_image_url="https://example.test/face.png")
        extra = set(body) - set(r.supports)
        assert not extra, f"{r.key} would receive unsupported field(s): {extra}"


@pytest.mark.parametrize("key,expected", [
    ("fal:minimax/h3-max/image-to-video", 8),        # int
    ("fal:fal-ai/wan-25-preview/image-to-video", "10"),   # string enum, snapped up
    ("fal:fal-ai/kling-video/v2.5-turbo/pro/image-to-video", "10"),
    ("fal:fal-ai/veo3.1/image-to-video", "8s"),      # seconds suffix
])
def test_duration_is_serialised_the_way_each_model_spells_it(key, expected):
    """fal is inconsistent here and rejects the wrong shape rather than coercing."""
    rate = rate_table.get(key)
    dur = rate.billed_duration(8.0)
    body = _payload(rate, duration_s=dur,
                    reference_image_url="https://example.test/face.png")
    assert body["duration"] == expected, (
        f"{key} expects {expected!r}, adapter produced {body['duration']!r}")


def test_an_editing_model_gets_a_list_of_references_not_a_single_url():
    rate = rate_table.get("fal:fal-ai/nano-banana/edit")
    body = _payload(rate, reference_image_url="https://example.test/plate.png")
    assert body["image_urls"] == ["https://example.test/plate.png"]
    assert "image_url" not in body


def test_veo_never_generates_its_own_audio():
    """Native audio means a different voice on every clip — and double the price."""
    rate = rate_table.get("fal:fal-ai/veo3.1/image-to-video")
    body = _payload(rate, duration_s=8.0,
                    reference_image_url="https://example.test/face.png")
    assert body["generate_audio"] is False


def test_fixed_duration_models_snap_up_and_are_billed_at_the_tier():
    """A 3s draft on a 5s-minimum model is billed as 5s — the gate stops saving."""
    wan = rate_table.get("fal:fal-ai/wan-25-preview/image-to-video")
    assert wan.billed_duration(3.0) == 5.0
    assert wan.estimate(duration_s=3.0, variant="480p") == pytest.approx(0.25)


def test_every_lipsync_model_actually_accepts_audio():
    for r in rate_table.candidates("lipsync"):
        assert "audio_url" in r.supports, (
            f"{r.key} is routed for lipsync but takes no audio track")


def test_every_video_model_can_carry_a_persona_identity():
    """A text-to-video model returns a stranger. None may be routed to draft/final."""
    for stage in ("draft", "final", "lipsync"):
        for r in rate_table.candidates(stage):
            if not r.verified:
                continue
            assert r.identity_via_image, (
                f"{r.key} is routed to {stage} but does not take a reference image")
            assert "image_url" in r.supports or r.reference_field == "image_urls"


def test_queue_status_uses_the_app_id_not_the_full_endpoint_path():
    """fal submits to the full path but polls under the owning app.

    Getting this wrong is expensive in a specific way: the submit succeeds, the job
    runs and is billed, and only the poll 405s — so you pay for a render and then
    throw it away.
    """
    from app.providers.fal import queue_app
    cases = {
        "fal-ai/flux/schnell": "fal-ai/flux",
        "fal-ai/flux/dev/image-to-image": "fal-ai/flux",
        "minimax/h3-max/image-to-video": "minimax/h3-max",
        "fal-ai/bytedance/seedance/v1/pro/image-to-video": "fal-ai/bytedance",
        "fal-ai/kling-video/v2.5-turbo/pro/image-to-video": "fal-ai/kling-video",
        "fal-ai/nano-banana/edit": "fal-ai/nano-banana",
        "fal-ai/qwen-image": "fal-ai/qwen-image",
    }
    for model, expected in cases.items():
        assert queue_app(model) == expected, f"{model} -> {queue_app(model)}"


def test_every_configured_fal_model_resolves_to_a_two_segment_queue_app():
    from app.providers.fal import queue_app
    for r in fal_rates():
        assert len(queue_app(r.model).split("/")) == 2, (
            f"{r.key} would poll a path fal rejects with 405")


def test_duration_is_clamped_to_what_the_model_accepts():
    """A 3s draft on a 5s-minimum model is a 422, not a short clip.

    fal reports it as a failed *result* — the job is accepted, then rejected — so it
    reads like a render that failed rather than a request that was never valid.
    """
    rate = rate_table.get("fal:minimax/h3-max/image-to-video")
    assert rate.min_duration == 5 and rate.max_duration == 15
    assert rate.billed_duration(3.0) == 5.0, "below the minimum must snap up"
    assert rate.billed_duration(20.0) == 15.0, "above the maximum must clamp down"
    assert rate.billed_duration(8.0) == 8.0


def test_the_draft_request_carries_the_clamped_duration():
    """The clamp has to reach the payload, not just the estimate."""
    from app import jobs
    rate = rate_table.get("fal:minimax/h3-max/image-to-video")
    job = {
        "structured_prompt": '{"prompt": "x"}', "target_duration": 8,
        "persona_name": "T", "identity_strategy": "reference_image",
        "reference_image_url": "https://example.test/f.png", "identity_lock_id": None,
    }
    req = jobs._build_request(job, stage="draft", rate=rate)
    assert req.duration_s >= rate.min_duration, (
        f"submitted duration {req.duration_s} is below the model's minimum")


def test_no_model_is_ever_asked_for_a_duration_it_rejects():
    for r in fal_rates():
        if r.kind != "video" or not r.min_duration:
            continue
        assert r.billed_duration(1.0) >= r.min_duration
        assert r.estimate(duration_s=r.billed_duration(1.0)) > 0
