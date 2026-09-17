"""Retrouver le site web d'une entreprise à partir de sa raison sociale.

L'annuaire Sirene ne publie pas l'URL du site. C'est le maillon faible de la
chaîne, traité en deux temps :
  1. déduction : « CHAPSVISION SAS » -> chapsvision.fr / chapsvision.com, testés
     directement. Gratuit, instantané, suffisant pour une bonne part des cas.
  2. repli moteur de recherche, seulement si la déduction échoue.

Chaque domaine retenu est *vérifié* : on exige que le nom de l'entreprise se
retrouve dans la page, sinon on repart avec un domaine homonyme.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import unquote, urlparse

import httpx

from ..config import REQUEST_TIMEOUT
from ..extract.people import strip_accents
from ..models import Company
from ..crawl.fetcher import PoliteFetcher

log = logging.getLogger(__name__)

# Formes juridiques et mentions à retirer avant de construire un nom de domaine.
LEGAL_NOISE = {
    "sas", "sasu", "sarl", "eurl", "sa", "sci", "scp", "scm", "selarl", "snc",
    "sccv", "scic", "scop", "gie", "gaec", "earl", "asso", "association",
    "societe", "ste", "groupe", "group", "holding", "france", "international",
    "cie", "compagnie", "etablissements", "ets", "entreprise", "et", "de", "du",
    "des", "la", "le", "les", "l", "d", "au", "aux", "en",
}

# TLD testés en déduction, dans l'ordre de probabilité pour une entreprise française.
CANDIDATE_TLDS = (".fr", ".com", ".net", ".eu", ".io", ".org")

# Domaines qui ne sont jamais le site d'une entreprise : agrégateurs, réseaux,
# annuaires. Ils remontent systématiquement dans les résultats de recherche.
NON_CORPORATE_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "tiktok.com", "pinterest.fr", "pinterest.com", "viadeo.com",
    "societe.com", "verif.com", "pappers.fr", "infogreffe.fr", "bodacc.fr",
    "annuaire-entreprises.data.gouv.fr", "manageo.fr", "kompass.com", "bilansgratuits.fr",
    "wikipedia.org", "glassdoor.fr", "indeed.com", "welcometothejungle.com",
    "hellowork.com", "apec.fr", "pole-emploi.fr", "francetravail.fr", "meilleurtaux.com",
    "leboncoin.fr", "pagesjaunes.fr", "google.com", "youtube.fr", "amazon.fr",
    "tripadvisor.fr", "yelp.fr", "doctolib.fr", "crunchbase.com", "bloomberg.com",
    # le moteur interrogé renvoie aussi ses propres pages
    "duckduckgo.com", "duck.com", "bing.com", "qwant.com", "ecosia.org",
    "startpage.com", "yandex.com", "baidu.com", "archive.org", "webcache.googleusercontent.com",
}

# DuckDuckGo refuse les agents inconnus sur son point d'entrée HTML. On s'y
# présente donc comme un navigateur — uniquement pour cette requête de recherche,
# jamais pour le crawl des sites d'entreprises.
SEARCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def company_tokens(name: str) -> list[str]:
    """Mots significatifs du nom, hors forme juridique."""
    # « EXASCALE AGENCE WEB (EXASCALE) » -> on ignore le rappel entre parenthèses
    cleaned = re.sub(r"\(.*?\)", " ", name)
    words = re.split(r"[^A-Za-z0-9]+", strip_accents(cleaned).lower())
    return [w for w in words if w and w not in LEGAL_NOISE and len(w) > 1]


def candidate_domains(name: str) -> list[str]:
    """Génère les domaines plausibles, du plus au moins probable."""
    tokens = company_tokens(name)
    if not tokens:
        return []
    bases: list[str] = []
    joined = "".join(tokens)
    hyphened = "-".join(tokens)
    if len(tokens) == 1:
        bases = [tokens[0]]
    else:
        bases = [joined, hyphened, tokens[0]]
    # Un nom trop long donne des domaines invraisemblables.
    bases = [b for b in dict.fromkeys(bases) if 3 <= len(b) <= 40]
    return [f"{base}{tld}" for base in bases for tld in CANDIDATE_TLDS]


# Mots trop banals pour identifier une entreprise : ils apparaissent sur
# n'importe quelle page française et gonflent artificiellement la confiance.
GENERIC_TOKENS = {
    "web", "digital", "conseil", "consulting", "services", "service", "solutions",
    "solution", "technologies", "technologie", "tech", "groupe", "group", "agence",
    "agency", "studio", "atelier", "maison", "centre", "batiment", "travaux",
    "fr", "com", "eu", "sud", "nord", "est", "ouest", "grand", "nouveau", "nouvelle",
    "local", "region", "ile", "cabinet", "bureau", "expert", "experts", "partner",
    "partners", "developpement", "innovation", "gestion", "systeme", "systemes",
}


def name_matches_page(name: str, html: str) -> float:
    """Confiance [0,1] que la page parle bien de cette entreprise.

    Pondéré par la longueur des mots : « chapsvision » identifie une entreprise,
    « local » ou « web » non. Et si le mot le plus distinctif est absent, on
    considère que ce n'est pas le bon site — sans quoi « LOCAL.FR » matchait
    la page de résultats du moteur de recherche lui-même.
    """
    tokens = company_tokens(name)
    if not tokens:
        return 0.0
    haystack = strip_accents(html.lower())

    distinctive = [t for t in tokens if len(t) >= 4 and t not in GENERIC_TOKENS]
    if not distinctive:
        # « LOCAL.FR », « GROUPE CONSEIL »... : rien d'identifiant dans le nom.
        # Ces mots figurent sur des milliers de pages, on ne peut donc rien
        # confirmer. On preferera repondre « introuvable ».
        return 0.0

    # Le mot le plus long est le plus identifiant : il fait office de verrou.
    anchor = max(distinctive, key=len)
    if anchor not in haystack:
        return 0.0

    total = sum(len(t) ** 2 for t in tokens)
    hits = sum(len(t) ** 2 for t in tokens if t in haystack)
    return hits / total if total else 0.0


def _usable_domain(url: str) -> str | None:
    host = urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    if not host or "." not in host:
        return None
    if host in NON_CORPORATE_DOMAINS or any(
        host == d or host.endswith("." + d) for d in NON_CORPORATE_DOMAINS
    ):
        return None
    return host


async def _verify(fetcher: PoliteFetcher, domain: str, company: Company) -> float:
    """Confiance [0,1] que `domain` soit bien le site de `company`.

    La confiance repose *uniquement* sur ce que contient la page. Accorder un
    bonus au simple fait que le domaine ressemble au nom laisse passer les
    homonymes : « GROUPE SAPH » résolvait vers saph.net, un site sans aucun
    rapport. Mieux vaut rendre « introuvable » qu'un domaine faux, qui produirait
    ensuite des adresses inventées.
    """
    for host in (domain, f"www.{domain}"):
        page = await fetcher.get_html(f"https://{host}/")
        if page:
            _url, html = page
            return name_matches_page(company.name, html)
    return 0.0


async def guess_by_heuristic(fetcher: PoliteFetcher, company: Company) -> tuple[str, float] | None:
    for domain in candidate_domains(company.name)[:8]:
        confidence = await _verify(fetcher, domain, company)
        if confidence >= 0.5:
            return domain, confidence
    return None


async def search_engine_lookup(client: httpx.AsyncClient, company: Company) -> list[str]:
    """Interroge DuckDuckGo et renvoie les domaines candidats, dans l'ordre."""
    city = f" {company.city}" if company.city else ""
    query = f"{company.name}{city} site officiel"
    try:
        resp = await client.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers={"User-Agent": SEARCH_UA},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("recherche indisponible pour %s : %s", company.name, exc)
        return []

    raw = re.findall(r"uddg=([^&\"']+)", resp.text) or re.findall(
        r'href="(https?://[^"]+)"', resp.text)
    domains: list[str] = []
    for item in raw:
        host = _usable_domain(unquote(item))
        if host and host not in domains:
            domains.append(host)
    return domains[:6]


async def resolve_domain(fetcher: PoliteFetcher, client: httpx.AsyncClient,
                         company: Company, *, use_search: bool = True) -> Company:
    """Renseigne `company.domain`, sa confiance et la méthode employée."""
    found = await guess_by_heuristic(fetcher, company)
    if found:
        company.domain, company.domain_confidence = found
        company.domain_method = "deduction"
        return company

    if use_search:
        await asyncio.sleep(1.0)  # on n'enchaîne pas les requêtes au moteur
        for domain in await search_engine_lookup(client, company):
            confidence = await _verify(fetcher, domain, company)
            if confidence >= 0.4:
                company.domain, company.domain_confidence = domain, confidence
                company.domain_method = "recherche"
                return company

    company.domain_method = "introuvable"
    return company
