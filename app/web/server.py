"""Interface web locale : lancer une recherche, suivre sa progression, exporter.

Le serveur n'est pas prevu pour etre expose : il ecoute sur la boucle locale et
n'a aucune authentification. Les recherches tournent en tache de fond et
diffusent leur avancement via Server-Sent Events.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import campaigns as engine
from .. import compose, db, gmail
from ..config import BASE_URL, DAILY_CAP, DRY_RUN, MONTHLY_CAP, PUBLIC_URL, ROOT
from ..models import SearchQuery
from ..naf import catalogue, codes_for
from ..pipeline import run_search

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Candidature Radar", docs_url="/api/docs")


class Job:
    """Une recherche en cours et le canal qui diffuse son avancement."""

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.done = False
        self.error: str | None = None
        self.processed = 0
        self.total = 0


JOBS: dict[int, Job] = {}


class SearchPayload(BaseModel):
    job_title: str = Field(min_length=2, description="Poste vise, sert au scoring")
    sectors: list[str] = Field(default_factory=list)
    naf_codes: list[str] = Field(default_factory=list)
    keywords: str = ""
    department: str | None = None
    postal_code: str | None = None
    min_headcount: int | None = None
    max_headcount: int | None = None
    limit: int = Field(default=30, ge=1, le=200)
    use_search_engine: bool = True
    verify_smtp: bool = False


BACKGROUND: list[asyncio.Task] = []


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    # Envoi et détection des réponses tournent tant que le serveur est ouvert.
    BACKGROUND.append(asyncio.create_task(engine.sender_loop()))
    BACKGROUND.append(asyncio.create_task(engine.reply_loop()))


@app.on_event("shutdown")
async def _shutdown() -> None:
    for task in BACKGROUND:
        task.cancel()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/sectors")
async def sectors() -> list[dict]:
    return catalogue()


@app.get("/api/runs")
async def runs() -> list[dict]:
    return db.list_runs()


@app.post("/api/search")
async def start_search(payload: SearchPayload) -> dict:
    naf = codes_for(payload.sectors) + [c for c in payload.naf_codes if c]
    if not naf and not payload.keywords.strip():
        raise HTTPException(
            status_code=400,
            detail="Choisissez au moins un secteur, ou saisissez un mot-cle "
                   "present dans le nom des entreprises visees.",
        )

    query = SearchQuery(
        job_title=payload.job_title.strip(),
        keywords=payload.keywords.strip(),
        department=(payload.department or "").strip() or None,
        postal_code=(payload.postal_code or "").strip() or None,
        naf_codes=list(dict.fromkeys(naf)),
        min_headcount=payload.min_headcount,
        max_headcount=payload.max_headcount,
        limit=payload.limit,
    )

    run_id = db.start_run(
        job_title=query.job_title,
        sectors=",".join(payload.sectors),
        department=query.department,
        params=json.dumps(payload.model_dump(), ensure_ascii=False),
    )
    job = Job(run_id)
    JOBS[run_id] = job

    asyncio.create_task(_execute(job, query, payload))
    return {"run_id": run_id}


async def _execute(job: Job, query: SearchQuery, payload: SearchPayload) -> None:
    async def progress(event: dict) -> None:
        if event.get("event") == "etape" and event.get("total"):
            job.total = int(event["total"])
        if event.get("event") == "entreprise" and "contacts" in event:
            job.processed += 1
        event["processed"] = job.processed
        event["total"] = job.total
        await job.queue.put(event)

    try:
        companies, contacts = await run_search(
            query,
            use_search=payload.use_search_engine,
            verify_smtp=payload.verify_smtp,
            callback=progress,
        )
        for company in companies:
            db.save_company(company)

        # Les adresses deja suivies restent dans les resultats : l'interface les
        # annote avec leur statut plutot que de les faire disparaitre.
        db.save_contacts(job.run_id, contacts)
        db.finish_run(job.run_id, len(companies), len(contacts))
        await job.queue.put({"event": "termine", "total_contacts": len(contacts),
                             "processed": job.processed, "total": job.total})
    except Exception as exc:
        log.exception("recherche %s en echec", job.run_id)
        job.error = str(exc)
        db.finish_run(job.run_id, 0, 0, status="echec")
        await job.queue.put({"event": "erreur_fatale", "detail": str(exc)[:300]})
    finally:
        job.done = True
        await job.queue.put(None)


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: int) -> StreamingResponse:
    job = JOBS.get(run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Recherche inconnue")

    async def events():
        while True:
            try:
                item = await asyncio.wait_for(job.queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # garde la connexion ouverte
                continue
            if item is None:
                yield f"data: {json.dumps({'event': 'fin_flux'})}\n\n"
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: int) -> dict:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Recherche inconnue")
    return run


@app.get("/api/runs/{run_id}/contacts")
async def contacts(run_id: int) -> list[dict]:
    return db.run_contacts(run_id)


@app.get("/api/runs/{run_id}/export")
async def export(run_id: int) -> FileResponse:
    rows = db.run_contacts(run_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Aucun contact a exporter")
    path = db.export_csv(run_id)
    return FileResponse(path, filename=path.name, media_type="text/csv")


# ---------------------------------------------------------------- suivi ---

class OutreachCreate(BaseModel):
    email: str
    status: str = "a_contacter"
    note: str = ""
    company_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    role_title: str | None = None
    category: str | None = None
    score: float | None = None
    run_id: int | None = None


class OutreachPatch(BaseModel):
    status: str | None = None
    note: str | None = None


def _check_status(value: str | None) -> None:
    if value is not None and value not in db.OUTREACH_STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"Statut inconnu : {value}. Attendu : "
                                   + ", ".join(db.OUTREACH_STATUSES))


@app.get("/api/outreach")
async def outreach_list(status: str | None = None) -> list[dict]:
    _check_status(status)
    return db.list_outreach(status)


@app.post("/api/outreach")
async def outreach_add(payload: OutreachCreate) -> dict:
    _check_status(payload.status)
    data = payload.model_dump()
    email = data.pop("email").strip().lower()
    status = data.pop("status")
    note = data.pop("note")
    db.upsert_outreach(email, status, note, **data)
    return {"ok": True, "email": email, "status": status}


@app.patch("/api/outreach/{email}")
async def outreach_patch(email: str, payload: OutreachPatch) -> dict:
    _check_status(payload.status)
    if not db.update_outreach(email.strip().lower(), payload.status, payload.note):
        raise HTTPException(status_code=404, detail="Contact absent du suivi")
    return {"ok": True}


@app.delete("/api/outreach/{email}", status_code=204, response_class=Response)
async def outreach_delete(email: str) -> Response:
    db.delete_outreach(email.strip().lower())
    return Response(status_code=204)


@app.get("/api/outreach/export")
async def outreach_export() -> FileResponse:
    if not db.list_outreach():
        raise HTTPException(status_code=404, detail="Le suivi est vide")
    path = db.export_outreach_csv()
    return FileResponse(path, filename=path.name, media_type="text/csv")


# -------------------------------------------------------------- réglages ---

SETTING_KEYS = ("sender_name", "signature", "profile_summary", "dry_run")
CV_DIR = ROOT / "data" / "cv"


@app.get("/api/settings")
async def settings_get() -> dict:
    values = db.all_settings()
    return {
        "settings": {k: values.get(k, "") for k in (*SETTING_KEYS, "cv_path")},
        "cv_name": Path(values["cv_path"]).name if values.get("cv_path") else None,
        "gmail": {"configured": gmail.configured(), "connected": gmail.is_connected(),
                  "needs_reconnect": gmail.needs_reconnect(), "email": gmail.connected_email(),
                  "redirect_uri": BASE_URL + gmail.REDIRECT_PATH},
        "claude": compose.claude_available(),
        "pixel": bool(PUBLIC_URL),
        "dry_run": engine.dry_run(),
        "dry_run_forced": DRY_RUN,
        "caps": {"monthly": MONTHLY_CAP, "daily": DAILY_CAP},
        "variables": compose.VARIABLES,
        "defaults": {"subject": compose.DEFAULT_SUBJECT, "body": compose.DEFAULT_BODY},
    }


@app.put("/api/settings")
async def settings_put(payload: dict) -> dict:
    for key in SETTING_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "dry_run":
            value = "1" if value in (True, 1, "1", "true", "on") else "0"
        db.set_setting(key, str(value or "").strip())
    return {"ok": True}


@app.post("/api/settings/cv")
async def settings_cv(file: UploadFile = File(...)) -> dict:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Le CV doit être un PDF")
    CV_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in Path(file.filename).name if c.isalnum() or c in "._- ") or "cv.pdf"
    target = CV_DIR / safe
    target.write_bytes(await file.read())
    db.set_setting("cv_path", str(target))
    return {"ok": True, "cv_name": safe}


# ----------------------------------------------------------------- Gmail ---

@app.get("/api/gmail/connect")
async def gmail_connect() -> RedirectResponse:
    try:
        return RedirectResponse(gmail.auth_url())
    except gmail.GmailError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/gmail/callback")
async def gmail_callback(code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    if error or not code:
        return RedirectResponse(f"{BASE_URL}/#/reglages?gmail=refus")
    try:
        await gmail.exchange_code(code, state)
    except gmail.GmailError as exc:
        log.warning("connexion Gmail échouée : %s", exc)
        return RedirectResponse(f"{BASE_URL}/#/reglages?gmail=erreur")
    return RedirectResponse(f"{BASE_URL}/#/reglages?gmail=ok")


@app.post("/api/gmail/disconnect")
async def gmail_disconnect() -> dict:
    gmail.disconnect()
    return {"ok": True}


class TestMailPayload(BaseModel):
    job_title: str = "développeur Python"
    run_id: int | None = None


@app.post("/api/gmail/test")
async def gmail_test(payload: TestMailPayload) -> dict:
    """Un vrai envoi, vers la boîte connectée uniquement : pour se relire."""
    to = (gmail.connected_email() or "").strip().lower()
    if not to:
        raise HTTPException(status_code=400, detail="Gmail n'est pas connecté")
    sample = None
    if payload.run_id:
        picked = engine.pick_recipients(payload.run_id)["recipients"]
        sample = picked[0] if picked else None
    try:
        return await engine.send_test(to, payload.job_title.strip() or "développeur Python", sample)
    except gmail.GmailNotConnected as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except gmail.GmailError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ------------------------------------------------------------- campagnes ---

class CampaignCreate(BaseModel):
    name: str | None = None
    job_title: str = Field(min_length=2)
    zone: str | None = None
    sectors: list[str] = Field(default_factory=list)
    run_id: int | None = None
    recipients: list[str] = Field(default_factory=list)
    subject_tpl: str | None = None
    body_tpl: str | None = None
    personalize: bool = True
    cv_path: str | None = None
    daily_cap: int | None = Field(default=None, ge=1, le=200)


class CampaignPatch(BaseModel):
    name: str | None = None
    subject_tpl: str | None = None
    body_tpl: str | None = None
    personalize: bool | None = None
    daily_cap: int | None = Field(default=None, ge=1, le=200)


class PreviewPayload(BaseModel):
    job_title: str
    subject_tpl: str | None = None
    body_tpl: str | None = None
    personalize: bool = True
    application: dict = Field(default_factory=dict)


@app.get("/api/dashboard")
async def dashboard() -> dict:
    campaigns = db.list_campaigns()
    return {
        "quota": engine.quota_state(),
        "totals": {"sent": sum(c["sent"] for c in campaigns),
                   "opened": sum(c["opened"] for c in campaigns),
                   "replied": sum(c["replied"] for c in campaigns)},
        "campaigns": campaigns,
        "events": db.list_events(limit=30),
    }


@app.get("/api/market")
async def market(zone: str | None = None, min_headcount: int | None = None,
                 max_headcount: int | None = None) -> list[dict]:
    return await engine.market((zone or "").strip() or None, min_headcount, max_headcount)


@app.get("/api/runs/{run_id}/recipients")
async def run_recipients(run_id: int) -> dict:
    if db.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Recherche inconnue")
    return engine.pick_recipients(run_id)


@app.post("/api/compose/preview")
async def compose_preview(payload: PreviewPayload) -> dict:
    campaign = {"job_title": payload.job_title, "subject_tpl": payload.subject_tpl,
                "body_tpl": payload.body_tpl}
    rendered = await compose.compose(payload.application, campaign, db.all_settings(),
                                     personalize=payload.personalize)
    db.set_company_brief(payload.application.get("company_siren"), rendered.get("brief"))
    return rendered


class BriefsPayload(BaseModel):
    job_title: str = Field(min_length=2)


@app.post("/api/runs/{run_id}/briefs")
async def run_briefs(run_id: int, payload: BriefsPayload) -> dict:
    """Fiche « enjeux » pour chaque entreprise retenue : à lire avant d'écrire."""
    if db.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Recherche inconnue")
    try:
        return await engine.analyse_briefs(run_id, payload.job_title.strip())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/campaigns")
