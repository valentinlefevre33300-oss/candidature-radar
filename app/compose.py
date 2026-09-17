"""Rédaction des mails : un squelette commun, deux paragraphes propres à chaque envoi.

Le gabarit contient des variables entre accolades, toutes remplies depuis ce que
l'on sait du contact et de l'entreprise. Deux d'entre elles sont rédigées pour
chaque destinataire :

  - `{ouverture}` : pourquoi on écrit à CETTE personne. À un CPO, on dit qu'on
    veut rejoindre son équipe en tant que product manager ; à un dirigeant,
    qu'on veut rejoindre l'entreprise et qu'il saura orienter ; aux RH, la
    candidature classique. Des règles y pourvoient sans clé API ; Claude affine.
  - `{accroche}` : ce qu'on a compris de l'entreprise et de ses enjeux, relié à
    ce que le candidat apporte. Nourrie par une fiche « enjeux » rédigée une
    fois par entreprise à partir du texte de son site.

Une règle non négociable pour la rédaction automatique : n'affirmer que ce que
les données fournies permettent d'affirmer. Un recruteur repère une flatterie
inventée en une ligne, et c'est le mail entier qui tombe.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
from pathlib import Path

import anthropic

from .config import ANTHROPIC_API_KEY, CLAUDE_MODEL, PUBLIC_URL
from .naf import label_for_code

log = logging.getLogger(__name__)

VARIABLES: dict[str, str] = {
    "salutation": "« Bonjour Prénom Nom, » — ou « Bonjour, » si le nom est inconnu",
    "ouverture": "pourquoi on écrit à cette personne, selon sa fonction (rédigée pour chaque envoi)",
    "accroche": "ce qu'on a compris de l'entreprise, relié à ce qu'on apporte (rédigée pour chaque envoi)",
    "prenom": "prénom du contact",
    "nom": "nom du contact",
    "entreprise": "nom de l'entreprise, nettoyé",
    "poste": "poste visé",
    "ville": "ville de l'entreprise",
    "secteur": "secteur d'activité",
    "taille": "tranche d'effectif",
    "moi": "ton nom",
    "signature": "ta signature (réglages)",
}

DEFAULT_SUBJECT = "Candidature spontanée — {poste} — {moi}"
DEFAULT_BODY = """{salutation}

{ouverture}

{accroche}

Vous trouverez mon CV en pièce jointe. Je reste à votre entière disposition pour en échanger.

Bien cordialement,
{signature}"""

LEGAL_NOISE = {"sas", "sasu", "sarl", "eurl", "sa", "sci", "snc", "scop", "groupe", "group",
               "societe", "société", "ste", "holding", "cie", "compagnie", "ets", "etablissements"}


# ------------------------------------------------------------- variables ---

def pretty_company(raw: str | None) -> str:
    """« CEGEDIM (HOSPITALIS) » -> « Cegedim », « TIBCO SAS » -> « Tibco »."""
    if not raw:
        return ""
    cleaned = re.sub(r"\(.*?\)", " ", raw)
    words = [w for w in re.split(r"\s+", cleaned.strip()) if w and w.lower() not in LEGAL_NOISE]
    pretty = []
    for word in words:
        # Un sigle court reste en capitales ; le reste passe en capitale initiale.
        pretty.append(word if (word.isupper() and len(word) <= 4) else word.capitalize())
    return " ".join(pretty) or raw.strip()


def build_context(application: dict, campaign: dict, settings: dict) -> dict[str, str]:
    """Toutes les variables du gabarit, sauf celles qui sont rédigées à part."""
    first = (application.get("first_name") or "").strip()
    last = (application.get("last_name") or "").strip()
    name = " ".join(part for part in (first, last) if part)
    me = settings.get("sender_name") or ""
    return {
        "salutation": f"Bonjour {name}," if name else "Bonjour,",
        "prenom": first,
        "nom": last,
        "entreprise": pretty_company(application.get("company_name")),
        "poste": campaign.get("job_title") or "",
        "ville": (application.get("company_city") or "").title(),
        "secteur": label_for_code(application.get("company_naf")) or "",
        "taille": application.get("company_size") or "",
        "moi": me,
        "signature": settings.get("signature") or me,
    }


def render(template: str, context: dict[str, str]) -> str:
    """Remplace les `{variables}` ; une variable inconnue devient vide."""
    text = re.sub(r"\{(\w+)\}", lambda m: context.get(m.group(1), ""), template or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ------------------------------------------------------------- ouverture ---

def _is_small(size_label: str | None) -> bool:
    match = re.match(r"\s*(\d[\d\s]*)", size_label or "")
    return bool(match) and int(match.group(1).replace(" ", "")) < 50


def recipient_kind(application: dict) -> str:
    """Étiquette lisible de la relation au poste visé, pour les règles et le prompt."""
    category = application.get("category")
    if category == "metier":
        return "dirige le service du métier visé" if application.get("is_manager") else "exerce le métier visé"
    if category == "direction":
        return "dirige l'entreprise"
    if category == "rh":
        return "ressources humaines"
    return "fonction inconnue"


def opening_for(application: dict, context: dict[str, str]) -> str:
    """Phrase d'ouverture adaptée à la fonction du destinataire, par règles."""
    poste = context.get("poste") or "ce poste"
    company = context.get("entreprise") or "votre entreprise"
    category = application.get("category")
    if category == "metier" and application.get("is_manager"):
        return (f"Je vous écris directement : c'est votre équipe que j'aimerais rejoindre, "
                f"en tant que {poste}.")
    if category == "metier":
        return (f"Je vous écris parce que vous exercez le métier que je vise : j'aimerais rejoindre "
                f"votre équipe en tant que {poste}.")
    if category == "direction":
        if _is_small(application.get("company_size")):
            return (f"Je vous écris directement, à la tête de {company} : j'aimerais la rejoindre "
                    f"en tant que {poste}.")
        return (f"Je me permets de vous écrire directement : j'aimerais rejoindre {company} en tant "
                f"que {poste}, et vous êtes la personne la mieux placée pour orienter ma candidature "
                f"vers la bonne équipe.")
    return (f"Je me permets de vous adresser ma candidature spontanée pour un poste de {poste} "
            f"au sein de {company}.")


