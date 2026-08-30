"""Still generation — the influencer's asset library.

Why this exists separately from jobs.py: a video is expensive, so it goes through a
draft-then-approve gate with a human in the middle. A still is roughly a hundredth of
the price ($0.003–$0.03 against $0.32), so gating it would mean paying for a preview
of a preview. Stills therefore spend directly — but through the *same* ledger, the
same per-client cap, and the same two-phase reserve/settle. Cheap is not free.

Two routes, and the difference matters more than the price does:

  still            text-to-image. Invents a NEW face. This is how you create an
                   influencer in the first place, and how you make product/ad stills.
  still_identity   image-to-image, seeded from the persona's locked reference. This
                   is how you get MORE pictures of an influencer you already have.
                   Ten times the price, and the only one of the two that keeps a face.

Choosing the wrong one is not a rendering flaw, it is a silent identity swap: the
result comes back polished, plausible, and a different person. So the caller states
which it wants, and the UI says which one it is about to run.
"""
from __future__ import annotations

import json
import logging
import time

from . import db, ledger, storage
from .config import settings
from .jobs import _pool, _shutdown
from .providers.base import GenerationRequest, ProviderError
from .rates import UnverifiedRate
from .router import NoProviderAvailable, router

log = logging.getLogger(__name__)

# Cost is per image and each one is reserved separately, so this is a guard against a
# fat-fingered "50" emptying a cap in one click, not a technical limit.
MAX_BATCH = 8


class ImageError(Exception):
    """The request could not be built — a missing face, or a bad persona/client pair."""


def _resolve_target(client_id: int | None, persona_id: int | None):
    """(client_id, persona_row) — a persona implies its client, never the reverse."""
    persona = None
    if persona_id is not None:
        persona = db.query_one("SELECT * FROM personas WHERE id=?", (persona_id,))
        if persona is None:
            raise KeyError(f"persona {persona_id} not found")
        client_id = persona["client_id"]
    if client_id is None:
        raise ImageError("a still must be charged to a client: pass client_id or persona_id")
    if db.query_one("SELECT 1 FROM clients WHERE id=?", (client_id,)) is None:
        raise KeyError(f"client {client_id} not found")
    return client_id, persona


def preview(*, client_id: int | None = None, persona_id: int | None = None,
            keep_face: bool = False, count: int = 1) -> dict:
    """What this would cost and which model would run it — before spending anything."""
    client_id, persona = _resolve_target(client_id, persona_id)
    stage = "still_identity" if keep_face else "still"
    try:
        route = router.resolve(stage, kind="image")
    except (NoProviderAvailable, UnverifiedRate, KeyError) as exc:
        return {"error": str(exc), "stage": stage}

    each = route.rate.estimate(calls=1)
    scope = ledger.scope_for_client(client_id, persona["id"] if persona else None)
    return {
        "stage": stage,
        "keeps_face": keep_face,
        "provider": route.provider.name,
        "model": route.rate.model,
        "rate_key": route.rate.key,
        "billable": route.billable,
        "rate_verified": route.rate.verified,
        "price_note": route.rate.price_note,
        "estimate_each_usd": each,
        "estimate_total_usd": round(each * count, 6),
        "budget": [vars(s) | {"would_exceed": s.would_exceed, "remaining": s.remaining}
                   for s in ledger.check_scope(scope, round(each * count, 6))],
    }


def generate(*, prompt: str, client_id: int | None = None, persona_id: int | None = None,
             keep_face: bool = False, count: int = 1, label: str = "",
             override_by: str | None = None) -> list[int]:
    """Reserve and fire `count` stills. Returns the new generation ids.

    Each image is reserved separately, so a batch that crosses the cap halfway stops
    halfway instead of being waved through whole.
    """
    if not prompt.strip():
        raise ImageError("a prompt is required")
    count = max(1, min(int(count), MAX_BATCH))
    client_id, persona = _resolve_target(client_id, persona_id)

    stage = "still_identity" if keep_face else "still"
    route = router.resolve(stage, kind="image")
    scope = ledger.scope_for_client(client_id, persona["id"] if persona else None)

    reference = persona["reference_image_url"] if persona else None
    if route.rate.identity_via_image and not reference:
        raise ImageError(
            "this model seeds from the persona's locked reference still, but "
            f"{'that persona has none set' if persona else 'no persona was given'}. "
            "Generate a face with text-to-image first, then save it as the reference."
        )

    gen_ids = []
    for _ in range(count):
        req = GenerationRequest(
            model=route.charged.model if not route.billable else route.rate.model,
            kind="image",
            prompt=prompt,
            duration_s=0.0,
            supports=route.rate.supports,
            extra=dict(route.rate.stage_params(stage)),
        )
        if keep_face:
            req.reference_image_url = reference

        gen_id = ledger.reserve(
            stage="still", scope=scope, rate=route.charged, duration_s=0.0,
            payload=json.dumps({"model": req.model, "kind": "image",
                                "keeps_face": keep_face, "prompt": prompt[:2000]}),
            billable=route.billable, override_by=override_by,
        )
        gen_ids.append(gen_id)
        _pool().submit(_run_still, gen_id, client_id,
                       persona["id"] if persona else None,
                       route.provider.name, req, prompt, label)
    return gen_ids


