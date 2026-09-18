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
from .naf import SECTORS, apply_floor, codes_for, min_headcount_for, relevant_sectors
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
PREPARE_SECONDS = 60       # la boucle qui rédige à l'avance le lot du jour
REVIEW_MINUTES = 60        # délai minimum entre la rédaction et le premier départ
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


def pick_recipients(run_id: int, *, min_score: float = MIN_SCORE, require_fit: bool = True) -> dict:
    """Un interlocuteur par entreprise, à partir des contacts d'une recherche.

    `require_fit` : n'écrire qu'aux entreprises où le métier visé (ou un métier
    voisin) a laissé une trace sur le site — moins de volume, plus de pertinence.
    Renvoie {"recipients": [...], "excluded": {...}} pour que l'assistant puisse
    dire ce qu'il a écarté et pourquoi.
    """
    reached_emails, reached_sirens = already_reached()
    best: dict[str, dict] = {}
    excluded = {"deja_contactes": 0, "hors_sujet": 0, "score_faible": 0, "domaine_tiers": 0, "sans_metier": 0}
    no_fit: set[str] = set()

    for contact in db.run_contacts(run_id):
        email = contact["email"].lower()
        key = contact.get("company_siren") or contact.get("company_name") or email
        if require_fit and not (contact.get("fit") or 0) and contact.get("category") != "metier":
            no_fit.add(key)
            continue
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
                "linkedin_url": contact.get("linkedin_url"),
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
                "company_fit": contact.get("fit") or 0,
                "company_fit_terms": contact.get("fit_terms"),
            }

    excluded["sans_metier"] = len(no_fit - set(best))
    recipients = sorted(best.values(), key=lambda r: (-min(r["company_fit"], 6), r["_rank"]))
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
        subject_tpl=payload.get("subject_tpl") or db.get_setting("subject_tpl") or compose.DEFAULT_SUBJECT,
        body_tpl=payload.get("body_tpl") or db.get_setting("body_tpl") or compose.DEFAULT_BODY,
        personalize=1 if payload.get("personalize", True) else 0,
        cv_path=payload.get("cv_path"),
        daily_cap=payload.get("daily_cap"),
    )
    for r in rows:
        r["status"] = "en_attente"   # rédigée et programmée plus tard, par lots
        r["token"] = secrets.token_urlsafe(12)
    db.add_applications(campaign_id, rows)
    db.add_event("creation", f"{len(rows)} candidature(s) préparée(s)", campaign_id=campaign_id)
    return campaign_id


def _reschedule_pending(campaign: dict) -> int:
    """Redistribue les envois restants à partir de maintenant."""
    pending = db.pending_prepared(campaign["id"])
    times = schedule_times(len(pending), daily_cap=campaign.get("daily_cap") or DAILY_CAP,
                           start=datetime.now() + timedelta(minutes=review_minutes()))
    for row, when in zip(pending, times):
        db.update_application(row["id"], scheduled_at=when)
    return len(pending)


def launch(campaign_id: int) -> dict:
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise KeyError(campaign_id)
    n = _reschedule_pending(campaign)
    db.update_campaign(campaign_id, status="active", launched_at=_now())
    queued = db.application_status_counts(campaign_id).get("en_attente", 0)
    db.add_event("lancement", f"{queued + n} candidature(s) en file, rédigées par lots quotidiens",
                 campaign_id=campaign_id)
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
    # Les relances comptent comme des envois : même boîte, même réputation.
    month_sent = db.sent_since(today.strftime("%Y-%m")) + db.count_events("relance", today.strftime("%Y-%m"))
    day_sent = db.sent_since(today.strftime("%Y-%m-%d")) + db.count_events("relance", today.strftime("%Y-%m-%d"))
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


# ------------------------------------------------------------ relecture ---

def review_mode() -> str:
    """« manuel » : chaque mail attend ton accord ; « auto » : il part à son créneau sauf blocage."""
    return "auto" if db.get_setting("review_mode", "manuel") == "auto" else "manuel"


def review_minutes() -> int:
    try:
        return max(5, int(db.get_setting("review_minutes", str(REVIEW_MINUTES))))
    except ValueError:
        return REVIEW_MINUTES


