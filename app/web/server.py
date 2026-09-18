"""Interface web locale : lancer une recherche, suivre sa progression, exporter.

Le serveur n'est pas prevu pour etre expose : il ecoute sur la boucle locale et
n'a aucune authentification. Les recherches tournent en tache de fond et
diffusent leur avancement via Server-Sent Events.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import asdict
import json
import logging
import re
import secrets
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import httpx

from .. import campaigns as engine
from .. import auth, compose, db, geo, gmail
from ..config import (APP_PASSWORD, APP_USER, BASE_URL, DAILY_CAP, DATA_DIR, DRY_RUN, MONTHLY_CAP,
                      PUBLIC_URL, REQUIRE_LOGIN)
from ..models import Company, SearchQuery
from ..naf import DOMAIN_MIN_HEADCOUNT, apply_floor, catalogue, codes_for, label_for_code, min_headcount_for, sector_for_code
from ..sources.sirene import count_companies, search_companies
from ..resolve import linkedin as linkedin_lookup
from ..pipeline import run_search

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"


def _static_version() -> str:
    """Empreinte du contenu de l'interface : change à chaque modification.

    Les fichiers sont servis sous /s/<empreinte>/… ; une nouvelle version a
    donc de nouvelles adresses, et aucune copie gardée par un navigateur ou un
    cache intermédiaire ne peut être resservie à sa place.
    """
    digest = hashlib.sha1()
    for path in sorted(STATIC.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(STATIC).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


STATIC_VERSION = _static_version()

app = FastAPI(title="Candidature Radar", docs_url="/api/docs")


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
# Ce qui reste ouvert sans session : le pixel (les messageries le chargent), la
# page de connexion et les retours de Google.
_OPEN_PREFIXES = ("/t/", "/login", "/api/auth/", "/api/gmail/callback", "/health")


def _is_local(request) -> bool:
    """Requête venue du poste lui-même, sans rien devant.

    Derrière un reverse proxy (tunnel Cloudflare, hébergeur), uvicorn voit 127.0.0.1 pour tout le
    monde : l'en-tête transmis par le proxy et le nom d'hôte demandé trahissent
    alors la vraie provenance, et on exige la connexion.
    """
    client = (request.client.host if request.client else "") or ""
    if client not in _LOCAL_HOSTS:
        return False
    if any(request.headers.get(h) for h in ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip")):
        return False
    host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
    return host in _LOCAL_HOSTS


def _basic_ok(request) -> bool:
    """Mot de passe HTTP : la voie de secours pour le téléphone sur le Wi-Fi,
    où Google refuse de renvoyer vers une adresse IP."""
    if not APP_PASSWORD:
        return False
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(password, APP_PASSWORD) and secrets.compare_digest(user, APP_USER)


def current_user(request) -> dict | None:
    session = auth.read_session(request.cookies.get(auth.COOKIE))
    if session and auth.is_allowed(session.get("email")):
        return session
    return None


@app.middleware("http")
async def _guard(request, call_next):
    """Connexion Google devant l'interface (mot de passe HTTP en secours).

    L'outil envoie des mails depuis un Gmail : dès qu'il est joignable d'ailleurs
    que du poste local, il faut être connecté. Le poste local entre sans rien,
    sauf si CR_REQUIRE_LOGIN=1.
    """
    path = request.url.path
    if path.startswith(_OPEN_PREFIXES):
        return await call_next(request)
    if not REQUIRE_LOGIN and _is_local(request):
        return await call_next(request)
    if current_user(request) or _basic_ok(request):
        return await call_next(request)
    if request.query_params.get("basic") == "1" and APP_PASSWORD:
        return Response("Authentification requise", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Candidature Radar", charset="UTF-8"'})
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Connexion requise"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


@app.middleware("http")
async def _revalidate_static(request, call_next):
    """Les modules JS sont importés sans suffixe de version : sans cet en-tête,
    le navigateur resservait un `core.js` périmé à côté de vues à jour.

    `private, no-store` plutôt que `no-cache` : derrière Cloudflare, `no-cache`
    était remplacé par un `max-age` de quatre heures (« Browser Cache TTL »),
    et le téléphone gardait une interface périmée après chaque mise à jour.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path == "/login" or path.startswith(("/static/", "/s/")):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["CDN-Cache-Control"] = "no-store"
    return response


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
    cities: list[dict] = Field(default_factory=list)   # communes choisies (code INSEE, nom, EPCI…)
    agglomeration: bool = False                        # étendre chaque ville à son intercommunalité
    department: str | None = None
    postal_code: str | None = None
    min_headcount: int | None = None
    max_headcount: int | None = None
    limit: int = Field(default=30, ge=1, le=200)
    use_search_engine: bool = True
    verify_smtp: bool = False
    companies: list[dict] = Field(default_factory=list)   # entreprises choisies à la main (étape « Les entreprises »)


