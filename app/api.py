"""Internal HTTP API. Internal-only for v1: no auth, bind to localhost.

Contract follows the PRD, with per-stage additions where the build revealed a gap.
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db, images, jobs, ledger, scripts
from .config import settings
from . import identity as identity_mod
from .identity import STRATEGIES, IdentityBinding
from .prompts import structure
from .rates import UnverifiedRate, rate_table
from .router import NoProviderAvailable, router as provider_router

api = APIRouter(prefix="/api")


# ---------------------------------------------------------------- payloads

class ClientIn(BaseModel):
    name: str
    contact_info: str = ""
    monthly_budget_cap: float = Field(0, ge=0)
    default_job_cap: float = Field(0, ge=0)


class PersonaIn(BaseModel):
    client_id: int
    name: str
    identity_strategy: str = "reference_image"
    reference_image_url: str | None = None
    identity_lock_id: str | None = None
    voice_profile: str = ""
    notes: str = ""


class JobIn(BaseModel):
    persona_id: int
    brief: str
    platform: str = "tiktok"
    target_duration: int = Field(8, ge=1, le=120)
    job_budget_cap: float = Field(0, ge=0)


class ImageIn(BaseModel):
    prompt: str
    client_id: int | None = None
    persona_id: int | None = None
    # False = text-to-image (a NEW face). True = seeded from the persona's locked
    # reference, which is the only way to get the SAME face back.
    keep_face: bool = False
    count: int = Field(1, ge=1, le=images.MAX_BATCH)
    label: str = ""
    override_by: str | None = None


class AssetIn(BaseModel):
    client_id: int | None = None
    persona_id: int | None = None
    url: str
    label: str = ""
    plate: str | None = None


class ActionIn(BaseModel):
    # Set only to deliberately spend past a cap. Recorded in budget_events.
    override_by: str | None = None
    reason: str = ""


def _row(r) -> dict:
    return dict(r) if r is not None else None


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


# ---------------------------------------------------------------- clients

@api.post("/clients", status_code=201)
def create_client(payload: ClientIn):
    try:
        cid = db.insert(
            """INSERT INTO clients (name, contact_info, monthly_budget_cap, default_job_cap)
               VALUES (?,?,?,?)""",
            (payload.name, payload.contact_info, payload.monthly_budget_cap,
             payload.default_job_cap),
        )
    except Exception as exc:
        raise HTTPException(409, f"could not create client: {exc}")
    return _row(db.query_one("SELECT * FROM clients WHERE id=?", (cid,)))


@api.get("/clients")
def list_clients():
    out = []
    for c in db.query("SELECT * FROM clients ORDER BY name"):
        spent, pending = ledger.client_spend(c["id"])
        d = dict(c)
        d.update(spent_this_month=spent, pending=pending,
                 remaining=round(c["monthly_budget_cap"] - spent, 4))
        out.append(d)
    return out


@api.get("/clients/{client_id}/ledger")
def client_ledger(client_id: int):
    client = db.query_one("SELECT * FROM clients WHERE id=?", (client_id,))
    if client is None:
        raise HTTPException(404, "client not found")
    spent, pending = ledger.client_spend(client_id)
    start, end = ledger.month_bounds()
    gens = db.query(
        """SELECT g.*, j.id AS job, p.name AS persona,
                  COALESCE(g.actual_cost_usd, g.estimated_cost_usd) AS effective_cost
             FROM generations g
             JOIN jobs j ON j.id = g.job_id
             JOIN personas p ON p.id = j.persona_id
            WHERE p.client_id = ? AND g.created_at >= ? AND g.created_at < ?
            ORDER BY g.id DESC""",
        (client_id, start, end),
    )
    return {
        "client": _row(client),
        "period": {"start": start, "end": end},
        "spent_this_month": spent,
        "pending": pending,
        "cap": client["monthly_budget_cap"],
        "remaining": round(client["monthly_budget_cap"] - spent, 4),
        "generations": _rows(gens),
        "budget_events": _rows(db.query(
            "SELECT * FROM budget_events WHERE client_id=? ORDER BY id DESC LIMIT 100",
            (client_id,))),
    }


# ---------------------------------------------------------------- personas

@api.post("/personas", status_code=201)
def create_persona(payload: PersonaIn):
    if payload.identity_strategy not in STRATEGIES:
        raise HTTPException(422, f"identity_strategy must be one of {STRATEGIES}")
    binding = IdentityBinding(payload.identity_strategy, payload.reference_image_url,
                              payload.identity_lock_id)
    blocking = binding.blocking_problems
    if blocking:
        raise HTTPException(422, "; ".join(blocking))
    try:
        pid = db.insert(
            """INSERT INTO personas (client_id, name, identity_strategy,
                   reference_image_url, identity_lock_id, voice_profile, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (payload.client_id, payload.name, payload.identity_strategy,
             payload.reference_image_url, payload.identity_lock_id,
             payload.voice_profile, payload.notes),
        )
    except Exception as exc:
        raise HTTPException(409, f"could not create persona: {exc}")
    return _row(db.query_one("SELECT * FROM personas WHERE id=?", (pid,)))


