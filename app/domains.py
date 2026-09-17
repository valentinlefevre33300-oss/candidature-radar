"""Domaines métier : relier le poste visé aux fonctions qui recrutent pour lui.

Le poste « développeur Python » n'apparaît jamais tel quel dans l'intitulé d'un
interlocuteur. Ce qu'on cherche, c'est la personne qui dirige ce métier dans
l'entreprise (CTO, responsable technique, lead dev) ou qui l'exerce (un pair).
Chaque domaine décrit donc trois choses :

  - `job`   : ce qu'on trouve dans l'intitulé du poste visé ;
  - `heads` : les fonctions qui dirigent ce métier — les vrais décideurs ;
  - `role`  : les mots qui, dans une fonction, rattachent quelqu'un au domaine.

Tout est comparé en minuscules, sans accents, sur des mots entiers.
"""
from __future__ import annotations

import re
import unicodedata


def _norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


DOMAINS: dict[str, dict[str, list[str]]] = {
    "tech": {
        "job": ["developpeur", "developpeuse", "dev", "devops", "sre", "ingenieur logiciel",
                "software", "backend", "back-end", "frontend", "front-end", "fullstack", "full-stack",
                "full stack", "python", "java", "javascript", "typescript", "php", "golang", "rust",
                "node", "react", "angular", "vue", "data engineer", "data scientist", "data analyst",
                "machine learning", "intelligence artificielle", "cloud", "cybersecurite",
                "securite informatique", "administrateur systeme", "sysadmin", "reseau", "qa",
                "testeur", "mobile", "android", "ios", "embarque", "informatique", "it",
                "ingenieur developpement", "integrateur", "architecte logiciel"],
        "heads": ["cto", "dsi", "directeur technique", "directrice technique", "vp engineering",
                  "head of engineering", "engineering manager", "lead developer", "lead dev",
                  "tech lead", "responsable technique", "responsable informatique", "responsable r&d",
                  "directeur informatique", "directrice informatique",
                  "directeur des systemes d'information", "head of data", "lead data",
                  "responsable data", "chief technology", "responsable developpement",
                  "directeur r&d", "responsable si", "head of tech", "responsable it"],
        "role": ["technique", "tech", "engineering", "ingenierie", "developpement", "development",
                 "informatique", "it", "si", "dsi", "logiciel", "software", "r&d", "data",
                 "architecte", "architect", "cto", "devops", "cloud", "infrastructure",
                 "systemes d'information", "systeme d'information", "digital", "numerique",
                 "developpeur", "developpeuse", "ingenieur", "ingenieure"],
    },
    "produit": {
        "job": ["product owner", "product manager", "po", "pm", "chef de produit", "product",
                "produit", "product designer"],
        "heads": ["cpo", "head of product", "directeur produit", "directrice produit",
                  "responsable produit", "chief product", "lead product", "product lead",
                  "vp product"],
        "role": ["produit", "product", "cpo", "po", "digital"],
    },
    "design": {
        "job": ["designer", "ux", "ui", "graphiste", "direction artistique", "directeur artistique",
                "motion", "illustrateur", "illustratrice", "webdesigner", "design"],
        "heads": ["head of design", "directeur artistique", "directrice artistique",
                  "responsable design", "lead designer", "design lead", "directeur de creation",
                  "directrice de creation", "directeur creation", "chief design", "responsable studio"],
        "role": ["design", "ux", "ui", "creatif", "creative", "creation", "artistique", "graphique",
                 "brand", "studio"],
    },
    "marketing": {
        "job": ["marketing", "communication", "growth", "seo", "sea", "content", "community manager",
                "brand", "acquisition", "crm", "e-commerce", "ecommerce", "charge de communication",
                "chargee de communication", "redacteur", "redactrice"],
        "heads": ["cmo", "head of marketing", "directeur marketing", "directrice marketing",
                  "responsable marketing", "responsable communication",
                  "directeur de la communication", "directrice de la communication",
                  "head of growth", "responsable acquisition", "chief marketing",
                  "responsable e-commerce", "responsable digital", "directeur digital",
                  "responsable contenu", "head of content"],
        "role": ["marketing", "communication", "growth", "acquisition", "digital", "brand", "marque",
                 "contenu", "content", "seo", "cmo", "e-commerce", "ecommerce", "crm", "media"],
    },
    "commercial": {
        "job": ["commercial", "commerciale", "sales", "business developer", "business development",
                "account manager", "account executive", "sdr", "bdr", "charge d'affaires",
                "chargee d'affaires", "ingenieur d'affaires", "key account", "technico-commercial",
                "vendeur", "vendeuse", "conseiller commercial"],
        "heads": ["directeur commercial", "directrice commerciale", "head of sales",
                  "responsable commercial", "responsable commerciale", "sales manager", "vp sales",
                  "chief revenue", "cro", "responsable des ventes", "directeur des ventes",
                  "directeur du developpement", "responsable business", "directeur business"],
        "role": ["commercial", "commerciale", "sales", "ventes", "vente", "business",
                 "developpement commercial", "affaires", "clientele", "comptes", "account",
                 "partenariats"],
    },
    "finance": {
        "job": ["comptable", "comptabilite", "controleur de gestion", "controleuse de gestion",
                "financier", "financiere", "finance", "tresorerie", "audit", "auditeur", "auditrice",
                "daf", "gestionnaire de paie", "credit"],
        "heads": ["daf", "directeur administratif et financier", "directrice administrative et financiere",
                  "directeur financier", "directrice financiere", "responsable comptable",
                  "responsable administratif", "responsable financier", "chef comptable", "cfo",
                  "head of finance", "responsable controle de gestion", "expert-comptable"],
        "role": ["finance", "financier", "financiere", "comptabilite", "comptable", "gestion",
                 "controle de gestion", "administratif", "daf", "tresorerie", "audit"],
    },
    "rh": {
        "job": ["ressources humaines", "rh", "recruteur", "recruteuse", "recrutement", "talent",
                "charge rh", "chargee rh", "gestionnaire rh", "paie"],
        "heads": ["drh", "rrh", "responsable rh", "responsable ressources humaines",
                  "directeur des ressources humaines", "directrice des ressources humaines",
                  "head of people", "chief people", "talent acquisition manager",
                  "responsable recrutement"],
        "role": ["ressources humaines", "rh", "recrutement", "talent", "people", "hr"],
    },
    "ops": {
        "job": ["logistique", "supply chain", "achats", "acheteur", "acheteuse", "approvisionnement",
                "production", "qualite", "hse", "qse", "maintenance", "methodes", "planification",
                "operations", "ordonnancement", "chef d'atelier", "chef d'equipe", "technicien",
                "technicienne", "operateur", "operatrice"],
        "heads": ["directeur des operations", "directrice des operations", "coo",
                  "responsable logistique", "responsable production", "directeur de production",
                  "responsable qualite", "responsable achats", "directeur achats",
                  "responsable supply", "responsable exploitation", "directeur industriel",
                  "directeur d'usine", "directeur de site", "responsable maintenance",
                  "responsable hse", "responsable qse", "chef d'atelier", "responsable d'atelier"],
        "role": ["logistique", "supply", "achats", "production", "qualite", "hse", "qse",
                 "maintenance", "operations", "exploitation", "industriel", "usine", "atelier",
                 "methodes", "technique"],
    },
    "projet": {
        "job": ["chef de projet", "cheffe de projet", "chef de projets", "project manager",
                "scrum master", "coordinateur de projet", "coordinatrice de projet",
                "directeur de projet", "pmo", "consultant", "consultante", "product owner"],
        "heads": ["directeur de projet", "directrice de projet", "responsable projets",
                  "head of delivery", "responsable pmo", "directeur conseil", "responsable conseil",
                  "directeur des operations", "responsable des projets", "delivery manager"],
        "role": ["projet", "projets", "project", "pmo", "delivery", "conseil", "consulting",
                 "transformation", "organisation", "agile"],
    },
    "juridique": {
        "job": ["juriste", "avocat", "avocate", "legal", "droit", "contrats", "compliance",
                "conformite", "paralegal"],
        "heads": ["directeur juridique", "directrice juridique", "responsable juridique",
                  "head of legal", "general counsel", "responsable conformite"],
        "role": ["juridique", "legal", "droit", "conformite", "compliance", "contrats"],
    },
    "support": {
        "job": ["customer success", "support client", "service client", "relation client",
                "conseiller client", "conseillere client", "charge de clientele",
                "chargee de clientele", "helpdesk", "technicien support"],
        "heads": ["responsable service client", "head of customer", "responsable relation client",
                  "customer success manager", "responsable support",
                  "directeur de la relation client"],
        "role": ["support", "service client", "relation client", "customer", "clientele",
                 "satisfaction", "succes client", "customer success"],
    },
    "btp": {
        "job": ["conducteur de travaux", "conductrice de travaux", "chef de chantier",
                "maitre d'oeuvre", "dessinateur", "projeteur", "geometre", "economiste de la construction",
                "ingenieur travaux", "architecte", "batiment", "genie civil", "electricien", "plombier",
                "menuisier", "macon", "chauffagiste"],
        "heads": ["directeur travaux", "directeur des travaux", "responsable travaux",
                  "directeur d'agence", "responsable d'agence", "chef d'agence",
                  "directeur technique", "responsable bureau d'etudes", "directeur d'exploitation",
                  "responsable chantier", "conducteur de travaux principal"],
        "role": ["travaux", "chantier", "chantiers", "batiment", "construction", "bureau d'etudes",
                 "etudes", "agence", "exploitation", "technique", "genie civil", "maitrise d'oeuvre"],
    },
    "sante": {
        "job": ["infirmier", "infirmiere", "aide-soignant", "aide-soignante", "soignant",
                "medecin", "pharmacien", "pharmacienne", "kinesitherapeute", "psychologue",
                "educateur", "educatrice", "auxiliaire", "sage-femme", "preparateur en pharmacie"],
        "heads": ["cadre de sante", "directeur d'etablissement", "directrice d'etablissement",
                  "directeur des soins", "directrice des soins", "chef de service", "cheffe de service",
                  "responsable de service", "medecin chef", "coordinateur", "coordinatrice",
                  "directeur medical", "responsable de pole", "infirmier coordinateur",
                  "infirmiere coordinatrice", "idec"],
        "role": ["soins", "sante", "medical", "medicale", "clinique", "service", "etablissement",
                 "pole", "soignant", "paramedical"],
    },
}