async def campaigns_list() -> list[dict]:
    return db.list_campaigns()


@app.post("/api/campaigns")
async def campaigns_create(payload: CampaignCreate) -> dict:
    campaign_id = engine.create(payload.model_dump())
    return db.get_campaign(campaign_id)


def _campaign_or_404(campaign_id: int) -> dict:
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campagne inconnue")
    return campaign


@app.get("/api/campaigns/{campaign_id}")
async def campaigns_get(campaign_id: int) -> dict:
    campaign = _campaign_or_404(campaign_id)
    campaign["counts"] = db.application_status_counts(campaign_id)
    campaign["quota"] = engine.quota_state()
    return campaign


@app.patch("/api/campaigns/{campaign_id}")
async def campaigns_patch(campaign_id: int, payload: CampaignPatch) -> dict:
    _campaign_or_404(campaign_id)
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "personalize" in changes:
        changes["personalize"] = 1 if changes["personalize"] else 0
    db.update_campaign(campaign_id, **changes)
    return db.get_campaign(campaign_id)


@app.delete("/api/campaigns/{campaign_id}", status_code=204, response_class=Response)
async def campaigns_delete(campaign_id: int) -> Response:
    db.delete_campaign(campaign_id)
    return Response(status_code=204)


@app.post("/api/campaigns/{campaign_id}/launch")
async def campaigns_launch(campaign_id: int) -> dict:
    _campaign_or_404(campaign_id)
    if not engine.dry_run() and not gmail.is_connected():
        raise HTTPException(status_code=400, detail="Connecte d'abord ton Gmail dans les réglages, "
                                                    "ou active le mode simulation")
    return engine.launch(campaign_id)