# ------------------------------------------------------------- accroche ---

def degraded_hook(context: dict[str, str]) -> str:
    """Paragraphe assemblé par règles, sans modèle. Sobre, jamais faux."""
    where = f" à {context['ville']}" if context.get("ville") else ""
    sector = context.get("secteur")
    size = context.get("taille")
    role = context.get("poste") or "ce poste"
    if sector and size:
        return (f"Votre activité dans le domaine « {sector.lower()} »{where} et la taille de votre "
                f"structure ({size}) correspondent à ce que je recherche : un environnement où un "
                f"{role} prend rapidement des responsabilités concrètes.")
    if sector:
        return (f"Votre activité dans le domaine « {sector.lower()} »{where} correspond à ce que je "
                f"recherche pour un poste de {role}, au sein d'une équipe où je pourrai contribuer "
                f"rapidement.")
    return (f"Votre entreprise{where} correspond au type de structure que je vise pour un poste de "
            f"{role} : une équipe à taille humaine où je pourrai contribuer rapidement.")


SYSTEM_BRIEF = """Tu prépares une candidature spontanée : à partir du texte du site d'une entreprise et de quelques données publiques, tu résumes ce qu'elle fait et quels sont ses enjeux probables.

Réponds en français, en trois lignes, 100 mots au plus, exactement sous cette forme :
Activité : <ce qu'elle fait, pour qui, comment>
Enjeux probables : <deux ou trois enjeux plausibles au vu de son activité, de sa taille et de son marché — formulés comme des hypothèses prudentes>
Pour un <poste> : <en quoi ce poste peut compter chez elle>

Uniquement d'après les éléments fournis. Si le site dit peu de choses, écris « Peu d'informations » et reste général. Pas de superlatifs, pas de jugement de valeur."""

SYSTEM_PERSONALIZE = """Tu rédiges deux courts paragraphes d'une candidature spontanée, adressée à une personne précise dans une entreprise précise. Ils s'insèrent dans un mail dont la salutation, la mention du CV, la formule finale et la signature existent déjà.

OUVERTURE (une à deux phrases, 35 mots au plus) : pourquoi le candidat écrit à CETTE personne, d'après sa fonction, et ce qu'il veut. À quelqu'un qui dirige le service du métier visé (CTO, CPO, head of, responsable…) : c'est son équipe qu'il veut rejoindre, en tant que <poste visé>. À quelqu'un qui dirige l'entreprise : il veut rejoindre l'entreprise, et cette personne saura orienter sa candidature. À un pair du métier : il vise le même métier et son équipe. Aux ressources humaines : une candidature spontanée classique pour le poste.

ACCROCHE (deux à trois phrases, 70 mots au plus) : montre que le candidat a compris ce que fait l'entreprise et ses enjeux probables — d'après la fiche fournie — et relie-les à ce qu'il apporte, d'après son profil. Parle de ce que fait l'entreprise (« votre activité de… », « vos outils pour… »), sans la renommer.

Règles : première personne, français, vouvoiement. UNIQUEMENT les faits fournis : aucun chiffre, produit, client ou actualité non cité. Sobre : pas de superlatifs (« leader », « incontournable », « passionnant »), pas de flatterie, pas de « je m'adresse à ». Le nom de l'entreprise et le poste n'apparaissent qu'une fois au total, dans l'ouverture. Pas de salutation, pas de formule finale, pas de guillemets, pas de puces.

Réponds exactement sous la forme :
OUVERTURE: <texte>
ACCROCHE: <texte>"""


