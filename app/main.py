"""FastAPI app + the internal dashboard."""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, images, jobs, ledger, scripts
from . import identity as identity_mod
from .api import api, system_status
from .config import settings
from .identity import IdentityBinding
from .rates import rate_table

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kalvid")

WEB = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=str(WEB / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    mode = "DRY RUN (mock provider, $0)" if settings.dry_run else "LIVE — BILLABLE"
    log.info("Kalvid AI starting — %s", mode)
    if not settings.dry_run:
        unverified = [r.key for r in rate_table.all()
                      if not r.verified and r.provider != "mock"]
        if unverified:
            log.warning("LIVE mode with unverified rates: %s", ", ".join(unverified))
    yield
    jobs.shutdown(wait=False)


app = FastAPI(title="Kalvid AI", version="0.1.0", lifespan=lifespan)
app.include_router(api)
(WEB / "static").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")


@app.get("/media/{path:path}")
def media(path: str):
    """Serve locally archived renders so drafts are viewable in the dashboard."""
    target = (settings.output_dir / path).resolve()
    if not str(target).startswith(str(settings.output_dir.resolve())):
        return HTMLResponse("forbidden", status_code=403)
    if not target.exists():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(target)


def media_url(stored: str | None) -> str | None:
    """A browser-usable URL for a generation's output.

    Archived renders are stored as absolute local paths, which a browser cannot open.
    They are re-pointed at /media/. Supabase signed URLs and provider URLs pass
    through untouched. Anything outside the output dir returns None rather than
    handing the page a path that would 403.
    """
    if not stored:
        return None
    if stored.startswith(("http://", "https://", "/media/")):
        return stored
    try:
        rel = Path(stored).resolve().relative_to(settings.output_dir.resolve())
    except (ValueError, OSError):
        return None
    return "/media/" + str(rel)


def _persona_rows(where: str = "", params: tuple = ()) -> list[dict]:
    """Personas with the one derived flag the UI needs: can this actually generate?"""
    rows = db.query(
        f"""SELECT p.*, c.name AS client_name FROM personas p
              JOIN clients c ON c.id = p.client_id
            {where} ORDER BY c.name, p.name""", params)
    out = []
    for r in rows:
        d = dict(r)
        binding = IdentityBinding(r["identity_strategy"], r["reference_image_url"],
                                  r["identity_lock_id"])
        d["identity_warnings"] = binding.validate()
        d["ready"] = not binding.blocking_problems
        out.append(d)
    return out


def _client_rows() -> list[dict]:
    out = []
    for c in db.query("SELECT * FROM clients ORDER BY name"):
        spent, pending = ledger.client_spend(c["id"])
        cap = c["monthly_budget_cap"]
        d = dict(c)
        d.update(spent=spent, pending=pending,
                 pct=round(100 * spent / cap, 1) if cap else 0.0,
                 pending_pct=round(100 * pending / cap, 1) if cap else 0.0,
                 remaining=round(cap - spent, 2))
        out.append(d)
    return out


def _asset_version() -> str:
    """Short hash of the static bundle, appended to its URL.

    A browser will re-use a cached app.js for the life of a tab unless the URL
    changes. During development that means a shipped fix can be invisible — the
    server is correct, the page is stale, and nothing says so.
    """
    h = hashlib.sha256()
    for name in ("app.css", "app.js"):
        f = WEB / "static" / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


def _ctx(request: Request, **kw) -> dict:
    return {"request": request, "status": system_status(), "settings": settings,
            "asset_v": _asset_version(), **kw}


# ---------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    clients = _client_rows()
    active = db.query(
        """SELECT j.*, p.name AS persona_name, c.name AS client_name,
                  (SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)),0)
                     FROM generations g WHERE g.job_id=j.id AND g.status!='cancelled') AS spend,
                  (SELECT g2.output_url FROM generations g2
                    WHERE g2.job_id=j.id AND g2.status='succeeded' AND g2.output_url IS NOT NULL
                    ORDER BY g2.id DESC LIMIT 1) AS thumb_raw
             FROM jobs j
             JOIN personas p ON p.id=j.persona_id
             JOIN clients  c ON c.id=p.client_id
            ORDER BY CASE j.status WHEN 'draft_ready' THEN 0 WHEN 'drafting' THEN 1
                                   WHEN 'rendering' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END,
                     j.id DESC
            LIMIT 50""")
    job_rows = []
    for j in active:
        d = dict(j)
        d["thumb"] = media_url(j["thumb_raw"])
        job_rows.append(d)

    totals = {
        "cap": round(sum(c["monthly_budget_cap"] for c in clients), 2),
        "spent": round(sum(c["spent"] for c in clients), 2),
        "pending": round(sum(c["pending"] for c in clients), 2),
        "draft_ready": sum(1 for j in job_rows if j["status"] == "draft_ready"),
    }
    totals["remaining"] = round(totals["cap"] - totals["spent"], 2)

    return templates.TemplateResponse(
        request, "dashboard.html",
        _ctx(request, clients=clients, jobs=job_rows, totals=totals,
             personas=_persona_rows()))


@app.get("/creator", response_class=HTMLResponse)
def creator(request: Request, brief: str = "", platform: str = "", duration: int = 0):
    open_jobs = db.query(
        """SELECT j.*, p.name AS persona_name, c.name AS client_name,
                  (SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)),0)
                     FROM generations g WHERE g.job_id=j.id AND g.status!='cancelled') AS spend
             FROM jobs j
             JOIN personas p ON p.id=j.persona_id
             JOIN clients  c ON c.id=p.client_id
            WHERE j.status NOT IN ('complete','rejected')
            ORDER BY j.id DESC LIMIT 8""")
    return templates.TemplateResponse(
        request, "creator.html",
        _ctx(request, personas=_persona_rows(), jobs=[dict(j) for j in open_jobs],
             script=scripts.preview(0),
             prefill={"brief": brief, "platform": platform, "duration": duration}))


@app.get("/studio", response_class=HTMLResponse)
def studio(request: Request, persona: int | None = None):
    personas = _persona_rows()
    selected = None
    if personas:
        selected = next((p for p in personas if p["id"] == persona), personas[0])
        selected = dict(selected)
        selected["jobs"] = [dict(j) for j in db.query(
            "SELECT id, brief, status, created_at FROM jobs WHERE persona_id=? "
            "ORDER BY id DESC", (selected["id"],))]
        selected["versions"] = identity_mod.versions(selected["id"])
        selected["current_version"] = identity_mod.current_version(selected["id"])
        # The two layers are shown separately on purpose: mixing a locked identity
        # plate into the same grid as a pair of sunglasses is exactly the leak the
        # platform spec warns about.
        variable = identity_mod.VARIABLE_PLATES
        selected["assets"] = images.list_assets(persona_id=selected["id"],
                                                exclude_plates=variable)
        selected["wardrobe"] = images.list_assets(persona_id=selected["id"],
                                                  plates=variable)
        for a in selected["assets"] + selected["wardrobe"]:
            a["url"] = media_url(a["url"]) or a["url"]
        draft = identity_mod.draft_version(selected["id"])
        selected["draft"] = draft
        selected["sheet"] = identity_mod.sheet(draft or selected["current_version"] or {})
        selected["sheet_fields"] = identity_mod.SHEET_FIELDS
        selected["sheet_labels"] = identity_mod.SHEET_LABELS
        selected["plate_labels"] = identity_mod.PLATE_LABELS
        selected["identity_plates"] = identity_mod.IDENTITY_PLATES
    return templates.TemplateResponse(
        request, "studio.html",
        _ctx(request, personas=personas, selected=selected, clients=_client_rows()))


@app.get("/library", response_class=HTMLResponse)
def library(request: Request):
    rows = db.query(
        """SELECT g.id, g.job_id, g.stage, g.model, g.output_url, g.created_at,
                  COALESCE(g.actual_cost_usd, g.estimated_cost_usd) AS cost,
                  j.brief, p.name AS persona_name, c.name AS client_name
             FROM generations g
             JOIN jobs j     ON j.id = g.job_id
             JOIN personas p ON p.id = j.persona_id
             JOIN clients  c ON c.id = p.client_id
            WHERE g.status = 'succeeded' AND g.output_url IS NOT NULL
            ORDER BY g.id DESC LIMIT 200""")
    media_items = []
    for r in rows:
        url = media_url(r["output_url"])
        if not url:            # a provider URL that has already expired off disk
            continue
        d = dict(r)
        d["media"] = url
        d["kind"] = "image" if url.lower().split("?")[0].endswith(
            (".png", ".jpg", ".jpeg", ".webp")) else "video"
        media_items.append(d)
    # Generated stills are recorded as assets, so they would otherwise be missing
    # from the one screen that claims to show everything.
    for a in images.list_assets():
        url = media_url(a["url"]) or a["url"]
        media_items.append({
            "id": f"a{a['id']}", "job_id": None, "stage": "still", "media": url,
            "kind": "image", "cost": 0.0, "model": "",
            "brief": a["prompt"] or a["label"] or "Untitled still",
            "persona_name": a["persona_name"] or "—",
            "client_name": a["client_name"], "created_at": a["created_at"],
            "is_primary": a["is_primary"],
        })
    media_items.sort(key=lambda m: str(m["created_at"]), reverse=True)
    return templates.TemplateResponse(request, "library.html",
                                      _ctx(request, media=media_items))


@app.get("/images", response_class=HTMLResponse)
def image_studio(request: Request, persona: int | None = None):
    assets = images.list_assets(persona_id=persona)
    for a in assets:
        a["url"] = media_url(a["url"]) or a["url"]
    pending = db.query_one(
        """SELECT COUNT(*) n FROM generations
            WHERE stage='still' AND status IN ('pending','running')""")["n"]
    return templates.TemplateResponse(
        request, "images.html",
        _ctx(request, personas=_persona_rows(), clients=_client_rows(),
             assets=assets[:24], pending=pending))


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    return templates.TemplateResponse(request, "clients.html",
                                      _ctx(request, clients=_client_rows()))


@app.get("/ads", response_class=HTMLResponse)
def ads(request: Request):
    return templates.TemplateResponse(request, "ads.html",
                                      _ctx(request, ad_templates=AD_TEMPLATES))


@app.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request):
    waiting = db.query(
        """SELECT j.id, j.brief, p.name AS persona_name, c.name AS client_name
             FROM jobs j
             JOIN personas p ON p.id=j.persona_id
             JOIN clients  c ON c.id=p.client_id
            WHERE j.status='draft_ready' ORDER BY j.id DESC""")
    failures = db.query(
        """SELECT g.job_id, g.stage, g.provider, g.error,
                  COALESCE(g.actual_cost_usd, g.estimated_cost_usd) AS effective
             FROM generations g WHERE g.status='failed' ORDER BY g.id DESC LIMIT 25""")
    events = db.query(
        """SELECT e.*, c.name AS client_name FROM budget_events e
             JOIN clients c ON c.id = e.client_id
            ORDER BY e.id DESC LIMIT 100""")
    return templates.TemplateResponse(
        request, "notifications.html",
        _ctx(request, waiting=[dict(w) for w in waiting],
             failures=[dict(f) for f in failures], events=[dict(e) for e in events]))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    rows = [
        ("Operator", settings.operator),
        ("Database", f"{settings.db_backend}"),
        ("Output directory", str(settings.output_dir)),
        ("Storage bucket", settings.supabase_bucket),
        ("Draft stage", f"{jobs.DRAFT_KIND} · {jobs.DRAFT_DURATION_S}s"),
        ("Rate staleness limit", f"{settings.rate_staleness_days} days"),
        ("Cost drift warning", f"{settings.cost_drift_warn_pct}%"),
        ("In-flight generations", system_status()["in_flight"]),
    ]
    return templates.TemplateResponse(request, "settings.html", _ctx(request, rows=rows))


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    from .api import get_job, preview_job
    job = get_job(job_id)
    if job.get("identity_version_id"):
        try:
            job["identity_version"] = identity_mod.get_version(job["identity_version_id"])
        except KeyError:
            job["identity_version"] = None
    for g in job["generations"]:
        g["media"] = media_url(g.get("output_url"))
    return templates.TemplateResponse(
        request, "job.html",
        _ctx(request, job=job, preview=preview_job(job_id),
             prompt_json=json.dumps(job["structured_prompt"], indent=2)))