BACKGROUND: list[asyncio.Task] = []


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    # Envoi et détection des réponses tournent tant que le serveur est ouvert.
    BACKGROUND.append(asyncio.create_task(engine.sender_loop()))
    BACKGROUND.append(asyncio.create_task(engine.reply_loop()))
    BACKGROUND.append(asyncio.create_task(engine.prepare_loop()))


@app.on_event("shutdown")
async def _shutdown() -> None:
    for task in BACKGROUND:
        task.cancel()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("/static/", f"/s/{STATIC_VERSION}/"))



@app.get("/health")
async def health() -> dict:
    """Bilan de santé pour l'hébergeur : ouvert, sans rien révéler."""
    return {"ok": True}

# ------------------------------------------------------------- Connexion ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if current_user(request) or (not REQUIRE_LOGIN and _is_local(request)):
        return RedirectResponse("/", status_code=302)
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    if not APP_PASSWORD:   # pas de mot de passe défini : inutile de proposer la voie de secours
        html = re.sub(r'<div class="alt">.*?</div>\n', "", html, flags=re.S)
    return HTMLResponse(html)


@app.get("/api/auth/google")
async def auth_google() -> RedirectResponse:
    try:
        return RedirectResponse(auth.login_url())
    except auth.AuthError:
        return RedirectResponse("/login?error=config")


async def _login_callback(code: str, state: str, error: str) -> RedirectResponse:
    if error or not code:
        return RedirectResponse("/login?error=annule")
    try:
        who = await auth.exchange(code, state)
    except auth.AuthError as exc:
        log.warning("connexion Google échouée : %s", exc)
        return RedirectResponse("/login?error=erreur")
    if not auth.is_allowed(who["email"]):
        log.warning("connexion refusée pour %s (compte non autorisé)", who["email"])
        return RedirectResponse("/login?error=refuse")
    resp = RedirectResponse(BASE_URL + "/", status_code=302)
    resp.set_cookie(value=auth.make_session(**who), **auth.cookie_kwargs())
    return resp


@app.get("/api/auth/logout")
async def auth_logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/me")
async def me(request: Request) -> dict:
    """Qui est connecté, pour l'avatar de la barre latérale."""
    user = current_user(request) or {}
    return {"email": user.get("email", ""), "name": user.get("name", ""), "picture": user.get("picture", ""),
            "session": bool(user), "local": _is_local(request), "allowed": sorted(auth.allowed_emails())}


@app.get("/api/sectors")
async def sectors() -> list[dict]:
    return catalogue()


@app.get("/api/geo/communes")
async def geo_communes(q: str = "") -> list[dict]:
    """Autocomplétion des villes (API géographique de l'État), les plus peuplées d'abord."""
    async with httpx.AsyncClient() as client:
        return [c.to_dict() for c in await geo.suggest_cities(client, q)]


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

    cities = [c for c in payload.cities if c.get("code")]
    lo, hi = apply_floor(payload.min_headcount, payload.max_headcount, payload.job_title)   # un PO n'existe pas sous 10 salariés
    query = SearchQuery(
        job_title=payload.job_title.strip(),
        keywords=payload.keywords.strip(),
        department=(payload.department or "").strip() or None,
        postal_code=(payload.postal_code or "").strip() or None,
        cities=cities,
        agglomeration=payload.agglomeration,
        naf_codes=list(dict.fromkeys(naf)),
        min_headcount=lo,
        max_headcount=hi,
        limit=payload.limit,
    )

    picked = [Company.from_dict(c) for c in payload.companies if isinstance(c, dict) and c.get("siren")]
    params = payload.model_dump()
    if picked:   # on garde la trace des choix sans stocker les fiches entières
        params["companies"] = [c.siren for c in picked]
        query.limit = len(picked)
    run_id = db.start_run(
        job_title=query.job_title,
        sectors=",".join(payload.sectors),
        department=query.department or (str(cities[0].get("department") or "") or None if cities else None),
        params=json.dumps(params, ensure_ascii=False),
    )
    job = Job(run_id)
    JOBS[run_id] = job

    asyncio.create_task(_execute(job, query, payload, picked or None))
    return {"run_id": run_id}


async def _execute(job: Job, query: SearchQuery, payload: SearchPayload,
                   picked: list[Company] | None = None) -> None:
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
            companies=picked,
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


class ExportPayload(BaseModel):
    emails: list[str] = Field(default_factory=list)


