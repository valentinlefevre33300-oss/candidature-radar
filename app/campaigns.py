"""Campagnes : qui contacter, quand, et l'envoi lui-même.

Une campagne part d'une recherche (les contacts trouvés), en retient **une
personne par entreprise** — la mieux placée pour une candidature — puis
programme les envois au rythme des quotas : un mail toutes les quelques minutes,
aux heures de bureau, jamais plus que le plafond du jour ni du mois.

L'envoi tourne en tâche de fond dans le serveur web. En mode simulation
(`CR_DRY_RUN=1`) toute la chaîne s'exécute, rédaction comprise, sans qu'aucun
mail ne parte : c'est le moyen de tester une campagne à blanc.
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import compose, db, gmail
from .config import DAILY_CAP, DRY_RUN, MONTHLY_CAP, SEND_INTERVAL
from .naf import SECTORS, codes_for
from .models import SearchQuery
from .sources.sirene import count_companies

log = logging.getLogger(__name__)

MIN_SCORE = 40
# Ordre de préférence quand une entreprise offre plusieurs contacts : d'abord la
# personne du métier visé (le responsable de service avant le pair), puis le
# dirigeant, puis les RH — qui reçoivent tout le monde — puis les boîtes.
CATEGORY_RANK = {"metier": 0, "direction": 1, "rh": 2, "nominatif": 3, "generique": 4, "inconnu": 5}
EXCLUDED_CATEGORIES = {"juridique", "technique", "commercial"}
SEND_WINDOW = (8, 19)      # heures locales : un mail à 3 h du matin sent le robot
TICK_SECONDS = 20
REPLY_SYNC_SECONDS = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dry_run() -> bool:
    """Simulation si l'environnement OU le réglage en base le demande."""
    return DRY_RUN or db.get_setting("dry_run") == "1"


# ------------------------------------------------------------ destinataires ---

def already_reached() -> tuple[set[str], set[str]]:
    """(adresses, SIREN) déjà démarchés : par une campagne, ou marqués dans le suivi."""
    emails: set[str] = set()
    sirens: set[str] = set()
    with db.connect() as conn:
        for row in conn.execute("SELECT email, company_siren FROM applications "
                                "WHERE status IN ('envoye', 'ouvert', 'repondu')"):
            emails.add(row["email"].lower())
            if row["company_siren"]:
                sirens.add(row["company_siren"])
    emails |= {e.lower() for e in db.already_contacted()}
    return emails, sirens


def pick_recipients(run_id: int, *, min_score: float = MIN_SCORE) -> dict:
    """Un interlocuteur par entreprise, à partir des contacts d'une recherche.

    Renvoie {"recipients": [...], "excluded": {...}} pour que l'assistant puisse
    dire ce qu'il a écarté et pourquoi.
    """
    reached_emails, reached_sirens = already_reached()
    best: dict[str, dict] = {}
    excluded = {"deja_contactes": 0, "hors_sujet": 0, "score_faible": 0, "domaine_tiers": 0}

    for contact in db.run_contacts(run_id):
        email = contact["email"].lower()
        key = contact.get("company_siren") or contact.get("company_name") or email
        if email in reached_emails or (contact.get("company_siren") in reached_sirens):
            excluded["deja_contactes"] += 1
            continue
        if contact.get("category") in EXCLUDED_CATEGORIES or contact.get("mx_ok") == 0:
            excluded["hors_sujet"] += 1
            continue
        if (contact.get("score") or 0) < min_score:
            excluded["score_faible"] += 1
            continue
        domain = contact.get("domain")
        if domain and not email.endswith("@" + domain.lower().removeprefix("www.")):
            excluded["domaine_tiers"] += 1   # agence web, hébergeur cités en mentions légales
            continue
        guessed = bool((contact.get("pattern_used") or "").endswith("?"))
        rank = (CATEGORY_RANK.get(contact.get("category"), 6), int(guessed),
                0 if contact.get("is_manager") else 1, -(contact.get("score") or 0),
                int(bool(contact.get("inferred"))))
        current = best.get(key)
        if current is None or rank < current["_rank"]:
            best[key] = {
                "_rank": rank,
                "email": email,
                "first_name": contact.get("first_name"),
                "last_name": contact.get("last_name"),
                "role_title": contact.get("role_title"),
                "category": contact.get("category"),
                "score": contact.get("score"),
                "inferred": bool(contact.get("inferred")),
                "guessed": guessed,
                "is_manager": bool(contact.get("is_manager")),
                "company_siren": contact.get("company_siren"),
                "company_name": contact.get("company_name"),
                "company_domain": domain,
                "company_city": contact.get("city"),
                "company_size": contact.get("size"),
                "company_naf": contact.get("naf"),
                "company_tagline": contact.get("tagline"),
                "company_about": contact.get("about"),
                "company_brief": contact.get("brief"),
            }

    recipients = sorted(best.values(), key=lambda r: r["_rank"])
    for r in recipients:
        r.pop("_rank", None)
    return {"recipients": recipients, "excluded": excluded}