def batch_needed(cap: int, sent_today: int, pending: int, after_hours: bool) -> int:
    """Combien de mails rédiger maintenant pour que le lot du jour soit complet.

    Après la fenêtre d'envoi, on prépare le lot du lendemain : ce qui est parti
    aujourd'hui ne compte plus.
    """
    used = pending + (0 if after_hours else sent_today)
    return max(0, cap - used)


async def prepare_campaign(campaign_id: int) -> int:
    """Rédige à l'avance le prochain lot d'une campagne active et lui donne ses créneaux.

    Un lot = le plafond quotidien. Les mails rédigés attendent ton accord (mode
    manuel) ou partent à leur créneau sauf blocage (mode auto), jamais avant le
    délai de relecture.
    """
    campaign = db.get_campaign(campaign_id)
    if campaign is None or campaign["status"] != "active":
        return 0
    cap = campaign.get("daily_cap") or DAILY_CAP
    local_now = datetime.now().astimezone()
    after_hours = local_now.hour >= SEND_WINDOW[1]
    sent_today = db.sent_since(datetime.now(timezone.utc).strftime("%Y-%m-%d"), campaign_id)
    pending = db.pending_prepared(campaign_id)
    needed = batch_needed(cap, sent_today, len(pending), after_hours)
    if needed <= 0:
        return 0
    queue = db.queued_applications(campaign_id, needed)
    if not queue:
        return 0

    # Créneaux : après le dernier prévu, et jamais avant le délai de relecture.
    start = datetime.now() + timedelta(minutes=review_minutes())
    if pending and pending[-1].get("scheduled_at"):
        last = datetime.fromisoformat(pending[-1]["scheduled_at"]).astimezone().replace(tzinfo=None)
        start = max(start, last + timedelta(seconds=SEND_INTERVAL))
    times = schedule_times(len(queue), daily_cap=cap, start=start)
    settings = db.all_settings()
    status = "a_valider" if review_mode() == "manuel" else "programme"
    done = 0
    for row, when in zip(queue, times):
        current = db.get_application(row["id"])
        if current is None or current["status"] != "en_attente":   # bloquée entre-temps
            continue
        if current.get("body_text"):   # déjà rédigé (ou retouché) depuis l'interface : on garde
            db.update_application(row["id"], scheduled_at=when, status=status, prepared_at=_now())
            done += 1
            continue
        try:
            rendered = await compose.compose(row, campaign, settings,
                                             personalize=bool(campaign.get("personalize")))
        except Exception as exc:
            log.warning("rédaction impossible pour %s : %s", row.get("email"), exc)
            continue
        db.update_application(row["id"], subject=rendered["subject"], body_text=rendered["body_text"],
                              body_html=rendered["body_html"], hook=rendered["hook"],
                              company_brief=rendered.get("brief"), scheduled_at=when,
                              status=status, prepared_at=_now())
        db.set_company_brief(row.get("company_siren"), rendered.get("brief"))
        done += 1
    if done:
        db.add_event("preparation",
                     f"{done} mail(s) rédigé(s), " + ("à relire avant départ" if status == "a_valider"
                                                       else "programmés (bloque ce que tu ne veux pas)"),
                     campaign_id=campaign_id)
    return done