@app.post("/api/runs/{run_id}/export")
async def export_selection(run_id: int, payload: ExportPayload) -> FileResponse:
    """CSV des seules personnes cochées (toutes si la liste est vide)."""
    wanted = {e.strip().lower() for e in payload.emails}
    rows = [r for r in db.run_contacts(run_id) if not wanted or str(r.get("email", "")).lower() in wanted]
    if not rows:
        raise HTTPException(status_code=404, detail="Aucun contact a exporter")
    path = db.export_csv(run_id, emails=sorted(wanted) or None)
    return FileResponse(path, filename=path.name, media_type="text/csv")


class LinkedinPayload(BaseModel):
    emails: list[str] = Field(default_factory=list)


@app.post("/api/runs/{run_id}/linkedin")
async def find_linkedin(run_id: int, payload: LinkedinPayload) -> dict:
    """Cherche par moteur le profil des personnes nommées qui n'en ont pas encore."""
    wanted = {e.strip().lower() for e in payload.emails}
    rows = [r for r in db.run_contacts(run_id)
            if (not wanted or str(r.get("email", "")).lower() in wanted)
            and r.get("first_name") and r.get("last_name") and not r.get("linkedin_url")][:40]
    found: dict[str, str] = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for row in rows:
            url = await linkedin_lookup.find_profile(client, row["first_name"], row["last_name"],
                                                    compose.pretty_company(row.get("company_name")))
            if url:
                db.set_contact_linkedin(run_id, row["email"], url)
                found[row["email"]] = url
            await asyncio.sleep(1.2)   # un moteur gratuit, on ne le martèle pas
    return {"searched": len(rows), "found": found}


class ExplorePayload(BaseModel):
    job_title: str = ""
    anywhere: bool = False      # recherche par nom : ignorer la zone (siège déclaré ailleurs)
    sectors: list[str] = Field(default_factory=list)
    naf_codes: list[str] = Field(default_factory=list)
    keywords: str = ""
    cities: list[dict] = Field(default_factory=list)
    agglomeration: bool = False
    department: str | None = None
    postal_code: str | None = None
    min_headcount: int | None = None
    max_headcount: int | None = None
    limit: int = Field(default=100, ge=1, le=400)


