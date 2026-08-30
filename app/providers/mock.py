"""Local mock provider. Never bills, never touches the network.

This is what runs while KALVID_DRY_RUN=1 (the default), so the whole
draft -> approve -> final loop is exercisable end to end for $0.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..config import settings
from .base import GenerationRequest, GenerationResult, Provider

# Put this literal in a brief to force the failure path — used by the tests.
FAIL_TRIGGER = "FAIL_TEST"


class MockProvider(Provider):
    name = "mock"
    billable = False

    def __init__(self):
        self._jobs: dict[str, tuple[float, GenerationRequest]] = {}

    def available(self) -> bool:
        return True

    def submit(self, req: GenerationRequest) -> GenerationResult:
        job_id = "mock_" + hashlib.sha256(
            f"{req.model}{req.prompt}{time.time()}".encode()
        ).hexdigest()[:16]
        self._jobs[job_id] = (time.time(), req)
        return GenerationResult(status="running", provider_job_id=job_id)

    def poll(self, provider_job_id: str, req: GenerationRequest) -> GenerationResult:
        started, stored = self._jobs.get(provider_job_id, (0.0, req))
        if time.time() - started < 1.0:          # brief 'render' so polling is real
            return GenerationResult(status="running", provider_job_id=provider_job_id)

        if FAIL_TRIGGER in stored.prompt:
            # Mirrors reality: many providers charge even for a failed generation.
            return GenerationResult(
                status="failed", provider_job_id=provider_job_id,
                cost_usd=0.0, error="mock: forced failure via FAIL_TEST trigger",
            )

        ext = "png" if stored.kind == "image" else "mp4"
        out = settings.output_dir / "mock" / f"{provider_job_id}.{ext}"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "note": "placeholder artifact from the mock provider — not real media",
            "model": stored.model, "kind": stored.kind,
            "duration_s": stored.duration_s,
            "reference_image_url": stored.reference_image_url,
            "prompt": stored.prompt,
        }, indent=2))
        return GenerationResult(
            status="succeeded", provider_job_id=provider_job_id,
            output_url=str(out), cost_usd=0.0,
        )
