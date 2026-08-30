"""Local mock provider. Never bills, never touches the network.

This is what runs while KALVID_DRY_RUN=1 (the default), so the whole
draft -> approve -> final loop is exercisable end to end for $0.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import struct
import time
import zlib
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
        if stored.kind == "image":
            # A real (if abstract) PNG rather than JSON-with-a-.png-extension, so the
            # asset library and persona pickers are actually exercisable in dry run.
            # Colour is derived from the prompt, so re-running the same prompt looks
            # the same and two different prompts are visibly different.
            out.write_bytes(_placeholder_png(stored.prompt or stored.model))
        else:
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


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _placeholder_png(seed: str, width: int = 360, height: int = 640) -> bytes:
    """A 9:16 gradient PNG, written with stdlib only — no image library to install.

    Dry run exists so the whole loop can be walked for $0. That only works if the
    artifacts are real enough to render: a placeholder the browser refuses to draw
    makes the asset library look broken rather than empty.
    """
    h = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    rows = bytearray()
    for y in range(height):
        t = y / (height - 1)
        r, g, b = colorsys.hsv_to_rgb((h + 0.12 * t) % 1.0, 0.55 - 0.25 * t,
                                      0.22 + 0.5 * t)
        rows.append(0)                                   # filter byte: none
        rows.extend(bytes((int(r * 255), int(g * 255), int(b * 255))) * width)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + _chunk(b"IEND", b""))