def _run_still(gen_id: int, client_id: int, persona_id: int | None,
               provider_name: str, req: GenerationRequest, prompt: str,
               label: str) -> None:
    """Submit, poll, settle, and — on success only — file the result as an asset."""
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
            log.warning("still %s still running at shutdown; left as running", gen_id)
            return

        output_url = result.output_url
        if result.status == "succeeded" and output_url:
            client_name = db.query_one(
                "SELECT name FROM clients WHERE id=?", (client_id,))["name"]
            output_url = storage.archive_output(
                output_url, client_name=client_name, job_id=None, gen_id=gen_id,
                kind="image",
                subdir=f"persona-{persona_id}" if persona_id else "stills")

        report = ledger.settle(gen_id, status=result.status,
                               actual_cost_usd=result.cost_usd,
                               output_url=output_url, error=result.error)
        if report["drift_warning"]:
            log.warning("still %s cost drift %+.1f%% (est $%.4f, actual $%.4f)",
                        gen_id, report["drift_pct"], report["estimated"], report["actual"])

        # Only a succeeded generation becomes an asset. A failed one still cost money
        # and stays in the ledger, but it is not something to pick from a library.
        if result.status == "succeeded" and output_url:
            db.insert(
                """INSERT INTO assets (client_id, persona_id, generation_id, kind,
                                       source, url, prompt, label)
                   VALUES (?,?,?, 'image', 'generated', ?,?,?)""",
                (client_id, persona_id, gen_id, output_url, prompt, label),
            )
        else:
            log.error("still %s failed: %s", gen_id, result.error)

    except (ProviderError, NoProviderAvailable, UnverifiedRate) as exc:
        ledger.settle(gen_id, status="failed", actual_cost_usd=0.0,
                      error=f"{type(exc).__name__}: {exc}")
        log.error("still %s never fired: %s", gen_id, exc)
    except Exception as exc:                                # never lose a reservation
        ledger.settle(gen_id, status="failed", error=f"unexpected: {exc}")
        log.exception("still %s crashed", gen_id)


# ---------------------------------------------------------------- asset library

def save_upload(*, client_id: int, persona_id: int | None, url: str,
                label: str = "") -> int:
    """Record an externally-hosted image as an asset. No generation, no cost."""
    client_id, persona = _resolve_target(client_id, persona_id)
    return db.insert(
        """INSERT INTO assets (client_id, persona_id, kind, source, url, label)
           VALUES (?,?, 'image', 'uploaded', ?,?)""",
        (client_id, persona["id"] if persona else None, url, label),
    )


def set_primary(asset_id: int) -> dict:
    """Make this asset the persona's locked face.

    Writes personas.reference_image_url as well as the flag, because that column is
    what every generation actually reads. A 'primary' asset the renderer never sees
    would be a label that lies.
    """
    asset = db.query_one("SELECT * FROM assets WHERE id=?", (asset_id,))
    if asset is None:
        raise KeyError(f"asset {asset_id} not found")
    if asset["persona_id"] is None:
        raise ImageError("this asset is not attached to a persona, so it cannot be "
                         "that persona's locked face")
    db.execute("UPDATE assets SET is_primary=0 WHERE persona_id=?", (asset["persona_id"],))
    db.execute("UPDATE assets SET is_primary=1 WHERE id=?", (asset_id,))
    db.execute("UPDATE personas SET reference_image_url=? WHERE id=?",
               (asset["url"], asset["persona_id"]))
    return dict(db.query_one("SELECT * FROM personas WHERE id=?", (asset["persona_id"],)))


def delete_asset(asset_id: int) -> None:
    """Remove an asset from the library.

    The generation row stays: the money was spent whether or not you kept the picture,
    and the ledger records what happened, not what you wish had happened.
    """
    asset = db.query_one("SELECT * FROM assets WHERE id=?", (asset_id,))
    if asset is None:
        raise KeyError(f"asset {asset_id} not found")
    if asset["is_primary"]:
        raise ImageError("this is the persona's locked face. Set a different asset as "
                         "primary first, or every future render loses its reference.")
    db.execute("DELETE FROM assets WHERE id=?", (asset_id,))


def list_assets(*, persona_id: int | None = None, client_id: int | None = None) -> list[dict]:
    where, params = [], []
    if persona_id is not None:
        where.append("a.persona_id = ?"); params.append(persona_id)
    if client_id is not None:
        where.append("a.client_id = ?"); params.append(client_id)
    sql = """SELECT a.*, p.name AS persona_name, c.name AS client_name
               FROM assets a
               JOIN clients c ON c.id = a.client_id
               LEFT JOIN personas p ON p.id = a.persona_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [dict(r) for r in db.query(sql + " ORDER BY a.is_primary DESC, a.id DESC",
                                      tuple(params))]
