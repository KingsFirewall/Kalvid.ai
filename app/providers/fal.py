"""fal.ai adapter — the primary FINAL-stage provider.

!! VERIFY BEFORE GOING LIVE !!
The queue endpoints and payload keys below follow fal's documented queue API shape,
but fal versions its model endpoints and payload fields per model. Check the current
docs for the exact model you configure in rates.json, then run one job with a tiny
budget cap before trusting it. `KALVID_DRY_RUN=1` (the default) keeps this file
entirely unused until you deliberately switch it off.

fal does not return a per-request price in its API response, so cost_usd is left None
and the ledger falls back to the pre-call estimate from rates.json. That is exactly why
rates.json must carry real, verified numbers.
"""
from __future__ import annotations

import httpx

from ..config import settings
from .base import GenerationRequest, GenerationResult, Provider, ProviderError

QUEUE_BASE = "https://queue.fal.run"


def queue_app(model: str) -> str:
    """The id the QUEUE knows a model by — its first two path segments.

    fal submits to the full endpoint path but exposes status and result under the
    owning app only:

        POST https://queue.fal.run/fal-ai/flux/schnell          <- full path
        GET  https://queue.fal.run/fal-ai/flux/requests/{id}/status   <- app only

    Using the full path for status returns 405 Method Not Allowed, which is easy to
    misread as a transport problem. It is not: the submitted job runs and is billed,
    and only the polling fails — so the render is paid for and then abandoned.
    Verified against every model in rates.json.
    """
    return "/".join(model.split("/")[:2])


class FalProvider(Provider):
    name = "fal"

    def __init__(self, api_key: str | None = None, timeout: float = 60.0):
        self.api_key = api_key or settings.fal_api_key
        self._client = httpx.Client(timeout=timeout)

    def available(self) -> bool:
        # Not just "a key is set" — a leftover YOUR_... placeholder must not read as
        # configured, or the router would pick this provider and fail at call time.
        return settings.provider_configured("fal")

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Key {self.api_key}", "Content-Type": "application/json"}

    def _payload(self, req: GenerationRequest) -> dict:
        """Build the request body, sending ONLY fields the model declares.

        fal validates its input schema, and models differ sharply — minimax/h3-max
        takes `resolution` (480P/768P) and has no `negative_prompt` at all, while
        others take pixel dimensions. Anything outside `req.supports` is dropped
        rather than guessed at.
        """
        candidate: dict = {"prompt": req.prompt}

        if req.negative_prompt:
            # Not universally supported. Never fold it into the positive prompt as a
            # fallback — models happily render the very terms you meant to exclude.
            candidate["negative_prompt"] = req.negative_prompt
        if req.reference_image_url:
            # The persona's locked still. For an image-to-video model this is what
            # carries the face across clips, and it also sets the output aspect ratio.
            # Editing models (nano-banana) take a LIST under a different key.
            if req.reference_field == "image_urls":
                candidate["image_urls"] = [req.reference_image_url]
            else:
                candidate["image_url"] = req.reference_image_url
        if req.end_image_url:
            candidate["end_image_url"] = req.end_image_url
        if req.kind == "video" and req.duration_s:
            # NOT always an int. wan/kling/seedance want "5"; veo wants "8s". fal
            # rejects the wrong shape outright rather than coercing.
            candidate["duration"] = req.duration_value()
        if req.resolution:
            candidate["resolution"] = req.resolution
        else:
            candidate["width"], candidate["height"] = req.width, req.height
        if req.seed is not None:
            candidate["seed"] = req.seed

        body = {k: v for k, v in candidate.items() if req.accepts(k)}
        # Model-specific extras from rates.json 'params' are already schema-checked.
        body.update({k: v for k, v in req.extra.items() if req.accepts(k)})
        return body

    def submit(self, req: GenerationRequest) -> GenerationResult:
        if not self.available():
            raise ProviderError("fal: FAL_KEY is not set")
        r = self._client.post(
            f"{QUEUE_BASE}/{req.model}", headers=self._headers, json=self._payload(req)
        )
        if r.status_code >= 400:
            raise ProviderError(f"fal submit {r.status_code}: {r.text[:400]}")
        data = r.json()
        rid = data.get("request_id")
        if not rid:
            raise ProviderError(f"fal submit returned no request_id: {data}")
        return GenerationResult(status="running", provider_job_id=rid, raw=data)

    def poll(self, provider_job_id: str, req: GenerationRequest) -> GenerationResult:
        base = f"{QUEUE_BASE}/{queue_app(req.model)}/requests/{provider_job_id}"
        r = self._client.get(f"{base}/status", headers=self._headers)
        if r.status_code >= 400:
            raise ProviderError(f"fal status {r.status_code}: {r.text[:400]}")
        status = (r.json() or {}).get("status", "").upper()

        if status in ("IN_QUEUE", "IN_PROGRESS"):
            return GenerationResult(status="running", provider_job_id=provider_job_id)

        res = self._client.get(base, headers=self._headers)
        if res.status_code >= 400:
            return GenerationResult(
                status="failed", provider_job_id=provider_job_id,
                error=f"fal result {res.status_code}: {res.text[:400]}",
            )
        data = res.json()
        url = _extract_url(data)
        if not url:
            return GenerationResult(
                status="failed", provider_job_id=provider_job_id,
                error=f"fal returned no output url: {str(data)[:400]}", raw=data,
            )
        return GenerationResult(
            status="succeeded", provider_job_id=provider_job_id,
            output_url=url, cost_usd=None, raw=data,
        )


def _extract_url(data: dict) -> str | None:
    """fal nests output under different keys per model; check the usual shapes."""
    for key in ("video", "image", "audio"):
        node = data.get(key)
        if isinstance(node, dict) and node.get("url"):
            return node["url"]
    for key in ("images", "videos", "outputs"):
        node = data.get(key)
        if isinstance(node, list) and node:
            first = node[0]
            if isinstance(first, dict) and first.get("url"):
                return first["url"]
            if isinstance(first, str):
                return first
    if isinstance(data.get("url"), str):
        return data["url"]
    return None
