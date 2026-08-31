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


def test_every_fal_rate_declares_its_schema_and_a_verified_price():
    for r in fal_rates():
        assert r.supports, f"{r.key} declares no supports; the adapter would guess"
        assert r.verified, f"{r.key} has no verified price"
        assert r.source, f"{r.key} has no source — an unciteable price is a rumour"


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