# ------------------------------------------------------------ programmation ---

def schedule_times(count: int, *, daily_cap: int, interval: int = SEND_INTERVAL,
                   start: datetime | None = None) -> list[str]:
    """Horaires d'envoi en UTC ISO, espacés, aux heures de bureau, plafonnés par jour."""
    local_now = (start or datetime.now()).astimezone()
    cursor = local_now
    open_h, close_h = SEND_WINDOW
    per_day = 0
    out: list[str] = []
    for _ in range(count):
        # Hors fenêtre ou plafond du jour atteint : on reprend le lendemain matin.
        if cursor.hour >= close_h or per_day >= daily_cap:
            cursor = (cursor + timedelta(days=1)).replace(hour=open_h, minute=0, second=0, microsecond=0)
            per_day = 0
        if cursor.hour < open_h:
            cursor = cursor.replace(hour=open_h, minute=0, second=0, microsecond=0)
        out.append(cursor.astimezone(timezone.utc).isoformat(timespec="seconds"))
        per_day += 1
        cursor += timedelta(seconds=interval + random.randint(0, 90))
    return out


def create(payload: dict) -> int:
    """Crée une campagne en brouillon avec ses candidatures (non programmées)."""
    picked = pick_recipients(payload["run_id"]) if payload.get("run_id") else {"recipients": []}
    wanted = {e.lower() for e in payload.get("recipients") or []}
    rows = [r for r in picked["recipients"] if not wanted or r["email"] in wanted]

    campaign_id = db.create_campaign(
        name=payload.get("name") or payload["job_title"],
        job_title=payload["job_title"],
        zone=payload.get("zone"),
        sectors=",".join(payload.get("sectors") or []),
        run_id=payload.get("run_id"),
        status="brouillon",
        subject_tpl=payload.get("subject_tpl") or compose.DEFAULT_SUBJECT,
        body_tpl=payload.get("body_tpl") or compose.DEFAULT_BODY,
        personalize=1 if payload.get("personalize", True) else 0,
        cv_path=payload.get("cv_path"),
        daily_cap=payload.get("daily_cap"),
    )
    for r in rows:
        r["status"] = "programme"
        r["token"] = secrets.token_urlsafe(12)
    db.add_applications(campaign_id, rows)
    db.add_event("creation", f"{len(rows)} candidature(s) préparée(s)", campaign_id=campaign_id)
    return campaign_id


def _reschedule_pending(campaign: dict) -> int:
    """Redistribue les envois restants à partir de maintenant."""
    pending, _ = db.list_applications(campaign["id"], status="programme", size=100000)
    times = schedule_times(len(pending), daily_cap=campaign.get("daily_cap") or DAILY_CAP)
    for row, when in zip(pending, times):
        db.update_application(row["id"], scheduled_at=when)
    return len(pending)


def launch(campaign_id: int) -> dict:
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise KeyError(campaign_id)
    n = _reschedule_pending(campaign)
    db.update_campaign(campaign_id, status="active", launched_at=_now())
    db.add_event("lancement", f"{n} envoi(s) programmé(s)", campaign_id=campaign_id)
    return db.get_campaign(campaign_id)


def pause(campaign_id: int) -> dict:
    db.update_campaign(campaign_id, status="en_pause")
    db.add_event("pause", "", campaign_id=campaign_id)
    return db.get_campaign(campaign_id)


def resume(campaign_id: int) -> dict:
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise KeyError(campaign_id)
    n = _reschedule_pending(campaign)   # sinon tous les envois en retard partiraient d'un coup
    db.update_campaign(campaign_id, status="active")
    db.add_event("reprise", f"{n} envoi(s) reprogrammé(s)", campaign_id=campaign_id)
    return db.get_campaign(campaign_id)


# ----------------------------------------------------------------- quotas ---