@app.post("/api/campaigns/{campaign_id}/pause")
async def campaigns_pause(campaign_id: int) -> dict:
    _campaign_or_404(campaign_id)
    return engine.pause(campaign_id)


@app.post("/api/campaigns/{campaign_id}/resume")
async def campaigns_resume(campaign_id: int) -> dict:
    _campaign_or_404(campaign_id)
    return engine.resume(campaign_id)


@app.get("/api/campaigns/{campaign_id}/applications")
async def campaigns_applications(campaign_id: int, status: str | None = None, q: str = "",
                                 page: int = 1, size: int = 10) -> dict:
    _campaign_or_404(campaign_id)
    rows, total = db.list_applications(campaign_id, status or None, q.strip(),
                                       max(page, 1), min(max(size, 1), 100))
    return {"rows": rows, "total": total, "page": page, "size": size,
            "counts": db.application_status_counts(campaign_id)}


@app.get("/api/campaigns/{campaign_id}/events")
async def campaigns_events(campaign_id: int, limit: int = 40) -> list[dict]:
    return db.list_events(campaign_id, limit)


@app.get("/api/campaigns/{campaign_id}/followups")
async def campaigns_followups(campaign_id: int) -> list[dict]:
    return db.priority_followups(campaign_id)


@app.post("/api/campaigns/{campaign_id}/sync")
async def campaigns_sync(campaign_id: int) -> dict:
    return {"replies": await engine.sync_replies()}