# Mots qui, en tête d'un intitulé, signent une responsabilité d'équipe.
MANAGER_PREFIXES = (
    "responsable", "directeur", "directrice", "director", "head of", "head", "chef de", "chef d'",
    "cheffe de", "cheffe d'", "lead", "vp", "vice-president", "vice president", "chief",
    "coordinateur", "coordinatrice", "dirigeant", "dirigeante", "gerant", "gerante",
)
MANAGER_ANYWHERE_RE = re.compile(r"\b(?:head of|lead|chief|vp)\b")
MANAGER_PREFIX_RE = re.compile(
    r"\b(?:responsable|directeur|directrice|director|chef de|chef d'|cheffe de|cheffe d'|"
    r"vice-president|vice president|coordinateur|coordinatrice|dirigeant|dirigeante|gerant|gerante)\b")

_STOPWORDS = {"de", "du", "des", "en", "la", "le", "les", "un", "une", "et", "a", "au", "aux",
              "pour", "chez", "senior", "junior", "confirme", "confirmee", "h", "f", "h/f", "f/h",
              "alternance", "alternant", "alternante", "stage", "stagiaire", "cdi", "cdd",
              "freelance", "temps", "plein", "partiel"}


# Sigles de deux lettres tolérés dans une FONCTION lue sur une page : « RH » et
# « HR » sont sans ambiguïté. Les autres (« po », « pm », « it », « si », « ux »…)
# valent dans un intitulé de poste saisi par l'utilisateur, pas dans du texte
# libre : « Lech Po(znań) » sur un site de paris devenait un product owner.
_SHORT_OK = {"rh", "hr"}