def _company_facts(application: dict, context: dict[str, str], *, with_site: bool) -> list[str]:
    lines = [f"Entreprise : {context.get('entreprise') or '?'}"]
    if context.get("secteur"):
        lines.append(f"Activité déclarée : {context['secteur']}"
                     + (f" (NAF {application.get('company_naf')})" if application.get("company_naf") else ""))
    if context.get("ville"):
        lines.append(f"Ville : {context['ville']}")
    if context.get("taille"):
        lines.append(f"Effectif : {context['taille']}")
    if application.get("company_tagline"):
        lines.append(f"Titre et description du site : {application['company_tagline']}")
    if with_site and application.get("company_about"):
        lines.append(f"Texte du site :\n{str(application['company_about'])[:2000]}")
    return lines


def _brief_prompt(application: dict, context: dict[str, str]) -> str:
    lines = [f"Poste visé par le candidat : {context.get('poste') or '?'}"]
    lines += _company_facts(application, context, with_site=True)
    return "\n".join(lines) + "\n\nRédige la fiche."


def _parts_prompt(context: dict[str, str], application: dict, profile: str, brief: str | None) -> str:
    who = " ".join(p for p in (application.get("first_name"), application.get("last_name")) if p)
    lines = [f"Poste visé : {context.get('poste') or '?'}",
             f"Destinataire : {who or 'inconnu'} — {application.get('role_title') or 'fonction inconnue'} "
             f"({recipient_kind(application)})"]
    lines += _company_facts(application, context, with_site=not brief)
    if brief:
        lines.append(f"Fiche enjeux :\n{brief}")
    lines += ["", "Profil du candidat :",
              profile.strip() or "(non renseigné — reste général sur ses motivations)"]
    return "\n".join(lines) + "\n\nRédige les deux paragraphes."


_PARTS_RE = re.compile(r"OUVERTURE\s*:\s*(.+?)\s*ACCROCHE\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)


def _clean(text: str) -> str:
    text = text.strip().strip('"«» \n')
    return re.sub(r"\s+", " ", text)


def _split_parts(text: str) -> tuple[str | None, str]:
    """(ouverture, accroche) depuis la réponse étiquetée ; sans étiquettes, tout est accroche."""
    match = _PARTS_RE.search(text or "")
    if not match:
        return None, _clean(text)
    return _clean(match.group(1)), _clean(match.group(2))


_client: anthropic.AsyncAnthropic | None = None


def claude_available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=45.0)
    return _client


async def _ask(system: str, user: str, max_tokens: int) -> str:
    """Un appel court, prompt système en cache, repli serveur en cas de refus."""
    response = await _get_client().beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("le modèle a refusé la demande")
    return " ".join(block.text for block in response.content if block.type == "text").strip()


async def claude_brief(application: dict, context: dict[str, str]) -> str:
    """Fiche « enjeux » d'une entreprise. Une par entreprise, réutilisée ensuite."""
    text = await _ask(SYSTEM_BRIEF, _brief_prompt(application, context), 500)
    text = re.sub(r"[ \t]+", " ", text).strip().strip('"«»')
    if len(text) < 40:
        raise RuntimeError("fiche trop courte")
    return text[:900]


async def claude_parts(context: dict[str, str], application: dict, profile: str,
                       brief: str | None) -> tuple[str | None, str]:
    """(ouverture, accroche) rédigées pour ce destinataire."""
    opening, hook = _split_parts(await _ask(SYSTEM_PERSONALIZE,
                                            _parts_prompt(context, application, profile, brief), 600))
    if len(hook) < 40:
        raise RuntimeError("réponse trop courte")
    return opening, hook


# --------------------------------------------------------------- réponses ---

KIND_LABELS = {"refus": "Refus", "interet": "Intérêt", "question": "Question",
               "absence": "Absence", "autre": "Autre"}

_QUOTE_START = re.compile(
    r"^(?:>|Le .{3,120}a (?:écrit|ecrit) ?:|On .{3,120}wrote ?:|-{2,}\s*(?:Original|Message d'origine|Forwarded)"
    r"|De ?:\s|From ?:\s|Envoyé ?:\s|Sent ?:\s|_{5,}|\*{5,})", re.IGNORECASE)