def quota_state() -> dict:
    today = datetime.now(timezone.utc)
    month_sent = db.sent_since(today.strftime("%Y-%m"))
    day_sent = db.sent_since(today.strftime("%Y-%m-%d"))
    reason = None
    if month_sent >= MONTHLY_CAP:
        reason = "plafond mensuel atteint"
    elif day_sent >= DAILY_CAP:
        reason = "plafond du jour atteint"
    else:
        last = db.last_sent_at()
        if last:
            elapsed = (today - datetime.fromisoformat(last)).total_seconds()
            if elapsed < SEND_INTERVAL:
                reason = f"prochain envoi dans {int(SEND_INTERVAL - elapsed)} s"
    return {"month_sent": month_sent, "month_cap": MONTHLY_CAP, "day_sent": day_sent,
            "day_cap": DAILY_CAP, "can_send": reason is None, "reason": reason,
            "dry_run": dry_run()}


# ------------------------------------------------------------------ envoi ---

def _attachment(campaign: dict, settings: dict) -> list[tuple[str, bytes]]:
    path = campaign.get("cv_path") or settings.get("cv_path")
    if not path or not Path(path).exists():
        return []
    return [(Path(path).name, Path(path).read_bytes())]


async def send_one(application: dict) -> None:
    campaign = db.get_campaign(application["campaign_id"])
    settings = db.all_settings()
    if campaign is None:
        return

    # Rédaction au moment de l'envoi, sauf si le mail a déjà été rédigé (et
    # peut-être retouché à la main) depuis l'interface.
    if not application.get("body_text"):
        rendered = await compose.compose(application, campaign, settings,
                                         personalize=bool(campaign.get("personalize")))
        db.update_application(application["id"], subject=rendered["subject"],
                              body_text=rendered["body_text"], body_html=rendered["body_html"],
                              hook=rendered["hook"], company_brief=rendered.get("brief"))
        db.set_company_brief(application.get("company_siren"), rendered.get("brief"))
        application.update(rendered)
    elif not application.get("body_html"):
        application["body_html"] = compose.to_html(
            application["body_text"], compose.pixel_url(application.get("token")))
        db.update_application(application["id"], body_html=application["body_html"])

    if dry_run():
        db.update_application(application["id"], status="envoye", sent_at=_now(),
                              gmail_message_id="simulation", gmail_thread_id=None)
        db.add_event("envoi", f"{application.get('company_name')} (simulation)",
                     campaign_id=campaign["id"], application_id=application["id"])
        return

    if not gmail.is_connected():
        db.update_campaign(campaign["id"], status="en_pause")
        db.add_event("erreur", "Gmail n'est pas connecté : campagne mise en pause",
                     campaign_id=campaign["id"])
        return

    try:
        message = gmail.build_message(
            sender=gmail.connected_email() or "",
            sender_name=settings.get("sender_name") or "",
            to=application["email"],
            subject=application["subject"],
            text=application["body_text"],
            html=application["body_html"],
            attachments=_attachment(campaign, settings),
        )
        message_id, thread_id = await gmail.send(message)
    except gmail.GmailNotConnected as exc:
        # L'autorisation Google a expiré : la candidature reste programmée, la
        # campagne attend une reconnexion. Rien n'est perdu.
        db.update_campaign(campaign["id"], status="en_pause")
        db.add_event("erreur", f"Gmail à reconnecter — campagne mise en pause ({str(exc)[:80]})",
                     campaign_id=campaign["id"])
        return
    except Exception as exc:
        log.warning("envoi impossible vers %s : %s", application["email"], exc)
        db.update_application(application["id"], status="echec", error=str(exc)[:300])
        db.add_event("erreur", f"{application.get('company_name')} : {str(exc)[:120]}",
                     campaign_id=campaign["id"], application_id=application["id"])
        return

    db.update_application(application["id"], status="envoye", sent_at=_now(),
                          gmail_message_id=message_id, gmail_thread_id=thread_id)
    db.add_event("envoi", application.get("company_name") or application["email"],
                 campaign_id=campaign["id"], application_id=application["id"])


async def analyse_briefs(run_id: int, job_title: str) -> dict[str, str]:
    """Rédige la fiche « enjeux » de chaque entreprise retenue pour une recherche.

    Une fiche par entreprise, mémorisée : relancer ne coûte rien pour celles
    déjà analysées. Renvoie {siren: fiche}.
    """
    if not compose.claude_available():
        raise RuntimeError("clé Claude absente : l'analyse des enjeux n'est pas disponible")
    settings = db.all_settings()
    campaign = {"job_title": job_title}
    recipients = pick_recipients(run_id)["recipients"]
    semaphore = asyncio.Semaphore(4)
    result: dict[str, str] = {}

    async def one(recipient: dict) -> None:
        siren = recipient.get("company_siren") or recipient["email"]
        if recipient.get("company_brief"):
            result[siren] = recipient["company_brief"]
            return
        async with semaphore:
            try:
                brief = await compose.claude_brief(recipient, compose.build_context(recipient, campaign, settings))
            except Exception as exc:
                log.warning("fiche impossible pour %s : %s", recipient.get("company_name"), exc)
                return
        db.set_company_brief(recipient.get("company_siren"), brief)
        result[siren] = brief

    await asyncio.gather(*(one(r) for r in recipients))
    return result