@app.post("/api/companies")
async def explore_companies(payload: ExplorePayload) -> dict:
    """Les entreprises de la cible, à choisir une par une avant d'explorer leurs sites."""
    naf = codes_for(payload.sectors) + [c for c in payload.naf_codes if c]
    if not naf and not payload.keywords.strip():
        raise HTTPException(status_code=400, detail="Choisissez au moins un secteur.")
    query = SearchQuery(
        job_title="", keywords=payload.keywords.strip(),
        department=(payload.department or "").strip() or None,
        postal_code=(payload.postal_code or "").strip() or None,
        cities=[] if payload.anywhere else [c for c in payload.cities if c.get("code")],
        agglomeration=payload.agglomeration and not payload.anywhere,
        naf_codes=list(dict.fromkeys(naf)),
        # Recherche par nom : on connaît l'entreprise, on ne filtre pas l'effectif —
        # beaucoup de sociétés (Betclic…) ne le déclarent pas et seraient invisibles.
        min_headcount=None if payload.keywords.strip() else apply_floor(payload.min_headcount, payload.max_headcount, payload.job_title)[0],
        max_headcount=None if payload.keywords.strip() else apply_floor(payload.min_headcount, payload.max_headcount, payload.job_title)[1],
        limit=payload.limit,
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        total, companies = await asyncio.gather(count_companies(client, query), search_companies(client, query))
    _, reached = engine.already_reached()
    rows = []
    for company in companies:
        row = asdict(company)
        row["sector"] = sector_for_code(company.naf)
        row["sector_label"] = label_for_code(company.naf)
        row["reached"] = company.siren in reached
        rows.append(row)
    return {"total": total, "companies": rows}


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

SETTING_KEYS = ("sender_name", "signature", "profile_summary", "linkedin_url", "portfolio_url",
                "subject_tpl", "body_tpl", "review_mode", "review_minutes", "dry_run")
CV_DIR = DATA_DIR / "cv"


@app.get("/api/settings")
async def settings_get() -> dict:
    values = db.all_settings()
    return {
        "settings": {k: values.get(k, "") for k in (*SETTING_KEYS, "cv_path")},
        "cv_name": Path(values["cv_path"]).name if values.get("cv_path") else None,
        "gmail": {"configured": gmail.configured(), "connected": gmail.is_connected(),
                  "needs_reconnect": gmail.needs_reconnect(), "email": gmail.connected_email(),
                  "missing_scopes": gmail.missing_scopes(),
                  "redirect_uri": BASE_URL + gmail.REDIRECT_PATH},
        "claude": compose.claude_available(),
        "pixel": bool(PUBLIC_URL),
        "dry_run": engine.dry_run(),
        "review": {"mode": engine.review_mode(), "minutes": engine.review_minutes()},
        "headcount_floors": DOMAIN_MIN_HEADCOUNT,
        "dry_run_forced": DRY_RUN,
        "caps": {"monthly": MONTHLY_CAP, "daily": DAILY_CAP},
        "variables": compose.VARIABLES,
        # Le modèle des nouvelles campagnes : celui des réglages, sinon celui d'origine.
        "defaults": {"subject": values.get("subject_tpl") or compose.DEFAULT_SUBJECT,
                     "body": values.get("body_tpl") or compose.DEFAULT_BODY},
        "factory": {"subject": compose.DEFAULT_SUBJECT, "body": compose.DEFAULT_BODY},
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
    if auth.is_login_state(state):   # même adresse de retour pour les deux flux Google
        return await _login_callback(code, state, error)
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


class MarketPayload(BaseModel):
    job_title: str = ""
    cities: list[dict] = Field(default_factory=list)
    agglomeration: bool = False
    zone: str | None = None
    min_headcount: int | None = None
    max_headcount: int | None = None


@app.post("/api/market")
async def market(payload: MarketPayload) -> list[dict]:
    cities = [c for c in payload.cities if c.get("code")]
    return await engine.market(cities, payload.agglomeration, (payload.zone or "").strip() or None,
                               payload.min_headcount, payload.max_headcount, payload.job_title)


@app.get("/api/runs/{run_id}/recipients")
async def run_recipients(run_id: int, fit: int = 1) -> dict:
    """Les destinataires proposés ; `fit=0` garde aussi les entreprises sans trace du métier."""
    if db.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Recherche inconnue")
    return engine.pick_recipients(run_id, require_fit=bool(fit))


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
    result = engine.launch(campaign_id)
    asyncio.create_task(engine.prepare_campaign(campaign_id))   # le premier lot, sans attendre la boucle
    return result


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


# ------------------------------------------------- tableau de bord & relances ---

@app.get("/api/outcomes")
async def outcomes() -> dict:
    return engine.outcomes()


@app.post("/api/replies/sync")
async def replies_sync() -> dict:
    if not gmail.is_connected():
        raise HTTPException(status_code=400, detail="Gmail n'est pas connecté")
    if gmail.missing_scopes():
        raise HTTPException(status_code=400, detail="Reconnecte ton Gmail : la lecture des réponses "
                                                    "demande une nouvelle permission")
    return {"replies": await engine.sync_replies()}


class ReplyKindPayload(BaseModel):
    reply_kind: str


@app.patch("/api/applications/{application_id}/reply")
async def application_reply_kind(application_id: int, payload: ReplyKindPayload) -> dict:
    if payload.reply_kind not in db.REPLY_KINDS:
        raise HTTPException(status_code=400, detail="Nature de réponse inconnue")
    if db.get_application(application_id) is None:
        raise HTTPException(status_code=404, detail="Candidature inconnue")
    db.update_application(application_id, reply_kind=payload.reply_kind)
    return {"ok": True}


class FollowupPayload(BaseModel):
    subject: str | None = None
    body_text: str | None = None


@app.post("/api/applications/{application_id}/followup/preview")
async def followup_preview(application_id: int) -> dict:
    try:
        return await engine.prepare_followup(application_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidature inconnue")


@app.post("/api/applications/{application_id}/followup")
async def followup_send(application_id: int, payload: FollowupPayload) -> dict:
    try:
        return await engine.send_followup(application_id, payload.subject, payload.body_text)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidature inconnue")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except gmail.GmailNotConnected as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except gmail.GmailError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


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
    """Bloque un envoi à venir (en file, à valider ou programmé)."""
    try:
        return engine.block(application_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidature inconnue")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/applications/{application_id}/validate")
async def application_validate(application_id: int) -> dict:
    try:
        return engine.validate(application_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidature inconnue")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class ApplicationEdit(BaseModel):
    subject: str | None = None
    body_text: str | None = None


@app.patch("/api/applications/{application_id}")
async def application_edit(application_id: int, payload: ApplicationEdit) -> dict:
    try:
        return engine.edit(application_id, payload.subject, payload.body_text)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidature inconnue")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/campaigns/{campaign_id}/validate-all")
async def campaign_validate_all(campaign_id: int) -> dict:
    _campaign_or_404(campaign_id)
    return {"validated": engine.validate_all(campaign_id)}


@app.post("/api/campaigns/{campaign_id}/prepare")
async def campaign_prepare(campaign_id: int) -> dict:
    """Rédige tout de suite le prochain lot (sans attendre la boucle)."""
    _campaign_or_404(campaign_id)
    return {"prepared": await engine.prepare_campaign(campaign_id)}


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


app.mount(f"/s/{STATIC_VERSION}", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static_plain")   # anciennes adresses