def strip_quoted(text: str, limit: int = 600) -> str:
    """Le texte propre d'une réponse : sans les lignes citées du mail d'origine."""
    kept: list[str] = []
    for line in (text or "").replace("\r", "").split("\n"):
        if _QUOTE_START.match(line.strip()):
            break
        kept.append(line.rstrip())
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return cleaned[:limit]


_ABSENCE = ("absent", "absence", "out of office", "conge", "de retour le", "back on", "auto-reply",
            "reponse automatique", "automatic reply", "je suis actuellement en")
_REFUS = ("malheureusement", "ne donnerons pas suite", "ne pouvons pas donner suite", "pas de poste",
          "aucun poste", "pas d'opportunit", "pas de besoin", "pas de recrutement", "ne recrutons pas",
          "n'a pas ete retenue", "ne correspond pas", "ne pourrons pas", "pas en mesure", "not hiring",
          "unfortunately", "not a fit", "no open position", "regret", "pas de place", "n'avons pas")
_INTERET = ("entretien", "echanger", "echange", "appel", "visio", "disponibilit", "rencontrer",
            "rendez-vous", "interesse", "interessant", "call", "interview", "discuter", "creneau",
            "avec plaisir", "volontiers")


def classify_reply_rules(text: str) -> str:
    """Classement par règles : absence > refus > intérêt > question > autre.

    Le refus passe avant l'intérêt : un refus poli propose souvent
    « d'échanger à l'avenir », ce qui n'en fait pas une bonne nouvelle.
    """
    from .domains import _norm
    t = _norm(text)
    if any(k in t for k in _ABSENCE):
        return "absence"
    if any(k in t for k in _REFUS):
        return "refus"
    if any(k in t for k in _INTERET):
        return "interet"
    if "?" in t:
        return "question"
    return "autre"


SYSTEM_CLASSIFY = """Tu lis la réponse d'une entreprise à une candidature spontanée et tu la classes.

- refus : pas de suite, pas de poste, candidature non retenue (même formulé poliment).
- interet : proposition d'échange ou d'entretien, demande de disponibilités, curiosité manifeste.
- question : demande d'informations (CV, prétentions, disponibilité, précisions) sans décision.
- absence : réponse automatique d'absence ou de congé.
- autre : accusé de réception, transfert à un collègue, hors sujet.

Réponds exactement sous la forme :
CATEGORIE: <refus|interet|question|absence|autre>
RESUME: <une phrase factuelle, 20 mots au plus, sans interprétation>"""


async def classify_reply(text: str) -> tuple[str, str]:
    """(catégorie, résumé) d'une réponse. Règles seules sans clé API."""
    fallback = classify_reply_rules(text)
    if not claude_available() or not text.strip():
        return fallback, ""
    try:
        out = await _ask(SYSTEM_CLASSIFY, "Réponse reçue :\n" + strip_quoted(text, 1500) + "\n\nClasse-la.", 150)
    except Exception as exc:
        log.warning("classement de la réponse indisponible : %s", exc)
        return fallback, ""
    kind = re.search(r"CATEGORIE\s*:\s*(refus|interet|question|absence|autre)", out, re.I)
    summary = re.search(r"RESUME\s*:\s*(.+)", out, re.I)
    return (kind.group(1).lower() if kind else fallback), (_clean(summary.group(1)) if summary else "")


# ---------------------------------------------------------------- relance ---

FOLLOWUP_BODY = """{salutation}

Je me permets de revenir vers vous au sujet de ma candidature spontanée pour un poste de {poste}, envoyée le {date}. Je reste pleinement disponible pour en échanger, par téléphone ou en visio si c'est plus simple pour vous.

Bien cordialement,
{signature}"""

SYSTEM_FOLLOWUP = """Tu rédiges une relance courte après une candidature spontanée restée sans réponse : trois phrases au plus, 60 mots au plus, première personne, vouvoiement, ton sobre — ni reproche, ni insistance, ni excuse. Rappelle en une phrase le poste visé et, si une fiche enjeux est fournie, un élément concret de ce que fait l'entreprise ; propose un échange court. Uniquement les faits fournis. Pas de salutation ni de formule finale : elles existent déjà. Renvoie le paragraphe seul."""


def _fmt_day(iso: str | None) -> str:
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m/%Y")
    except Exception:
        return "récemment"