@api.get("/personas")
def list_personas(client_id: int | None = None):
    if client_id:
        return _rows(db.query(
            """SELECT p.*, c.name AS client_name FROM personas p
                 JOIN clients c ON c.id = p.client_id
                WHERE p.client_id=? ORDER BY p.name""", (client_id,)))
    return _rows(db.query(
        """SELECT p.*, c.name AS client_name FROM personas p
             JOIN clients c ON c.id = p.client_id ORDER BY c.name, p.name"""))


@api.get("/personas/{persona_id}")
def get_persona(persona_id: int):
    p = db.query_one("SELECT * FROM personas WHERE id=?", (persona_id,))
    if p is None:
        raise HTTPException(404, "persona not found")
    d = dict(p)
    d["identity_warnings"] = IdentityBinding(
        p["identity_strategy"], p["reference_image_url"], p["identity_lock_id"]).validate()
    d["jobs"] = _rows(db.query(
        "SELECT id, brief, status, created_at FROM jobs WHERE persona_id=? ORDER BY id DESC",
        (persona_id,)))
    return d


class IdentityEditIn(BaseModel):
    reference_image_url: str | None = None
    identity_lock_id: str | None = None
    identity_strategy: str | None = None
    character_sheet: str | None = None
    voice_profile: str | None = None
    notes: str | None = None
    locked_by: str = "operator"


@api.get("/personas/{persona_id}/versions")
def persona_versions(persona_id: int):
    """Full identity history. Locked versions are immutable; edits create the next."""
    if db.query_one("SELECT 1 FROM personas WHERE id=?", (persona_id,)) is None:
        raise HTTPException(404, "persona not found")
    return {
        "persona_id": persona_id,
        "current": identity_mod.current_version(persona_id),
        "draft": identity_mod.draft_version(persona_id),
        "versions": identity_mod.versions(persona_id),
    }