@app.get("/api/applications/{application_id}")
async def application_get(application_id: int) -> dict:
    row = db.get_application(application_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Candidature inconnue")
    return row


@app.post("/api/applications/{application_id}/preview")
async def application_preview(application_id: int) -> dict:
    """Rédige (sans envoyer) le mail d'une candidature, et le mémorise."""
    row = db.get_application(application_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Candidature inconnue")
    campaign = _campaign_or_404(row["campaign_id"])
    rendered = await compose.compose(row, campaign, db.all_settings(),
                                     personalize=bool(campaign.get("personalize")))
    db.update_application(application_id, subject=rendered["subject"],
                          body_text=rendered["body_text"], body_html=rendered["body_html"],
                          hook=rendered["hook"], company_brief=rendered.get("brief"))
    db.set_company_brief(row.get("company_siren"), rendered.get("brief"))
    return {**db.get_application(application_id), "engine": rendered["engine"]}


@app.post("/api/applications/{application_id}/cancel")
async def application_cancel(application_id: int) -> dict:
    row = db.get_application(application_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Candidature inconnue")
    if row["status"] != "programme":
        raise HTTPException(status_code=400, detail="Seule une candidature programmée peut être annulée")
    db.update_application(application_id, status="annule")
    return {"ok": True}


# ------------------------------------------------------ pixel d'ouverture ---

_PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
          b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


@app.get("/t/{token}.gif")
async def tracking_pixel(token: str) -> Response:
    touched = db.record_open(token)
    if touched:
        db.add_event("ouverture", touched.get("company_name") or touched["email"],
                     campaign_id=touched["campaign_id"], application_id=touched["id"])
    return Response(content=_PIXEL, media_type="image/gif",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                             "Pragma": "no-cache", "Expires": "0"})


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