def _compile(terms: list[str], *, allow_short: bool = False) -> re.Pattern[str]:
    kept = [t for t in terms if allow_short or len(_norm(t)) >= 3 or _norm(t) in _SHORT_OK]
    escaped = sorted((re.escape(_norm(t)) for t in kept), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(escaped) + r")(?![a-z0-9])")


_JOB_RE = {key: _compile(spec["job"], allow_short=True) for key, spec in DOMAINS.items()}
_HEAD_RE = {key: _compile(spec["heads"]) for key, spec in DOMAINS.items()}
_ROLE_RE = {key: _compile(spec["role"] + spec["job"]) for key, spec in DOMAINS.items()}
ANY_ROLE_RE = _compile(
    sorted({t for spec in DOMAINS.values() for t in spec["heads"] + spec["role"]}
           | set(MANAGER_PREFIXES) | {"president", "presidente", "fondateur", "fondatrice",
                                      "founder", "ceo", "coo", "cfo", "directeur general",
                                      "directrice generale", "gerant", "gerante", "associe",
                                      "associee", "partner", "manager"}))


def job_domain(job_title: str) -> str | None:
    """Le domaine dominant d'un intitulé de poste, ou None s'il est inclassable."""
    text = _norm(job_title)
    if not text.strip():
        return None
    scores: dict[str, int] = {}
    for key, pattern in _JOB_RE.items():
        hits = pattern.findall(text)
        if hits:
            # Un terme long (« product owner ») pèse plus qu'un sigle (« pm »).
            scores[key] = sum(len(h) for h in hits)
    if not scores:
        return None
    return max(scores, key=scores.get)


