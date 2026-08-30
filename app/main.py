"""FastAPI app + the internal dashboard."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, jobs, ledger
from .api import api, system_status
from .config import settings
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


def _ctx(request: Request, **kw) -> dict:
    return {"request": request, "status": system_status(), "settings": settings, **kw}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    clients = []
    for c in db.query("SELECT * FROM clients ORDER BY name"):
        spent, pending = ledger.client_spend(c["id"])
        d = dict(c)
        d.update(spent=spent, pending=pending,
                 pct=round(100 * spent / c["monthly_budget_cap"], 1)
                     if c["monthly_budget_cap"] else 0.0,
                 remaining=round(c["monthly_budget_cap"] - spent, 2))
        clients.append(d)

    active = db.query(
        """SELECT j.*, p.name AS persona_name, c.name AS client_name,
                  (SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)),0)
                     FROM generations g WHERE g.job_id=j.id AND g.status!='cancelled') AS spend
             FROM jobs j
             JOIN personas p ON p.id=j.persona_id
             JOIN clients  c ON c.id=p.client_id
            ORDER BY CASE j.status WHEN 'draft_ready' THEN 0 WHEN 'drafting' THEN 1
                                   WHEN 'rendering' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END,
                     j.id DESC
            LIMIT 50""")
    return templates.TemplateResponse(
        request, "dashboard.html",
        _ctx(request, clients=clients, jobs=[dict(j) for j in active],
             personas=[dict(p) for p in db.query(
                 """SELECT p.*, c.name AS client_name FROM personas p
                      JOIN clients c ON c.id=p.client_id ORDER BY c.name, p.name""")]))


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    from .api import get_job, preview_job
    job = get_job(job_id)
    return templates.TemplateResponse(
        request, "job.html", _ctx(request, job=job, preview=preview_job(job_id),
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
