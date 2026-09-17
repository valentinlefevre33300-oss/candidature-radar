"""Classement des contacts par pertinence pour une candidature spontanée.

Le but n'est pas de ramasser le plus d'adresses possible, mais de faire remonter
les quelques-unes qui valent un mail. L'ordre de préférence retenu — parler aux
gens concernés, pas aux boîtes qui reçoivent tout le monde :

  1. la personne du métier visé, et d'abord celle qui dirige ce service : c'est
     elle qui veut un nouveau membre dans son équipe ;
  2. le dirigeant d'une petite structure, qui est de fait le manager ;
  3. les ressources humaines, qui reçoivent des centaines de candidatures ;
  4. la boîte de recrutement ou le contact générique, en dernier recours.

Tout ce qui est manifestement hors sujet (facturation, support, RGPD) est
conservé mais relégué : on préfère expliquer pourquoi un contact est mal classé
plutôt que de le faire disparaître silencieusement.
"""
from __future__ import annotations

from .extract.people import (
    SERVICE_PROVIDER_DOMAINS,
    classify_mailbox,
    normalize,
)
from .models import Company, Contact

# Note de départ selon la nature de l'interlocuteur.
BASE_SCORES: dict[str, float] = {
    "metier": 100.0,
    "direction": 85.0,
    "rh": 75.0,
    "nominatif": 60.0,
    "generique": 40.0,
    "commercial": 18.0,
    "technique": 12.0,
    "juridique": 4.0,
    "inconnu": 30.0,
}

# Boîtes aux lettres à ne jamais remonter en tête, même bien notées par ailleurs.
HARD_DEMOTE = {"juridique", "technique"}

SMALL_COMPANY = {"00", "01", "02", "03", "11", "12"}   # moins de 50 salariés
LARGE_COMPANY = {"41", "42", "51", "52", "53"}         # 500 et plus


def is_guessed(contact: Contact) -> bool:
    """Adresse construite sans qu'aucune adresse nominative n'ait été observée."""
    return bool(contact.pattern_used and contact.pattern_used.endswith("?"))


def score_contact(contact: Contact, company: Company, job_title: str) -> Contact:
    """Calcule la note et la catégorie finale d'un contact. Modifie et renvoie l'objet."""
    reasons: list[str] = []
    mailbox = classify_mailbox(contact.email)

    # La fonction lue dans la page prime sur la seule lecture de l'adresse :
    # « marie.dupont@ » ne dit rien, « Marie Dupont, DRH » dit tout.
    if contact.role_title and contact.category in ("metier", "rh", "direction"):
        category = contact.category
        reasons.append(f"fonction repérée : {contact.role_title[:60]}")
    elif contact.category in BASE_SCORES and contact.category != "inconnu":
        category = contact.category
    else:
        category = mailbox if mailbox in BASE_SCORES else "inconnu"

    score = BASE_SCORES[category]
    contact.category = category

    # -- pertinence de l'interlocuteur --------------------------------------

    if category == "metier":
        if contact.is_manager:
            score += 15
            reasons.append("dirige le service qui recrute pour ce poste")
        else:
            reasons.append("exerce le métier visé : un pair, qui sait ce que cherche l'équipe")

    if contact.is_nominative:
        score += 15
        reasons.append("adresse nominative (on écrit à quelqu'un, pas à une boîte)")

    if contact.matched_director:
        score += 25
        reasons.append("correspond à un dirigeant déclaré au registre")

    # Dans une structure de moins de 50 personnes, le dirigeant est le manager
    # et lit ses mails lui-même ; au-delà, la candidature finit dans un filtre.
    if category == "direction" and company.headcount_code in SMALL_COMPANY:
        score += 18
        reasons.append("petite structure : le dirigeant recrute lui-même")
    elif category == "direction" and company.headcount_code in LARGE_COMPANY:
        score -= 20
        reasons.append("grande entreprise : écrire au dirigeant a peu de chances d'aboutir")

    if category == "rh":
        reasons.append("les RH reçoivent beaucoup de candidatures : utile, mais pas prioritaire")

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
        # Une adresse déduite reste une hypothèse : elle ne doit pas passer
        # devant une adresse observée à interlocuteur équivalent.
        score -= 30
        reasons.append("adresse déduite du motif maison, jamais vue en ligne")
        if is_guessed(contact):
            score -= 20
            reasons.append("motif supposé (aucune adresse nominative observée) : rebond possible")

    if contact.mx_ok is True:
        score += 5

    if contact.smtp_ok is False:
        reasons.append("le serveur a rejeté cette adresse")

    if category in HARD_DEMOTE:
        reasons.append("boîte fonctionnelle sans rapport avec le recrutement")

    # Une adresse qui ne peut pas recevoir de courrier ne vaut rien, quelle que
    # soit la qualité de son titulaire : un plafond dur vaut mieux qu'une pénalité
    # que le score d'un bon profil finirait par absorber.
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