def is_manager(role_title: str, domain: str | None = None) -> bool:
    """La fonction implique-t-elle de diriger une équipe ?"""
    text = _norm(role_title).strip(" -–:,.")
    if not text:
        return False
    if domain and _HEAD_RE[domain].search(text):
        return True
    # Les intitulés arrivent parfois noyés dans un extrait de page : on cherche
    # donc les marqueurs sur des mots entiers, pas seulement en tête.
    if MANAGER_PREFIX_RE.search(text) or MANAGER_ANYWHERE_RE.search(text):
        return True
    # « engineering manager », « sales manager » : le mot précédent doit être du domaine.
    match = re.search(r"([a-z&'-]+)\s+manager\b", text)
    if match and domain and _ROLE_RE[domain].search(match.group(1)):
        return True
    return False


def classify_role(role_title: str | None, domain: str | None) -> tuple[str | None, bool]:
    """(catégorie, dirige-une-équipe) pour une fonction lue quelque part.

    Priorité au métier visé : « directeur technique » est un interlocuteur métier
    pour un développeur, pas un simple membre de la direction.
    """
    if not role_title:
        return None, False
    text = _norm(role_title)
    if domain and (_HEAD_RE[domain].search(text) or _ROLE_RE[domain].search(text)):
        return "metier", is_manager(text, domain)
    if _ROLE_RE["rh"].search(text) or _HEAD_RE["rh"].search(text):
        return "rh", is_manager(text, "rh")
    if re.search(r"\b(?:president|presidente|directeur general|directrice generale|gerant|gerante|"
                 r"fondateur|fondatrice|founder|co-?founder|ceo|coo|dg|pdg|dirigeant|dirigeante|"
                 r"managing director|associe|associee|partner|chief executive)\b", text):
        return "direction", True
    return None, is_manager(text, domain)


def domain_label(domain: str | None) -> str:
    return {"tech": "tech", "produit": "produit", "design": "design", "marketing": "marketing",
            "commercial": "commercial", "finance": "finance", "rh": "RH", "ops": "opérations",
            "projet": "projet", "juridique": "juridique", "support": "relation client",
            "btp": "BTP", "sante": "santé"}.get(domain or "", "")