async def prepare_loop() -> None:
    log.info("boucle de préparation démarrée")
    while True:
        try:
            for campaign in db.list_campaigns():
                if campaign["status"] == "active":
                    await prepare_campaign(campaign["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("boucle de préparation : erreur ignorée")
        await asyncio.sleep(PREPARE_SECONDS)


def validate(application_id: int) -> dict:
    """Donne le feu vert : le mail partira à son créneau (tout de suite s'il est passé)."""
    row = db.get_application(application_id)
    if row is None:
        raise KeyError(application_id)
    if row["status"] != "a_valider":
        raise ValueError("Cette candidature n'attend pas de validation")
    when = row.get("scheduled_at") or _now()
    db.update_application(application_id, status="programme", validated_at=_now(), scheduled_at=when)
    return db.get_application(application_id)


def validate_all(campaign_id: int) -> int:
    rows = [r for r in db.pending_prepared(campaign_id) if r["status"] == "a_valider"]
    for row in rows:
        validate(row["id"])
    if rows:
        db.add_event("validation", f"{len(rows)} mail(s) validé(s)", campaign_id=campaign_id)
    return len(rows)


def block(application_id: int) -> dict:
    """Bloque un envoi tant qu'il n'est pas parti."""
    row = db.get_application(application_id)
    if row is None:
        raise KeyError(application_id)
    if row["status"] not in db.PENDING_STATUSES:
        raise ValueError("Seul un envoi à venir peut être bloqué")
    db.update_application(application_id, status="annule")
    db.add_event("blocage", f"{row.get('company_name')} : envoi bloqué", campaign_id=row["campaign_id"],
                 application_id=application_id)
    _close_if_done(row["campaign_id"])
    return db.get_application(application_id)


def edit(application_id: int, subject: str | None, body_text: str | None) -> dict:
    """Retouche un mail avant son départ ; la version HTML est régénérée."""
    row = db.get_application(application_id)
    if row is None:
        raise KeyError(application_id)
    if row["status"] not in db.PENDING_STATUSES:
        raise ValueError("Ce mail est déjà parti")
    changes: dict = {}
    if subject is not None:
        changes["subject"] = subject.strip()
    if body_text is not None:
        changes["body_text"] = body_text.strip()
        changes["body_html"] = compose.to_html(changes["body_text"], compose.pixel_url(row.get("token")))
    if changes:
        db.update_application(application_id, **changes)
    return db.get_application(application_id)


def _close_if_done(campaign_id: int) -> None:
    campaign = db.get_campaign(campaign_id)
    if campaign and campaign["status"] == "active" and campaign["scheduled"] == 0:
        db.update_campaign(campaign_id, status="terminee")
        detail = "tous les envois sont partis" if campaign.get("sent") else "plus rien à envoyer (tout a été bloqué)"
        db.add_event("fin", detail, campaign_id=campaign_id)


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
    """Lit les réponses reçues dans les fils des mails envoyés et les classe.

    Une réponse automatique d'absence ne compte pas comme une réponse : la
    candidature reste « envoyée » et pourra être relancée.
    """
    if dry_run() or not gmail.is_connected():
        return 0
    own = gmail.connected_email() or ""
    found = 0
    for application in db.pending_reply_checks():
        try:
            replies = await gmail.thread_replies(application["gmail_thread_id"], own)
        except Exception as exc:
            log.debug("lecture des réponses impossible : %s", exc)
            continue
        if not replies:
            await asyncio.sleep(0.3)
            continue
        latest = replies[-1]
        if latest.get("id") and latest["id"] == application.get("last_reply_id"):
            continue   # déjà vue (réponse automatique déjà classée)
        substantive = [r for r in replies if not r.get("auto")]
        target = substantive[-1] if substantive else latest
        text = compose.strip_quoted(target.get("text") or target.get("snippet") or "")
        kind, summary = await compose.classify_reply(text)
        if not substantive or kind == "absence":
            db.update_application(application["id"], reply_kind="absence", reply_excerpt=text[:400],
                                  last_reply_id=latest.get("id"))
            await asyncio.sleep(0.3)
            continue
        db.update_application(application["id"], status="repondu", replied_at=_now(), reply_kind=kind,
                              reply_excerpt=(summary or text)[:400], last_reply_id=latest.get("id"))
        db.add_event("reponse", f"{application.get('company_name') or application['email']} — "
                     f"{compose.KIND_LABELS.get(kind, kind)}" + (f" : {summary}" if summary else ""),
                     campaign_id=application["campaign_id"], application_id=application["id"])
        found += 1
        await asyncio.sleep(0.3)
    return found


# ---------------------------------------------------------------- relances ---

FOLLOWUP_AFTER_DAYS = 7
FOLLOWUP_AFTER_OPEN_DAYS = 5
MAX_FOLLOWUPS = 1


def followup_candidates() -> list[dict]:
    return db.followup_candidates(FOLLOWUP_AFTER_DAYS, FOLLOWUP_AFTER_OPEN_DAYS, MAX_FOLLOWUPS)


async def prepare_followup(application_id: int) -> dict:
    """Rédige la relance sans l'envoyer : à relire, retoucher, puis valider."""
    application = db.get_application(application_id)
    if application is None:
        raise KeyError(application_id)
    campaign = db.get_campaign(application["campaign_id"]) or {}
    subject, body = await compose.followup_text(application, campaign, db.all_settings())
    return {"application_id": application_id, "to": application["email"], "subject": subject,
            "body_text": body, "company_name": application.get("company_name")}


async def send_followup(application_id: int, subject: str | None = None,
                        body_text: str | None = None) -> dict:
    """Envoie la relance dans le fil Gmail d'origine, sous les mêmes quotas."""
    application = db.get_application(application_id)
    if application is None:
        raise KeyError(application_id)
    if application["status"] not in ("envoye", "ouvert"):
        raise ValueError("cette candidature a déjà reçu une réponse ou n'est pas partie")
    if (application.get("followups") or 0) >= MAX_FOLLOWUPS:
        raise ValueError("relance déjà envoyée : on n'insiste pas deux fois")
    if not quota_state()["can_send"]:
        raise ValueError(f"quota : {quota_state()['reason']}")
    if not subject or not body_text:
        prepared = await prepare_followup(application_id)
        subject = subject or prepared["subject"]
        body_text = body_text or prepared["body_text"]
    settings = db.all_settings()

    if dry_run():
        db.update_application(application_id, followups=(application.get("followups") or 0) + 1,
                              last_followup_at=_now())
        db.add_event("relance", f"{application.get('company_name')} (simulation)",
                     campaign_id=application["campaign_id"], application_id=application_id)
        return {"ok": True, "simulation": True}

    if not gmail.is_connected():
        raise ValueError("Gmail n'est pas connecté")
    in_reply_to = await gmail.message_rfc_id(application["gmail_message_id"]) \
        if application.get("gmail_message_id") not in (None, "simulation") else None
    message = gmail.build_message(
        sender=gmail.connected_email() or "", sender_name=settings.get("sender_name") or "",
        to=application["email"], subject=subject, text=body_text,
        html=compose.to_html(body_text), in_reply_to=in_reply_to)
    await gmail.send(message, thread_id=application.get("gmail_thread_id") or None)
    db.update_application(application_id, followups=(application.get("followups") or 0) + 1,
                          last_followup_at=_now())
    db.add_event("relance", application.get("company_name") or application["email"],
                 campaign_id=application["campaign_id"], application_id=application_id)
    return {"ok": True, "simulation": False}


def outcomes() -> dict:
    """Tout ce que le tableau de bord affiche, en une réponse."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "totals": db.outcome_totals(),
        "month": db.outcome_totals(month),
        "by_campaign": db.outcomes_by_campaign(),
        "days": db.sends_by_day(14),
        "replies": db.recent_replies(20),
        "followups": followup_candidates(),
        "quota": quota_state(),
        "gmail": {"connected": gmail.is_connected(), "needs_reconnect": gmail.needs_reconnect(),
                  "missing_scopes": gmail.missing_scopes(), "email": gmail.connected_email()},
        "rules": {"after_days": FOLLOWUP_AFTER_DAYS, "after_open_days": FOLLOWUP_AFTER_OPEN_DAYS,
                  "max_followups": MAX_FOLLOWUPS},
    }


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
                 min_headcount: int | None, max_headcount: int | None,
                 job_title: str = "") -> list[dict]:
    """Nombre d'entreprises par secteur pour une cible : l'« analyse du marché ».

    Chaque ligne dit aussi si le secteur est pertinent pour le poste visé.
    """
    wanted = relevant_sectors(job_title)
    min_headcount, max_headcount = apply_floor(min_headcount, max_headcount, job_title)
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
            return {"key": key, "label": value["label"], "count": total,
                    "relevant": wanted is None or key in wanted}

        rows = await asyncio.gather(*(one(k, v) for k, v in SECTORS.items()))
    return sorted(rows, key=lambda r: -r["count"])
