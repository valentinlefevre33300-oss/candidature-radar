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

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import db
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


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()


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
