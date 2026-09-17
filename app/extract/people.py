"""Rattacher une adresse à un humain et à une fonction.

Deux directions complémentaires :
  - descendante : lire l'adresse (« marie.dupont@ ») pour en déduire un nom ;
  - montante    : lire le texte autour de l'adresse pour en déduire une fonction.

Et surtout : déduire le *motif* d'adressage de l'entreprise, ce qui permet de
reconstituer l'adresse d'un dirigeant connu par l'annuaire mais absent du site.
"""
from __future__ import annotations

import re
import unicodedata

# Fonctions classées par utilité pour une candidature spontanée.
ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "rh": (
        "ressources humaines", "resources humaines", "drh", "rrh", "human resources",
        "talent acquisition", "talent", "recrutement", "recruteur", "recruteuse",
        "recruiter", "recruiting", "people", "hr manager", "hrbp", "chargee de recrutement",
        "charge de recrutement", "responsable rh", "directrice des ressources",
        "directeur des ressources", "staffing", "chief people",
    ),
    "direction": (
        "president", "presidente", "directeur general", "directrice generale",
        "gerant", "gerante", "fondateur", "fondatrice", "founder", "co-founder",
        "cofounder", "ceo", "coo", "dg", "pdg", "managing director", "associe",
        "associee", "partner", "chief executive", "dirigeant",
    ),
    "technique": (
        "cto", "chief technology", "directeur technique", "directrice technique",
        "vp engineering", "head of engineering", "responsable technique",
        "lead developer", "tech lead", "engineering manager", "architecte",
    ),
}

# Boîtes fonctionnelles repérables au seul préfixe de l'adresse.
MAILBOX_CATEGORY: dict[str, tuple[str, ...]] = {
    "rh": ("rh", "hr", "recrutement", "recrutements", "recruitment", "jobs", "job",
           "emploi", "emplois", "career", "careers", "carriere", "carrieres",
           "candidature", "candidatures", "talent", "talents", "cv", "people",
           "nousrejoindre", "rejoignez", "join"),
    "direction": ("direction", "dg", "ceo", "president", "presidence", "gerance",
                  "founders", "founder", "bureau"),
    "generique": ("contact", "info", "infos", "information", "hello", "bonjour",
                  "accueil", "societe", "agence", "bureau", "welcome", "mail",
                  "courrier", "secretariat"),
    "technique": ("support", "sav", "technique", "helpdesk", "assistance", "it",
                  "dev", "admin", "administration", "service-client", "serviceclient"),
    "juridique": ("dpo", "rgpd", "gdpr", "privacy", "legal", "juridique",
                  "confidentialite", "compliance", "conformite"),
    "commercial": ("commercial", "sales", "vente", "ventes", "devis", "achat",
                   "achats", "compta", "comptabilite", "facturation", "billing",
                   "finance", "marketing", "communication", "presse", "press"),
}

# Hébergeurs et agences que l'on retrouve dans les mentions légales : ce sont des
# prestataires du site, pas des interlocuteurs de l'entreprise ciblée.
SERVICE_PROVIDER_DOMAINS = {
    "ovh.com", "ovh.net", "ovhcloud.com", "ionos.fr", "ionos.com", "1and1.fr",
    "gandi.net", "o2switch.fr", "hostinger.fr", "planethoster.com", "infomaniak.com",
    "scaleway.com", "online.net", "amazonaws.com", "cloudflare.com", "wix.com",
    "squarespace.com", "shopify.com", "wordpress.com", "webflow.com", "hubspot.com",
    "google.com", "microsoft.com", "orange.fr", "sfr.fr", "free.fr", "bouyguestelecom.fr",
}

_TITLE_WINDOW = 110  # caractères examinés de part et d'autre d'une adresse


