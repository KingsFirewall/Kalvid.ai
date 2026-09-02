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
import uuid

from . import db, ledger, storage
from . import identity as identity_mod
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
            reference_field=route.rate.reference_field,
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

def save_upload(*, client_id: int | None, persona_id: int | None, url: str,
                label: str = "", plate: str | None = None) -> int:
    """Record an externally-hosted image as an asset. No generation, no cost."""
    client_id, persona = _resolve_target(client_id, persona_id)
    _check_plate(plate, persona)
    return db.insert(
        """INSERT INTO assets (client_id, persona_id, kind, source, url, label, plate)
           VALUES (?,?, 'image', 'uploaded', ?,?,?)""",
        (client_id, persona["id"] if persona else None, url, label, plate),
    )


def _check_plate(plate: str | None, persona: dict | None) -> None:
    if plate is None:
        return
    if plate not in identity_mod.PLATES:
        raise ImageError(f"plate must be one of {', '.join(identity_mod.PLATES)}")
    if plate in identity_mod.IDENTITY_PLATES and persona is None:
        raise ImageError(
            f"a {plate!r} plate is part of an influencer's identity, so it has to "
            f"belong to one. Pick an influencer, or file it as a product asset.")


def upload_file(*, data: bytes, content_type: str, filename: str = "",
                client_id: int | None = None, persona_id: int | None = None,
                plate: str | None = None, label: str = "") -> dict:
    """Store an uploaded image and file it as an asset. Costs nothing.

    Identity plates go into the persona's OPEN DRAFT, never into a locked version —
    uploading a picture must not silently rewrite an identity that work has already
    been rendered against. Wardrobe and product assets are the variable layer and are
    not attached to a version at all.
    """
    from . import storage

    client_id, persona = _resolve_target(client_id, persona_id)
    _check_plate(plate, persona)

    client_name = db.query_one("SELECT name FROM clients WHERE id=?", (client_id,))["name"]
    key = f"upload-{uuid.uuid4().hex[:12]}"
    try:
        url = storage.store_upload(
            data, content_type=content_type, client_name=client_name,
            persona_id=persona["id"] if persona else None, asset_key=key)
    except storage.StorageError as exc:
        raise ImageError(str(exc))

    version_id = None
    if plate in identity_mod.IDENTITY_PLATES and persona is not None:
        draft = identity_mod.open_draft(persona["id"])
        version_id = draft["id"]

    asset_id = db.insert(
        """INSERT INTO assets (client_id, persona_id, identity_version_id, kind,
                               source, url, label, plate)
           VALUES (?,?,?, 'image', 'uploaded', ?,?,?)""",
        (client_id, persona["id"] if persona else None, version_id, url,
         label or filename, plate),
    )
    return dict(db.query_one("SELECT * FROM assets WHERE id=?", (asset_id,)))


def set_primary(asset_id: int, locked_by: str = "operator") -> dict:
    """Make this asset the persona's locked face — as a NEW identity version.

    This used to overwrite personas.reference_image_url in place, which quietly
    rewrote the provenance of every clip already delivered under the old face: there
    was no way, afterwards, to tell which reference a given video had actually used.
    Now it cuts v(n+1). Existing jobs stay pinned to the version they were briefed
    against and will re-draft as the same person.
    """
    asset = db.query_one("SELECT * FROM assets WHERE id=?", (asset_id,))
    if asset is None:
        raise KeyError(f"asset {asset_id} not found")
    if asset["persona_id"] is None:
        raise ImageError("this asset is not attached to a persona, so it cannot be "
                         "that persona's locked face")

    current = identity_mod.current_version(asset["persona_id"])
    if current and current["reference_image_url"] == asset["url"]:
        raise ImageError("this is already the locked face for the current version")

    version = identity_mod.edit(asset["persona_id"], locked_by,
                                reference_image_url=asset["url"])

    # is_primary marks the plate belonging to the CURRENT version, so the Studio can
    # show which one is live without joining every time.
    db.execute("UPDATE assets SET is_primary=0 WHERE persona_id=?", (asset["persona_id"],))
    db.execute("UPDATE assets SET is_primary=1, identity_version_id=?, plate='identity' "
               "WHERE id=?", (version["id"], asset_id))

    persona = dict(db.query_one("SELECT * FROM personas WHERE id=?", (asset["persona_id"],)))
    persona["identity_version"] = version["version"]
    return persona


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
    # Only the LOCKED layer is protected. Wardrobe and product plates are the variable
    # layer by design — a pair of glasses is swapped per scene, and refusing to delete
    # one would be treating a prop as part of the persona's identity.
    if asset["plate"] in identity_mod.IDENTITY_PLATES:
        backing = db.query_one(
            """SELECT version FROM identity_versions
                WHERE id=? AND status IN ('locked','superseded')""",
            (asset["identity_version_id"],))
        if backing:
            raise ImageError(
                f"this is the {asset['plate']} plate of locked identity version "
                f"v{backing['version']}, so deleting it would break the provenance of "
                f"work already rendered against it.")
    db.execute("DELETE FROM assets WHERE id=?", (asset_id,))


def list_assets(*, persona_id: int | None = None, client_id: int | None = None,
                plates: tuple[str, ...] | None = None,
                exclude_plates: tuple[str, ...] | None = None) -> list[dict]:
    where, params = [], []
    if persona_id is not None:
        where.append("a.persona_id = ?")
        params.append(persona_id)
    if client_id is not None:
        where.append("a.client_id = ?")
        params.append(client_id)
    if plates:
        where.append("a.plate IN ({})".format(",".join("?" * len(plates))))
        params.extend(plates)
    if exclude_plates:
        where.append("(a.plate IS NULL OR a.plate NOT IN ({}))".format(
            ",".join("?" * len(exclude_plates))))
        params.extend(exclude_plates)
    sql = """SELECT a.*, p.name AS persona_name, c.name AS client_name
               FROM assets a
               JOIN clients c ON c.id = a.client_id
               LEFT JOIN personas p ON p.id = a.persona_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [dict(r) for r in db.query(sql + " ORDER BY a.is_primary DESC, a.id DESC",
                                      tuple(params))]
