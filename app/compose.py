"""Rédaction des mails : un squelette commun, un paragraphe propre à chaque entreprise.

Le gabarit contient des variables entre accolades, toutes remplies depuis ce que
l'on sait du contact et de l'entreprise — sauf `{accroche}` : ce paragraphe est
rédigé pour chaque entreprise par Claude, à partir de son activité, sa ville, sa
taille et la page d'accueil de son site. Sans clé API, ou si l'appel échoue, un
repli par règles prend la main : le mail part quand même, juste moins vivant.

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
    "prenom": "prénom du contact",
    "nom": "nom du contact",
    "entreprise": "nom de l'entreprise, nettoyé",
    "poste": "poste visé",
    "ville": "ville de l'entreprise",
    "secteur": "secteur d'activité",
    "taille": "tranche d'effectif",
    "accroche": "paragraphe rédigé pour cette entreprise",
    "moi": "ton nom",
    "signature": "ta signature (réglages)",
}

DEFAULT_SUBJECT = "Candidature spontanée — {poste} — {moi}"
DEFAULT_BODY = """{salutation}

Je me permets de vous adresser ma candidature spontanée pour un poste de {poste} au sein de {entreprise}.

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
    """Toutes les variables du gabarit, sauf `{accroche}` qui est rédigée à part."""
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


SYSTEM_PROMPT = """Tu rédiges, pour une candidature spontanée, le paragraphe qui explique pourquoi le candidat écrit à cette entreprise précisément. Il s'insère au milieu d'un mail déjà rédigé (salutation et formule de politesse sont ailleurs).

Règles :
- 2 à 3 phrases, 70 mots au plus, à la première personne, en français, tutoiement exclu.
- Appuie-toi UNIQUEMENT sur les faits fournis (activité, ville, taille, description du site, fonction de l'interlocuteur). N'invente ni chiffre, ni produit, ni actualité, ni valeur d'entreprise. Si les faits sont minces, parle du type de structure et du poste, pas de l'entreprise en détail.
- Ton sobre et direct : pas de superlatifs, pas de « leader », « incontournable », « passionnant », pas de flatterie.
- Fais le lien entre ce que fait l'entreprise et ce que le candidat apporte, d'après son profil.
- Pas de salutation, pas de formule finale, pas de guillemets, pas de puces, pas de titre. Renvoie le paragraphe seul."""


def _facts(context: dict[str, str], application: dict, profile: str) -> str:
    lines = [f"Poste visé : {context.get('poste') or '?'}",
             f"Entreprise : {context.get('entreprise') or '?'}"]
    if context.get("secteur"):
        lines.append(f"Activité : {context['secteur']}" + (f" (NAF {application.get('company_naf')})" if application.get("company_naf") else ""))
    if context.get("ville"):
        lines.append(f"Ville : {context['ville']}")
    if context.get("taille"):
        lines.append(f"Effectif : {context['taille']}")
    if application.get("company_tagline"):
        lines.append(f"Page d'accueil du site : {application['company_tagline']}")
    role = application.get("role_title")
    who = " ".join(p for p in (application.get("first_name"), application.get("last_name")) if p)
    if who or role:
        lines.append(f"Interlocuteur : {who or 'inconnu'}" + (f" — {role}" if role else ""))
    lines.append("")
    lines.append("Profil du candidat :")
    lines.append(profile.strip() or "(non renseigné — reste général sur ses motivations)")
    return "\n".join(lines)


_client: anthropic.AsyncAnthropic | None = None


def claude_available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=45.0)
    return _client


async def claude_hook(context: dict[str, str], application: dict, profile: str) -> str:
    """Demande le paragraphe à Claude. Lève une exception si rien d'exploitable ne revient."""
    client = _get_client()
    response = await client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=400,
        # Le prompt système est identique pour toutes les entreprises : il est
        # mis en cache, seul le bloc de faits change d'un mail à l'autre.
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _facts(context, application, profile)
                   + "\n\nRédige le paragraphe."}],
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("le modèle a refusé la demande")
    text = " ".join(block.text for block in response.content if block.type == "text").strip()
    text = text.strip('"«» \n')
    text = re.sub(r"\s+", " ", text)
    if len(text) < 40:
        raise RuntimeError("réponse trop courte")
    return text


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

    Renvoie subject, body_text, body_html, hook et `engine` (« claude » ou « regles »)
    pour que l'interface puisse dire d'où vient le paragraphe.
    """
    context = build_context(application, campaign, settings)
    engine = "regles"
    hook = degraded_hook(context)
    if personalize and claude_available():
        try:
            hook = await claude_hook(context, application, profile_text(settings))
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
        "hook": hook,
        "engine": engine,
    }
