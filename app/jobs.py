"""Job orchestrator: the draft-then-final gate.

State machine (only these transitions exist):

    created ─┐
             ├─> drafting ─> draft_ready ─> approved ─> rendering ─> complete
    draft_ready ┘                │                          │
                                 └──────> rejected <────────┘
    (drafting|rendering) ─> failed ─> drafting   (retry is a human re-draft, never automatic)

Two properties this file exists to guarantee:

  1. A final render is reachable ONLY through draft_ready. There is no code path from
     'created' straight to a full-price render.
  2. Every transition into a spending state is an atomic conditional UPDATE. A
     double-clicked Approve loses the race and fires nothing — the second click sees
     zero rows updated and is rejected, rather than paying twice.

Renders take minutes, so provider calls run on a background worker and the dashboard
polls. That is not "scaling ahead of need" — a synchronous final render would blow
past any sane HTTP timeout with a single client.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db, ledger, storage
from . import identity as identity_mod
from .config import settings
from .identity import binding_from_version
from .prompts import structure
from .providers.base import GenerationRequest, ProviderError
from .rates import UnverifiedRate
from .router import NoProviderAvailable, router

log = logging.getLogger(__name__)

# A still draft proves identity, framing and lighting but says nothing about motion —
# and motion artifacts (warping faces, broken hands) are what actually ruin a take.
# So the draft defaults to a SHORT LOW-RES CLIP, which is the cheap check that
# actually predicts the final. Set KALVID_DRAFT_KIND=image to fall back to a still.
DRAFT_KIND = os.getenv("KALVID_DRAFT_KIND", "video")
DRAFT_DURATION_S = float(os.getenv("KALVID_DRAFT_DURATION", "3"))
DRAFT_WIDTH, DRAFT_HEIGHT = 360, 640          # quarter-res preview

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_shutdown = threading.Event()


def _pool() -> ThreadPoolExecutor:
    """The worker pool, created on demand.

    Lazy so that a shutdown (an app reload, or a test client closing its lifespan)
    does not permanently disable generation — the next submit builds a fresh pool.
    """
    global _executor
    with _executor_lock:
        if _executor is None:
            _shutdown.clear()
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kalvid-gen")
        return _executor


class TransitionError(Exception):
    """The job was not in a state that allows this action."""


class IdentityError(Exception):
    """The persona has no usable identity lock; generating would waste the spend."""


# ---------------------------------------------------------------- job lifecycle

def create_job(*, persona_id: int, brief: str, platform: str = "tiktok",
               target_duration: int = 8, job_budget_cap: float = 0.0) -> int:
    persona = db.query_one("SELECT * FROM personas WHERE id=?", (persona_id,))
    if persona is None:
        raise KeyError(f"persona {persona_id} not found")

    # Pin the identity now. If the persona is later edited to v2, this job keeps
    # rendering — and re-drafting — as the person it was briefed for.
    version = identity_mod.current_version(persona_id)
    if version is None:
        version = identity_mod.lock(persona_id, "auto") \
            if identity_mod.draft_version(persona_id) \
            else identity_mod.edit(persona_id, "auto")

    sp = structure(
        brief,
        persona_name=persona["name"],
        persona_notes=persona["notes"] or "",
        platform=platform,
        duration_s=target_duration,
    )
    return db.insert(
        """INSERT INTO jobs (persona_id, identity_version_id, brief, structured_prompt,
                             platform, target_duration, job_budget_cap, status)
           VALUES (?,?,?,?,?,?,?, 'created')""",
        (persona_id, version["id"], brief, json.dumps(sp.to_dict()), platform,
         target_duration, job_budget_cap),
    )


def _transition(job_id: int, to: str, allowed_from: tuple[str, ...]) -> bool:
    """Atomic guarded transition. False means the job was not in an allowed state."""
    placeholders = ",".join("?" * len(allowed_from))
    cur = db.execute(
        f"""UPDATE jobs SET status=?, updated_at=CURRENT_TIMESTAMP
             WHERE id=? AND status IN ({placeholders})""",
        (to, job_id, *allowed_from),
    )
    return cur.rowcount > 0


def _job_bundle(job_id: int):
    """The job plus the identity it was PINNED to.

    Deliberately reads identity_strategy / reference_image_url / identity_lock_id from
    identity_versions and not from personas: a job briefed against v1 must render as
    v1 forever, including on a re-draft made after the persona moved to v2. Falling
    back to the persona row covers a job created before versioning existed.
    """
    row = db.query_one(
        """SELECT j.*, p.name AS persona_name, p.notes AS persona_notes,
                  COALESCE(iv.identity_strategy,   p.identity_strategy)   AS identity_strategy,
                  COALESCE(iv.reference_image_url, p.reference_image_url) AS reference_image_url,
                  COALESCE(iv.identity_lock_id,    p.identity_lock_id)    AS identity_lock_id,
                  iv.version AS identity_version
             FROM jobs j
             JOIN personas p ON p.id = j.persona_id
             LEFT JOIN identity_versions iv ON iv.id = j.identity_version_id
            WHERE j.id = ?""",
        (job_id,),
    )
    if row is None:
        raise KeyError(f"job {job_id} not found")
    return row


def _stage_shape(job, stage: str) -> tuple[str, float]:
    """(kind, duration) for a stage — known before a provider is chosen."""
    if stage == "draft":
        kind = DRAFT_KIND
        return kind, (min(DRAFT_DURATION_S, job["target_duration"])
                      if kind == "video" else 0.0)
    return "video", float(job["target_duration"])


def _build_request(job, *, stage: str, rate=None) -> GenerationRequest:
    sp = json.loads(job["structured_prompt"] or "{}")
    kind, duration = _stage_shape(job, stage)
    # Clamp to what the chosen model will actually accept. Without this a 3s draft is
    # submitted to a 5s-minimum model and comes back as a 422 that reads like a failed
    # render — after the reservation has already been taken.
    if rate is not None and kind == "video":
        duration = rate.billed_duration(duration)
    params = rate.stage_params(stage) if rate is not None else {}

    req = GenerationRequest(
        model=rate.model if rate is not None else "",
        kind=kind,
        prompt=sp.get("prompt") or job["brief"],
        negative_prompt=sp.get("negative_prompt", ""),
        duration_s=duration,
        # Models taking a resolution enum use it; the rest fall back to pixel
        # dimensions. The draft tier is the cheap lever (480P vs 768P on h3-max).
        resolution=params.get("resolution"),
        width=DRAFT_WIDTH if stage == "draft" else sp.get("width", 720),
        height=DRAFT_HEIGHT if stage == "draft" else sp.get("height", 1280),
        supports=rate.supports if rate is not None else (),
        duration_format=rate.duration_format if rate is not None else "int",
        reference_field=rate.reference_field if rate is not None else "image_url",
        extra={k: v for k, v in params.items() if k != "resolution"},
    )

    binding = binding_from_version(job)
    problems = binding.blocking_problems
    if problems:
        raise IdentityError(
            f"persona '{job['persona_name']}' has no usable identity lock: "
            + "; ".join(problems)
        )
    binding.apply(req)

    # This model carries the persona's face through image_url. It will happily run
    # text-to-video without one and hand back a stranger, so refuse before paying.
    if rate is not None and rate.identity_via_image and not req.reference_image_url:
        raise IdentityError(
            f"model {rate.model!r} carries persona identity through its reference "
            f"still, but persona '{job['persona_name']}' has no reference_image_url. "
            f"Without it the render would return an unrelated face."
        )
    return req


def _start_generation(job_id: int, stage: str, override_by: str | None) -> int:
    """Reserve budget, then hand the call to the background worker."""
    job = _job_bundle(job_id)
    kind, duration = _stage_shape(job, stage)
    route = router.resolve(stage, kind=kind)

    # Build against the chosen model, so its schema and identity requirements apply.
    req = _build_request(job, stage=stage, rate=route.rate)
    # The mock provider must be handed a mock model id; a live one gets the real model.
    req.model = route.charged.model if not route.billable else route.rate.model

    gen_id = ledger.reserve(
        job_id=job_id, identity_version_id=job["identity_version_id"],
        stage=stage, rate=route.charged, duration_s=duration,
        variant=route.charged.variant_for(stage),
        payload=json.dumps({"model": req.model, "kind": req.kind,
                            "duration_s": duration, "resolution": req.resolution,
                            "prompt": req.prompt[:2000]}),
        billable=route.billable, override_by=override_by,
    )
    _pool().submit(_run_generation, gen_id, job_id, stage,
                   route.provider.name, req, route.billable)
    return gen_id


def start_draft(job_id: int, *, override_by: str | None = None) -> int:
    """Draft or re-draft. Cheap by construction, and the only route to a final."""
    if not _transition(job_id, "drafting", ("created", "draft_ready", "failed")):
        cur = _job_bundle(job_id)["status"]
        raise TransitionError(f"cannot draft a job in status {cur!r}")
    try:
        return _start_generation(job_id, "draft", override_by)
    except Exception:
        _transition(job_id, "failed", ("drafting",))
        raise


def approve(job_id: int, *, override_by: str | None = None) -> int:
    """THE expensive step. Only reachable from draft_ready, and only once."""
    job = _job_bundle(job_id)
    if job["status"] != "draft_ready":
        raise TransitionError(
            f"cannot approve a job in status {job['status']!r} — a reviewed draft is "
            f"required before a full-price render"
        )
    ok = db.query_one(
        """SELECT 1 FROM generations
            WHERE job_id=? AND stage='draft' AND status='succeeded' LIMIT 1""",
        (job_id,),
    )
    if not ok:
        raise TransitionError("no successful draft exists for this job")

    # Atomic: a second concurrent approve finds status already 'approved' and stops.
    if not _transition(job_id, "approved", ("draft_ready",)):
        raise TransitionError("job already approved — refusing a duplicate render")
    try:
        gen_id = _start_generation(job_id, "final", override_by)
        _transition(job_id, "rendering", ("approved",))
        return gen_id
    except Exception:
        _transition(job_id, "draft_ready", ("approved",))   # let a human retry
        raise


def reject(job_id: int, reason: str = "") -> None:
    if not _transition(job_id, "rejected", ("created", "drafting", "draft_ready",
                                            "approved", "failed")):
        raise TransitionError("job cannot be rejected from its current status")
    if reason:
        db.execute("UPDATE jobs SET brief = brief || ? WHERE id=?",
                   (f"\n\n[rejected: {reason}]", job_id))


# ---------------------------------------------------------------- worker

def _run_generation(gen_id: int, job_id: int, stage: str, provider_name: str,
                    req: GenerationRequest, billable: bool) -> None:
    """Submit, poll to completion, settle the ledger, advance the job."""
    provider = router.get(provider_name)
    try:
        result = provider.submit(req)
        ledger.mark_running(gen_id, result.provider_job_id)

        deadline = time.time() + settings.poll_timeout_s
        while result.status == "running" and not _shutdown.is_set():
            if time.time() > deadline:
                result.status = "failed"
                result.error = f"timed out after {settings.poll_timeout_s:.0f}s"
                break
            time.sleep(settings.poll_interval_s)
            result = provider.poll(result.provider_job_id, req)

        if _shutdown.is_set() and result.status == "running":
            log.warning("gen %s still running at shutdown; left as running", gen_id)
            return

        # Provider output URLs expire. Archive to durable storage before settling so
        # the ledger points at something that will still resolve next month.
        output_url = result.output_url
        if result.status == "succeeded" and output_url:
            client_name = db.query_one(
                """SELECT c.name FROM jobs j
                     JOIN personas p ON p.id = j.persona_id
                     JOIN clients  c ON c.id = p.client_id
                    WHERE j.id = ?""", (job_id,))["name"]
            output_url = storage.archive_output(
                output_url, client_name=client_name, job_id=job_id,
                gen_id=gen_id, kind=req.kind,
            )

        report = ledger.settle(
            gen_id, status=result.status, actual_cost_usd=result.cost_usd,
            output_url=output_url, error=result.error,
        )
        if report["drift_warning"]:
            log.warning("gen %s cost drift %+.1f%% (est $%.4f, actual $%.4f)",
                        gen_id, report["drift_pct"], report["estimated"], report["actual"])

        if result.status == "succeeded":
            _transition(job_id, "draft_ready" if stage == "draft" else "complete",
                        ("drafting",) if stage == "draft" else ("rendering", "approved"))
        else:
            # No automatic retry, ever. A failed generation goes back to a human for a
            # prompt tweak — blind auto-retry is exactly how credits vanish silently.
            _transition(job_id, "failed",
                        ("drafting",) if stage == "draft" else ("rendering", "approved"))
            log.error("gen %s (%s/%s) failed: %s", gen_id, stage, provider_name,
                      result.error)

    except (ProviderError, NoProviderAvailable, UnverifiedRate) as exc:
        ledger.settle(gen_id, status="failed", actual_cost_usd=0.0,
                      error=f"{type(exc).__name__}: {exc}")
        _transition(job_id, "failed", ("drafting", "rendering", "approved"))
        log.error("gen %s never fired: %s", gen_id, exc)
    except Exception as exc:                                   # never lose a reservation
        ledger.settle(gen_id, status="failed", error=f"unexpected: {exc}")
        _transition(job_id, "failed", ("drafting", "rendering", "approved"))
        log.exception("gen %s crashed", gen_id)


def shutdown(wait: bool = True) -> None:
    global _executor
    with _executor_lock:
        _shutdown.set()
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None


def wait_idle(timeout: float = 60.0) -> bool:
    """Block until nothing is in flight. For tests and CLI runs.

    Checks jobs as well as generations: the worker settles the ledger row a moment
    before it transitions the job, so waiting on generations alone can return while a
    job is still mid-transition.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.query_one(
            """SELECT (SELECT COUNT(*) FROM generations
                        WHERE status IN ('pending','running'))
                     + (SELECT COUNT(*) FROM jobs
                        WHERE status IN ('drafting','approved','rendering')) AS n""")
        if row["n"] == 0:
            return True
        time.sleep(0.1)
    return False
