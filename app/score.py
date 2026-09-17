"""Classement des contacts par pertinence pour une candidature spontanée.

Le but n'est pas de ramasser le plus d'adresses possible, mais de faire remonter
les quelques-unes qui valent un mail. L'ordre de préférence retenu :

  1. une personne identifiée aux ressources humaines ;
  2. un dirigeant, surtout dans une petite structure où il lit ses mails ;
  3. une personne identifiée dont le métier recoupe le poste visé ;
  4. la boîte de recrutement générique ;
  5. le contact général, en dernier recours.

Tout ce qui est manifestement hors sujet (facturation, support, RGPD) est
conservé mais relégué : on préfère expliquer pourquoi un contact est mal classé
plutôt que de le faire disparaître silencieusement.
"""
from __future__ import annotations

import re

from .extract.people import (
    SERVICE_PROVIDER_DOMAINS,
    classify_mailbox,
    normalize,
    strip_accents,
)
from .models import Company, Contact

# Note de départ selon la nature de la boîte mail.
BASE_SCORES: dict[str, float] = {
    "rh": 100.0,
    "direction": 80.0,
    "nominatif": 65.0,
    "generique": 40.0,
    "commercial": 18.0,
    "technique": 12.0,
    "juridique": 4.0,
    "inconnu": 30.0,
}

# Boîtes aux lettres à ne jamais remonter en tête, même bien notées par ailleurs.
HARD_DEMOTE = {"juridique", "technique"}


def _title_tokens(job_title: str) -> set[str]:
    """Mots significatifs du poste visé, pour détecter un métier voisin."""
    stop = {"de", "du", "des", "en", "la", "le", "les", "un", "une", "et", "a",
            "au", "aux", "pour", "chez", "senior", "junior", "confirme", "h", "f"}
    words = re.split(r"[^a-z0-9+#]+", strip_accents(job_title).lower())
    return {w for w in words if len(w) > 2 and w not in stop}


def score_contact(contact: Contact, company: Company, job_title: str) -> Contact:
    """Calcule la note et la catégorie finale d'un contact. Modifie et renvoie l'objet."""
    reasons: list[str] = []
    mailbox = classify_mailbox(contact.email)

    # La fonction lue dans la page prime sur la seule lecture de l'adresse :
    # « marie.dupont@ » ne dit rien, « Marie Dupont, DRH » dit tout.
    if contact.role_title and contact.category in ("rh", "direction", "technique"):
        category = contact.category
        reasons.append(f"fonction repérée dans la page : {contact.role_title[:60]}")
    elif contact.category in BASE_SCORES and contact.category != "inconnu":
        category = contact.category
    else:
        category = mailbox if mailbox in BASE_SCORES else "inconnu"

    score = BASE_SCORES[category]
    contact.category = category

    # -- pertinence de l'adresse elle-même ---------------------------------

    if contact.is_nominative:
        score += 15
        reasons.append("adresse nominative (on écrit à quelqu'un, pas à une boîte)")

    if contact.matched_director:
        score += 25
        reasons.append("correspond à un dirigeant déclaré au registre")

    # Un métier proche du poste visé = interlocuteur qui comprendra le profil.
    wanted = _title_tokens(job_title)
    if wanted and contact.role_title:
        role_words = _title_tokens(contact.role_title)
        overlap = wanted & role_words
        if overlap:
            score += 20
            reasons.append(f"métier proche du poste visé ({', '.join(sorted(overlap))})")

    # -- pénalités ---------------------------------------------------------

    domain = contact.domain
    company_domain = (company.domain or "").lower().removeprefix("www.")

    if company_domain and domain != company_domain and not domain.endswith("." + company_domain):
        # Typiquement l'agence web ou l'hébergeur cité dans les mentions légales.
        score -= 55
        reasons.append(f"domaine étranger à l'entreprise ({domain}) — probable prestataire")

    if domain in SERVICE_PROVIDER_DOMAINS or any(
        domain.endswith("." + d) for d in SERVICE_PROVIDER_DOMAINS
    ):
        score -= 30
        reasons.append("domaine d'hébergeur ou de plateforme")

    if contact.was_obfuscated:
        score -= 5
        reasons.append("adresse reconstituée depuis une forme masquée — à vérifier")

    if contact.inferred:
        # Une adresse déduite reste une hypothèse : elle ne doit jamais passer
        # devant une adresse réellement observée sur le site, même moins bien
        # placée hiérarchiquement. Un contact RH confirmé vaut mieux qu'un PDG
        # supposé. La pénalité doit donc compenser les bonus cumulés d'un
        # dirigeant (nominatif + dirigeant reconnu + petite structure).
        score -= 30
        reasons.append("adresse déduite du motif maison, jamais vue en ligne")

    if contact.mx_ok is True:
        score += 5

    if contact.smtp_ok is False:
        reasons.append("le serveur a rejeté cette adresse")

    if category in HARD_DEMOTE:
        reasons.append("boîte fonctionnelle sans rapport avec le recrutement")

    # -- bonus contextuels -------------------------------------------------

    # Dans une structure de moins de 50 personnes, le dirigeant lit ses mails
    # lui-même ; au-delà, la candidature finit dans un filtre.
    if category == "direction" and company.headcount_code in {"00", "01", "02", "03", "11", "12"}:
        score += 18
        reasons.append("petite structure : le dirigeant lit probablement ses mails")
    elif category == "direction" and company.headcount_code in {"41", "42", "51", "52", "53"}:
        score -= 20
        reasons.append("grande entreprise : écrire au dirigeant a peu de chances d'aboutir")

    # Une adresse qui ne peut pas recevoir de courrier ne vaut rien, quelle que
    # soit la qualite de son titulaire : un plafond dur vaut mieux qu'une penalite
    # que le score d'un profil RH finirait par absorber.
    if contact.mx_ok is False:
        score = min(score, 10.0)
        reasons.append("le domaine n'accepte pas d'email (aucun enregistrement MX)")
    if contact.smtp_ok is False:
        score = min(score, 8.0)

    contact.score = round(max(score, 0.0), 1)
    contact.reasons = reasons
    return contact


def dedupe_and_rank(contacts: list[Contact]) -> list[Contact]:
    """Fusionne les doublons d'adresse et trie par pertinence décroissante."""
    best: dict[str, Contact] = {}
    for contact in contacts:
        key = contact.email.lower()
        current = best.get(key)
        if current is None or contact.score > current.score:
            # On conserve les informations nominatives déjà collectées ailleurs.
            if current is not None:
                contact.first_name = contact.first_name or current.first_name
                contact.last_name = contact.last_name or current.last_name
                contact.role_title = contact.role_title or current.role_title
            best[key] = contact
    return sorted(best.values(), key=lambda c: (-c.score, c.email))


def matches_director(contact: Contact, company: Company) -> bool:
    """L'adresse correspond-elle à un dirigeant connu de l'annuaire ?"""
    local = normalize(contact.email.split("@", 1)[0])
    if len(local) < 4:
        return False
    for director in company.directors:
        last = normalize(director.last_name)
        first = normalize(director.first_names.split()[0] if director.first_names else "")
        if not last or len(last) < 3:
            continue
        if last in local and (not first or first[:1] in local or first in local):
            return True
    return False