@api.post("/personas/{persona_id}/versions", status_code=201)
def edit_identity(persona_id: int, payload: IdentityEditIn):
    """Cut a new identity version. The previous one is superseded, never overwritten."""
    changes = {k: v for k, v in payload.model_dump().items()
               if k != "locked_by" and v is not None}
    if not changes:
        raise HTTPException(422, "no changes given")
    if "identity_strategy" in changes and changes["identity_strategy"] not in STRATEGIES:
        raise HTTPException(422, f"identity_strategy must be one of {STRATEGIES}")
    try:
        return identity_mod.edit(persona_id, payload.locked_by, **changes)
    except identity_mod.IdentityLocked as exc:
        raise HTTPException(422, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


# ---------------------------------------------------------------- jobs

@api.post("/jobs", status_code=201)
def create_job(payload: JobIn):
    try:
        jid = jobs.create_job(
            persona_id=payload.persona_id, brief=payload.brief,
            platform=payload.platform, target_duration=payload.target_duration,
            job_budget_cap=payload.job_budget_cap,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return get_job(jid)


@api.get("/jobs")
def list_jobs(status: str | None = None):
    sql = """SELECT j.*, p.name AS persona_name, c.name AS client_name,
                    (SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)),0)
                       FROM generations g
                      WHERE g.job_id=j.id AND g.status != 'cancelled') AS spend
               FROM jobs j
               JOIN personas p ON p.id = j.persona_id
               JOIN clients  c ON c.id = p.client_id"""
    params = ()
    if status:
        sql += " WHERE j.status = ?"
        params = (status,)
    return _rows(db.query(sql + " ORDER BY j.id DESC", params))


@api.get("/jobs/{job_id}")
def get_job(job_id: int):
    j = db.query_one(
        """SELECT j.*, p.name AS persona_name, p.reference_image_url,
                  p.identity_strategy, c.name AS client_name, c.id AS client_id
             FROM jobs j
             JOIN personas p ON p.id = j.persona_id
             JOIN clients  c ON c.id = p.client_id
            WHERE j.id = ?""", (job_id,))
    if j is None:
        raise HTTPException(404, "job not found")
    d = dict(j)
    d["structured_prompt"] = json.loads(j["structured_prompt"] or "{}")
    d["generations"] = _rows(db.query(
        "SELECT * FROM generations WHERE job_id=? ORDER BY id", (job_id,)))
    d["cost"] = job_cost(job_id)
    return d


@api.get("/jobs/{job_id}/cost")
def job_cost(job_id: int):
    rows = db.query(
        """SELECT stage, status, provider, model, estimated_cost_usd, actual_cost_usd,
                  COALESCE(actual_cost_usd, estimated_cost_usd) AS effective
             FROM generations WHERE job_id=? AND status != 'cancelled' ORDER BY id""",
        (job_id,))
    by_stage = {}
    for r in rows:
        by_stage[r["stage"]] = round(by_stage.get(r["stage"], 0) + r["effective"], 6)
    return {
        "job_id": job_id,
        "total_usd": round(sum(by_stage.values()), 6),
        "by_stage": by_stage,
        "generations": _rows(rows),
    }


@api.post("/jobs/{job_id}/preview")
def preview_job(job_id: int):
    """What the next draft would cost, and against which cap — before spending."""
    j = db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if j is None:
        raise HTTPException(404, "job not found")
    out = {}
    for stage in ("draft", "final"):
        try:
            route = provider_router.resolve(
                stage, kind=jobs.DRAFT_KIND if stage == "draft" else "video")
            duration = (min(jobs.DRAFT_DURATION_S, j["target_duration"])
                        if stage == "draft" and jobs.DRAFT_KIND == "video"
                        else (0.0 if stage == "draft" else j["target_duration"]))
            variant = route.rate.variant_for(stage)
            est = route.rate.estimate(duration_s=duration, variant=variant)   # true cost
            charged = route.charged.estimate(                                # $0 dry-run
                duration_s=duration, variant=route.charged.variant_for(stage))
            out[stage] = {
                "provider": route.provider.name, "model": route.rate.model,
                "rate_key": route.rate.key,
                "estimate_usd": est,
                "charged_usd": charged,
                "billable": route.billable,
                "rate_verified": route.rate.verified,
                "variant": variant,
                "price_expires": (route.rate.price_expires.isoformat()
                                  if route.rate.price_expires else None),
                "price_note": route.rate.price_note,
                # Budget is checked against the true cost, so dry-run rehearses the
                # real cap behaviour instead of sailing under a fake $0.
                "budget": [vars(s) | {"would_exceed": s.would_exceed,
                                      "remaining": s.remaining}
                           for s in ledger.check(job_id, est)],
            }
        except (NoProviderAvailable, UnverifiedRate, KeyError) as exc:
            out[stage] = {"error": str(exc)}

    # The gate only pays for itself if the draft is genuinely cheap. Fixed-duration
    # models can make a "short" draft cost nearly as much as the final render, at
    # which point drafting is close to paying twice. Say so plainly.
    d, f = out.get("draft", {}), out.get("final", {})
    if "error" not in d and "error" not in f and f.get("estimate_usd"):
        ratio = d["estimate_usd"] / f["estimate_usd"]
        out["gate"] = {
            "draft_pct_of_final": round(100 * ratio, 1),
            "effective": ratio <= 0.5,
            "note": (
                f"Draft is {round(100 * ratio)}% of the final's cost — the draft-first "
                f"gate is barely saving anything. Route drafts to a cheaper model "
                f"(see rates.json 'routing.draft')."
            ) if ratio > 0.5 else
            f"Draft costs {round(100 * ratio)}% of a final render.",
        }
    return out


def _act(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except jobs.TransitionError as exc:
        raise HTTPException(409, str(exc))
    except jobs.IdentityError as exc:
        raise HTTPException(422, str(exc))
    except ledger.BudgetExceeded as exc:
        raise HTTPException(402, str(exc))          # 402 Payment Required — literally
    except UnverifiedRate as exc:
        raise HTTPException(412, str(exc))
    except NoProviderAvailable as exc:
        raise HTTPException(503, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@api.post("/jobs/{job_id}/draft")
def start_draft(job_id: int, payload: ActionIn = Body(default=ActionIn())):
    gen_id = _act(jobs.start_draft, job_id, override_by=payload.override_by)
    return {"generation_id": gen_id, "job": get_job(job_id)}


@api.post("/jobs/{job_id}/approve")
def approve(job_id: int, payload: ActionIn = Body(default=ActionIn())):
    """The only route to a full-price render."""
    gen_id = _act(jobs.approve, job_id, override_by=payload.override_by)
    return {"generation_id": gen_id, "job": get_job(job_id)}


@api.post("/jobs/{job_id}/reject")
def reject(job_id: int, payload: ActionIn = Body(default=ActionIn())):
    _act(jobs.reject, job_id, payload.reason)
    return get_job(job_id)


# ---------------------------------------------------------------- scripts

class ScriptIn(BaseModel):
    persona_id: int
    scene: str
    platform: str = "tiktok"
    duration_s: int = Field(8, ge=1, le=120)
    product: str = ""
    tone: str = ""
    override_by: str | None = None


@api.get("/scripts/preview")
def preview_script(persona_id: int | None = None):
    """Whether script writing is available, and what one costs."""
    return scripts.preview(persona_id or 0)


@api.post("/scripts", status_code=201)
def write_script(payload: ScriptIn):
    """Claude writes the dialogue; the visual prompt stays deterministic."""
    try:
        script = scripts.generate(
            persona_id=payload.persona_id, scene=payload.scene,
            platform=payload.platform, duration_s=payload.duration_s,
            product=payload.product, tone=payload.tone,
            override_by=payload.override_by)
    except scripts.ScriptError as exc:
        raise HTTPException(422, str(exc))
    except ledger.BudgetExceeded as exc:
        raise HTTPException(402, str(exc))
    except UnverifiedRate as exc:
        raise HTTPException(412, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    # The brief is what the job pipeline actually consumes, so hand it back ready to use.
    script["brief"] = scripts.to_brief(script)
    return script


# ---------------------------------------------------------------- stills & assets

@api.post("/images/preview")
def preview_image(payload: ImageIn):
    """Cost and model for a still, before anything is spent."""
    try:
        return images.preview(client_id=payload.client_id, persona_id=payload.persona_id,
                              keep_face=payload.keep_face, count=payload.count)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except images.ImageError as exc:
        raise HTTPException(422, str(exc))


@api.post("/images", status_code=201)
def create_image(payload: ImageIn):
    """Generate one or more stills. Cheap, but it spends — same cap, same ledger."""
    try:
        gen_ids = images.generate(
            prompt=payload.prompt, client_id=payload.client_id,
            persona_id=payload.persona_id, keep_face=payload.keep_face,
            count=payload.count, label=payload.label, override_by=payload.override_by)
    except images.ImageError as exc:
        raise HTTPException(422, str(exc))
    except ledger.BudgetExceeded as exc:
        raise HTTPException(402, str(exc))
    except UnverifiedRate as exc:
        raise HTTPException(412, str(exc))
    except NoProviderAvailable as exc:
        raise HTTPException(503, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return {"generation_ids": gen_ids, "count": len(gen_ids)}


@api.get("/assets")
def get_assets(persona_id: int | None = None, client_id: int | None = None):
    return images.list_assets(persona_id=persona_id, client_id=client_id)


@api.post("/assets/upload", status_code=201)
async def upload_asset(
    file: UploadFile = File(...),
    persona_id: int | None = Form(None),
    client_id: int | None = Form(None),
    plate: str | None = Form(None),
    label: str = Form(""),
):
    """Upload an image and file it against an influencer.

    Identity plates land in the persona's OPEN DRAFT — uploading a picture must never
    silently rewrite a locked identity that work has already been rendered against.
    Wardrobe and product assets are the variable layer and are attached to no version.
    """
    data = await file.read()
    try:
        return images.upload_file(
            data=data, content_type=file.content_type or "",
            filename=file.filename or "", client_id=client_id,
            persona_id=persona_id, plate=(plate or None), label=label)
    except images.ImageError as exc:
        raise HTTPException(422, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@api.get("/personas/{persona_id}/sheet")
def get_sheet(persona_id: int):
    """The character sheet on the current locked version, plus any open draft."""
    current = identity_mod.current_version(persona_id)
    draft = identity_mod.draft_version(persona_id)
    return {
        "persona_id": persona_id,
        "fields": identity_mod.SHEET_FIELDS,
        "locked": identity_mod.sheet(current) if current else {},
        "locked_version": current["version"] if current else None,
        "draft": identity_mod.sheet(draft) if draft else None,
        "draft_version": draft["version"] if draft else None,
    }


@api.post("/personas/{persona_id}/sheet")
def edit_sheet(persona_id: int, payload: dict = Body(...)):
    """Write the character sheet into the OPEN DRAFT. Does not touch a locked version.

    Editing is deliberately two steps — draft then lock — so a typo does not cut a
    new identity version, but a real change still has to be committed explicitly.
    """
    unknown = set(payload) - set(identity_mod.SHEET_FIELDS)
    if unknown:
        raise HTTPException(422, f"unknown character-sheet field(s): {', '.join(sorted(unknown))}")
    try:
        draft = identity_mod.open_draft(
            persona_id, character_sheet=json.dumps(payload, indent=2))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return {"draft_version": draft["version"], "sheet": identity_mod.sheet(draft)}


@api.post("/personas/{persona_id}/lock")
def lock_identity(persona_id: int, payload: ActionIn = Body(default=ActionIn())):
    """Freeze the open draft as the next identity version."""
    try:
        return identity_mod.lock(persona_id, payload.override_by or settings.operator)
    except identity_mod.IdentityLocked as exc:
        raise HTTPException(422, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@api.post("/assets", status_code=201)
def add_asset(payload: AssetIn):
    """Register an image we did not generate. Costs nothing."""
    try:
        aid = images.save_upload(client_id=payload.client_id,
                                 persona_id=payload.persona_id,
                                 url=payload.url, label=payload.label,
                                 plate=payload.plate)
    except images.ImageError as exc:
        raise HTTPException(422, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return _row(db.query_one("SELECT * FROM assets WHERE id=?", (aid,)))


@api.post("/assets/{asset_id}/primary")
def make_primary(asset_id: int):
    """Promote an asset to the persona's locked face."""
    try:
        return images.set_primary(asset_id)
    except images.ImageError as exc:
        raise HTTPException(422, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@api.delete("/assets/{asset_id}", status_code=204)
def remove_asset(asset_id: int):
    try:
        images.delete_asset(asset_id)
    except images.ImageError as exc:
        raise HTTPException(409, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


# ---------------------------------------------------------------- system

@api.get("/rates")
def list_rates():
    today = date.today()
    return {
        "path": str(rate_table.path),
        "staleness_days": settings.rate_staleness_days,
        "rates": [
            {"key": r.key, "provider": r.provider, "model": r.model, "kind": r.kind,
             "unit": r.unit, "usd": r.usd,
             "last_verified": r.last_verified.isoformat() if r.last_verified else None,
             "verified": r.verified, "age_days": r.age_days(today),
             "stale": r.is_stale(today), "source": r.source}
            for r in rate_table.all()
        ],
    }


@api.post("/rates/reload")
def reload_rates():
    rate_table.reload()
    return list_rates()


@api.get("/status")
def system_status():
    """One call that answers 'is this safe to spend with right now?'"""
    stale = rate_table.stale()
    return {
        "dry_run": settings.dry_run,
        "billable": not settings.dry_run,
        "operator": settings.operator,
        "providers": {
            "fal": settings.provider_configured("fal"),
            "runware": settings.provider_configured("runware"),
        },
        "supabase_storage": settings.supabase_configured,
        "unverified_rates": [r.key for r in rate_table.all()
                             if not r.verified and r.provider != "mock"],
        "stale_rates": [r.key for r in stale],
        "in_flight": db.query_one(
            "SELECT COUNT(*) n FROM generations WHERE status IN ('pending','running')")["n"],
        "warnings": _status_warnings(stale),
    }


def _status_warnings(stale) -> list[str]:
    w = []
    if settings.dry_run:
        w.append("DRY RUN: all generations use the mock provider. Nothing is billable.")
    else:
        w.append("LIVE: generations will be charged to your provider accounts.")
        if stale:
            w.append(f"{len(stale)} rate(s) unverified or stale — budget estimates may "
                     f"be wrong. Update rates.json.")
    if not settings.supabase_configured:
        w.append("Supabase not configured: renders are kept on local disk only, and "
                 "provider URLs expire.")
    return w