async def send_test(to: str, job_title: str, sample: dict | None = None) -> dict:
    """Envoie un vrai mail de test — hors campagne, hors quotas — pour se relire.

    `sample` est un destinataire réel (entreprise, fonction) dont on reprend les
    faits pour que le paragraphe personnalisé soit représentatif ; seule
    l'adresse est remplacée par celle du testeur.
    """
    if not gmail.is_connected():
        raise gmail.GmailNotConnected("Gmail n'est pas connecté")
    settings = db.all_settings()
    campaign = {"job_title": job_title, "subject_tpl": None, "body_tpl": None}
    application = dict(sample or {"company_name": "Exemple SAS", "company_city": "Bordeaux",
                                  "company_size": "20 à 49 salariés", "company_naf": "62.01Z"})
    application["email"] = to
    application["token"] = None   # pas de pixel sur un test
    rendered = await compose.compose(application, campaign, settings, personalize=True)
    attachments = _attachment(campaign, settings)
    message = gmail.build_message(
        sender=gmail.connected_email() or "", sender_name=settings.get("sender_name") or "",
        to=to, subject=rendered["subject"], text=rendered["body_text"], html=rendered["body_html"],
        attachments=attachments)
    message_id, _thread = await gmail.send(message)
    db.add_event("test", f"mail de test envoyé à {to}")
    return {"to": to, "subject": rendered["subject"], "engine": rendered["engine"],
            "attachment": attachments[0][0] if attachments else None, "message_id": message_id,
            "sample": application.get("company_name")}


def _close_if_done(campaign_id: int) -> None:
    campaign = db.get_campaign(campaign_id)
    if campaign and campaign["status"] == "active" and campaign["scheduled"] == 0:
        db.update_campaign(campaign_id, status="terminee")
        db.add_event("fin", "tous les envois sont partis", campaign_id=campaign_id)


async def tick() -> None:
    """Un passage de la boucle : au plus un envoi, si les quotas le permettent."""
    if not quota_state()["can_send"]:
        return
    for application in db.due_applications(limit=1):
        await send_one(application)
        _close_if_done(application["campaign_id"])


async def sender_loop() -> None:
    log.info("boucle d'envoi démarrée (%s)", "simulation" if DRY_RUN else "réel")
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("boucle d'envoi : erreur ignorée")
        await asyncio.sleep(TICK_SECONDS)


# --------------------------------------------------------------- réponses ---

async def sync_replies() -> int:
    """Marque « a répondu » les candidatures dont le fil Gmail contient une réponse."""
    if dry_run() or not gmail.is_connected():
        return 0
    own = gmail.connected_email() or ""
    found = 0
    for application in db.pending_reply_checks():
        try:
            if await gmail.thread_has_reply(application["gmail_thread_id"], own):
                db.update_application(application["id"], status="repondu", replied_at=_now())
                db.add_event("reponse", application.get("company_name") or application["email"],
                             campaign_id=application["campaign_id"], application_id=application["id"])
                found += 1
        except Exception as exc:
            log.debug("vérification de réponse impossible : %s", exc)
        await asyncio.sleep(0.3)
    return found


async def reply_loop() -> None:
    while True:
        try:
            await sync_replies()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("synchronisation des réponses : erreur ignorée")
        await asyncio.sleep(REPLY_SYNC_SECONDS)


# ------------------------------------------------------------------ marché ---

async def market(cities: list[dict], agglomeration: bool, zone: str | None,
                 min_headcount: int | None, max_headcount: int | None) -> list[dict]:
    """Nombre d'entreprises par secteur pour une cible : l'« analyse du marché »."""
    department = zone if zone and len(zone) <= 3 and not cities else None
    postal = zone if zone and len(zone) == 5 and not cities else None
    semaphore = asyncio.Semaphore(4)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        async def one(key: str, value: dict) -> dict:
            query = SearchQuery(job_title="", naf_codes=list(value["codes"]), department=department,
                                postal_code=postal, cities=cities, agglomeration=agglomeration,
                                min_headcount=min_headcount, max_headcount=max_headcount, limit=1)
            async with semaphore:
                total = await count_companies(client, query)
            return {"key": key, "label": value["label"], "count": total}

        rows = await asyncio.gather(*(one(k, v) for k, v in SECTORS.items()))
    return sorted(rows, key=lambda r: -r["count"])