@app.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(request: Request, client_id: int):
    from .api import client_ledger
    return templates.TemplateResponse(
        request, "client.html", _ctx(request, data=client_ledger(client_id)))


@app.get("/rates", response_class=HTMLResponse)
def rates_page(request: Request):
    from .api import list_rates
    return templates.TemplateResponse(request, "rates.html", _ctx(request, data=list_rates()))


# Starting shapes for a brief, not generated content. Each one is a structure that
# reliably works in UGC; the operator swaps in the product and the spoken line.
AD_TEMPLATES = [
    {"name": "Unboxing", "icon": "inventory_2", "tone": "primary",
     "platform": "tiktok", "duration": 8,
     "why": "The most forgiving shape — hands, product, one reaction line.",
     "brief": 'She unboxes the product on a bright counter, turns it to camera and says '
              '"okay this packaging is unreal"'},
    {"name": "Problem → solution", "icon": "swap_horiz", "tone": "secondary",
     "platform": "tiktok", "duration": 10,
     "why": "Names the pain in the first second, then shows the fix.",
     "brief": 'She looks frustrated at her reflection, then holds up the product and says '
              '"three days of this and I stopped covering it up"'},
    {"name": "Testimonial", "icon": "record_voice_over", "tone": "tertiary",
     "platform": "instagram", "duration": 10,
     "why": "Straight to camera, no props. Cheapest to get right.",
     "brief": 'She speaks directly to camera in soft window light and says '
              '"I did not expect to be the person recommending this, but here we are"'},
    {"name": "Get ready with me", "icon": "styler", "tone": "primary",
     "platform": "tiktok", "duration": 15,
     "why": "Native to the feed — the product appears mid-routine, not as an ad.",
     "brief": 'She applies the product as part of a morning routine at a vanity and says '
              '"this is the step everyone skips"'},
    {"name": "Before / after", "icon": "compare", "tone": "secondary",
     "platform": "shorts", "duration": 8,
     "why": "Visual proof carries it; keep the line short.",
     "brief": 'She holds the product up beside her face, smiles and says '
              '"same lighting, same camera, two weeks apart"'},
    {"name": "Flash sale", "icon": "bolt", "tone": "tertiary",
     "platform": "instagram", "duration": 5,
     "why": "Short and urgent. Five seconds is plenty.",
     "brief": 'She grabs the product off a shelf, walks toward camera and says '
              '"this never goes on sale — it is on sale"'},
]