def strip_accents(value: str) -> str:
    """« Frédéric » -> « frederic ». Indispensable pour comparer aux adresses."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", strip_accents(value).lower())


def classify_mailbox(email: str) -> str:
    """Catégorise une adresse d'après sa partie locale uniquement."""
    local = email.split("@", 1)[0].lower()
    flat = re.sub(r"[^a-z]+", "", strip_accents(local))
    for category, keywords in MAILBOX_CATEGORY.items():
        if local in keywords or flat in keywords:
            return category
        if any(re.fullmatch(rf"{kw}[-._]?\d*", local) for kw in keywords):
            return category
    # Une partie locale structurée comme un nom est probablement nominative.
    if re.fullmatch(r"[a-z]{2,}[._-][a-z]{2,}", strip_accents(local)):
        return "nominatif"
    return "inconnu"


def guess_name_from_local(local: str) -> tuple[str | None, str | None]:
    """Déduit (prénom, nom) de la partie locale quand elle est structurée."""
    cleaned = strip_accents(local.lower())
    cleaned = re.sub(r"\d+$", "", cleaned)
    parts = [p for p in re.split(r"[._-]+", cleaned) if p]
    if len(parts) == 2:
        first, last = parts
        if len(first) >= 2 and len(last) >= 2:
            return first.title(), last.title()
        if len(first) == 1 and len(last) >= 2:  # « m.dupont »
            return None, last.title()
    return None, None


def find_role_near(text: str, email: str) -> tuple[str | None, str | None]:
    """Cherche une fonction citée autour de l'adresse. Renvoie (catégorie, extrait)."""
    lowered = strip_accents(text.lower())
    target = strip_accents(email.lower())
    position = lowered.find(target)
    if position == -1:
        return None, None
    window = lowered[max(0, position - _TITLE_WINDOW): position + len(target) + _TITLE_WINDOW]
    # L'adresse elle-meme pollue l'extrait : on la retire avant de decouper.
    window = window.replace(target, " ")
    for category, keywords in ROLE_KEYWORDS.items():
        for keyword in keywords:
            if keyword not in window:
                continue
            at = window.find(keyword)
            excerpt = window[max(0, at - 28): at + len(keyword) + 28]
            # On coupe aux frontieres de mots et on ecarte le bruit typographique
            # (numeros de rue, telephones, ponctuation de mise en page).
            excerpt = re.sub(r"[|•·]+", " ", excerpt)
            excerpt = re.sub(r"\s{2,}", " ", excerpt).strip(" .,;:-")
            words = excerpt.split()
            if words and len(words) > 2:
                words = words[1:] if len(words[0]) < 3 else words
            return category, " ".join(words)[:90]
    return None, None


# --- motif d'adressage de l'entreprise -------------------------------------

PATTERNS: tuple[tuple[str, str], ...] = (
    ("{first}.{last}", "prenom.nom"),
    ("{f}{last}", "pnom"),
    ("{f}.{last}", "p.nom"),
    ("{first}{last}", "prenomnom"),
    ("{first}_{last}", "prenom_nom"),
    ("{last}.{first}", "nom.prenom"),
    ("{last}{f}", "nomp"),
    ("{first}", "prenom"),
    ("{last}", "nom"),
)


def render(pattern: str, first: str, last: str) -> str:
    first_n, last_n = normalize(first), normalize(last)
    if not last_n:
        return ""
    return pattern.format(first=first_n, last=last_n,
                          f=first_n[:1], l=last_n[:1])


def infer_pattern(known_emails: list[str], people: list[tuple[str, str]]) -> str | None:
    """Déduit le motif d'adressage à partir d'adresses observées et de noms connus.

    `people` est une liste de (prénom, nom) — typiquement les dirigeants de l'annuaire
    ou les personnes citées sur la page équipe.
    """
    locals_seen = {e.split("@", 1)[0].lower() for e in known_emails}
    tally: dict[str, int] = {}
    for pattern, _label in PATTERNS:
        for first, last in people:
            candidate = render(pattern, first, last)
            if candidate and candidate in locals_seen:
                tally[pattern] = tally.get(pattern, 0) + 1
    if not tally:
        return None
    # À égalité, on privilégie le motif le plus spécifique (ordre de PATTERNS).
    order = [p for p, _ in PATTERNS]
    return max(tally, key=lambda p: (tally[p], -order.index(p)))
