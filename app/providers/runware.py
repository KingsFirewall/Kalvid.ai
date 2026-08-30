"""Runware adapter — the cheap DRAFT-stage provider.

!! VERIFY BEFORE GOING LIVE !!  Same caveat as the fal adapter: Runware's task schema
and model identifiers change, so confirm against current docs and run one capped job
first. Unused while KALVID_DRY_RUN=1.

Runware's REST endpoint takes a list of task objects and responds synchronously for
fast image tasks, so submit() may return a finished result directly; poll() then just
returns what submit already resolved.
"""
from __future__ import annotations

import uuid

import httpx

from ..config import settings
from .base import GenerationRequest, GenerationResult, Provider, ProviderError

API_URL = "https://api.runware.ai/v1"


class RunwareProvider(Provider):
    name = "runware"

    def __init__(self, api_key: str | None = None, timeout: float = 120.0):
        self.api_key = api_key or settings.runware_api_key
        self._client = httpx.Client(timeout=timeout)
        self._done: dict[str, GenerationResult] = {}

    def available(self) -> bool:
        # Not just "a key is set" — a leftover YOUR_... placeholder must not read as
        # configured, or the router would pick this provider and fail at call time.
        return settings.provider_configured("runware")

    def submit(self, req: GenerationRequest) -> GenerationResult:
        if not self.available():
            raise ProviderError("runware: RUNWARE_API_KEY is not set")
        task_uuid = str(uuid.uuid4())
        task: dict = {
            "taskType": "videoInference" if req.kind == "video" else "imageInference",
            "taskUUID": task_uuid,
            "model": req.model,
            "positivePrompt": req.prompt,
            "width": req.width,
            "height": req.height,
            "numberResults": 1,
        }
        if req.negative_prompt:
            task["negativePrompt"] = req.negative_prompt
        if req.kind == "video" and req.duration_s:
            task["duration"] = int(req.duration_s)
        if req.reference_image_url:
            task["referenceImages"] = [req.reference_image_url]
        if req.seed is not None:
            task["seed"] = req.seed
        task.update(req.extra)

        r = self._client.post(
            API_URL,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=[task],
        )
        if r.status_code >= 400:
            raise ProviderError(f"runware submit {r.status_code}: {r.text[:400]}")
        body = r.json()

        if isinstance(body, dict) and body.get("errors"):
            msg = str(body["errors"])[:400]
            raise ProviderError(f"runware error: {msg}")

        items = body.get("data", body) if isinstance(body, dict) else body
        item = items[0] if isinstance(items, list) and items else {}
        url = item.get("videoURL") or item.get("imageURL") or item.get("outputURL")
        # Runware reports its real charge per task — the one place we get a true cost.
        cost = item.get("cost")

        if url:
            res = GenerationResult(
                status="succeeded", provider_job_id=task_uuid, output_url=url,
                cost_usd=float(cost) if cost is not None else None, raw=item,
            )
            self._done[task_uuid] = res
            return res
        return GenerationResult(status="running", provider_job_id=task_uuid, raw=item)

    def poll(self, provider_job_id: str, req: GenerationRequest) -> GenerationResult:
        if provider_job_id in self._done:
            return self._done[provider_job_id]
        # An async Runware task is fetched by taskUUID; if it is not resolved yet the
        # worker simply polls again on the next tick.
        try:
            r = self._client.post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=[{"taskType": "getResponse", "taskUUID": provider_job_id}],
            )
            body = r.json()
            items = body.get("data", []) if isinstance(body, dict) else body
            item = items[0] if items else {}
            url = item.get("videoURL") or item.get("imageURL")
            if url:
                cost = item.get("cost")
                return GenerationResult(
                    status="succeeded", provider_job_id=provider_job_id, output_url=url,
                    cost_usd=float(cost) if cost is not None else None, raw=item,
                )
        except Exception as exc:  # a poll hiccup must not kill the job
            return GenerationResult(status="running", provider_job_id=provider_job_id,
                                    error=str(exc))
        return GenerationResult(status="running", provider_job_id=provider_job_id)