async def followup_text(application: dict, campaign: dict, settings: dict) -> tuple[str, str]:
    """(objet, corps) d'une relance, dans le fil du mail d'origine."""
    context = build_context(application, campaign, settings)
    context["date"] = _fmt_day(application.get("sent_at"))
    body = render(FOLLOWUP_BODY, context)
    if claude_available():
        who = " ".join(p for p in (application.get("first_name"), application.get("last_name")) if p)
        facts = [f"Poste visé : {context['poste']}", f"Entreprise : {context['entreprise']}",
                 f"Destinataire : {who or 'inconnu'} — {application.get('role_title') or 'fonction inconnue'}",
                 f"Mail envoyé le : {context['date']}",
                 f"Le mail a été ouvert : {'oui' if application.get('opens') else 'non'}"]
        if application.get("company_brief"):
            facts.append(f"Fiche enjeux :\n{application['company_brief']}")
        facts += ["", "Profil du candidat :", profile_text(settings).strip() or "(non renseigné)"]
        try:
            paragraph = _clean(await _ask(SYSTEM_FOLLOWUP, "\n".join(facts) + "\n\nRédige la relance.", 300))
            if len(paragraph) > 40:
                body = render("{salutation}\n\n{relance}\n\nBien cordialement,\n{signature}",
                              {**context, "relance": paragraph})
        except Exception as exc:
            log.warning("relance Claude indisponible : %s", exc)
    original = application.get("subject") or render(campaign.get("subject_tpl") or DEFAULT_SUBJECT, context)
    subject = original if original.lower().startswith("re:") else f"Re: {original}"
    return subject, body


# ----------------------------------------------------------------- profil ---

def cv_text(path: str | None, limit: int = 2500) -> str:
    """Texte brut du CV (PDF), tronqué, pour nourrir la rédaction."""
    if not path or not Path(path).exists():
        return ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        chunks = [page.extract_text() or "" for page in reader.pages[:3]]
        text = re.sub(r"[ \t]+", " ", "\n".join(chunks)).strip()
        return text[:limit]
    except Exception as exc:  # PDF illisible, chiffré, scanné…
        log.debug("lecture du CV impossible : %s", exc)
        return ""


def profile_text(settings: dict) -> str:
    """Le résumé saisi dans les réglages prime ; à défaut, le texte du CV."""
    return settings.get("profile_summary") or cv_text(settings.get("cv_path"))


# ------------------------------------------------------------------- HTML ---

def to_html(text: str, pixel_url: str | None = None) -> str:
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    body = "".join(
        "<p style=\"margin:0 0 14px\">" + html_lib.escape(p.strip()).replace("\n", "<br>") + "</p>"
        for p in paragraphs)
    pixel = (f'<img src="{html_lib.escape(pixel_url)}" width="1" height="1" alt="" '
             f'style="display:block;width:1px;height:1px;border:0">' if pixel_url else "")
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;'
            f'color:#1a1a1a">{body}</div>{pixel}')


def pixel_url(token: str | None) -> str | None:
    return f"{PUBLIC_URL}/t/{token}.gif" if (PUBLIC_URL and token) else None


# ---------------------------------------------------------------- compose ---

async def compose(application: dict, campaign: dict, settings: dict, *,
                  personalize: bool = True) -> dict:
    """Rend le mail complet pour une candidature.

    Renvoie subject, body_text, body_html, opening, hook, brief et `engine`
    (« claude » ou « regles ») pour que l'interface dise d'où vient le texte.
    """
    context = build_context(application, campaign, settings)
    context["ouverture"] = opening_for(application, context)
    hook = degraded_hook(context)
    brief = application.get("company_brief") or None
    engine = "regles"

    if personalize and claude_available():
        if not brief:
            try:
                brief = await claude_brief(application, context)
            except Exception as exc:  # on rédige quand même, sans la fiche
                log.warning("fiche enjeux indisponible pour %s : %s",
                            application.get("company_name"), exc)
        try:
            opening, hook = await claude_parts(context, application, profile_text(settings), brief)
            if opening:
                context["ouverture"] = opening
            engine = "claude"
        except Exception as exc:  # quota, réseau, refus… le mail doit partir quand même
            log.warning("rédaction Claude indisponible pour %s : %s",
                        application.get("company_name"), exc)

    context["accroche"] = hook
    subject = render(campaign.get("subject_tpl") or DEFAULT_SUBJECT, context)
    body_text = render(campaign.get("body_tpl") or DEFAULT_BODY, context)
    return {
        "subject": subject,
        "body_text": body_text,
        "body_html": to_html(body_text, pixel_url(application.get("token"))),
        "opening": context["ouverture"],
        "hook": hook,
        "brief": brief,
        "engine": engine,
    }
