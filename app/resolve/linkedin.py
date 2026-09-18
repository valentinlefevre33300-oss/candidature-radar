"""Le profil LinkedIn des personnes trouvées — sans jamais interroger LinkedIn.

Trois sources, dans l'ordre : les liens de profil présents sur le site de
l'entreprise (pages Équipe, signatures), rattachés à une personne quand
l'identifiant du profil contient son prénom et son nom ; puis une recherche
web faite par Claude (outil de recherche de l'API, limité à linkedin.com) ;
enfin, sans clé d'API, une recherche par moteur gratuit — qui ne rend plus
grand-chose, les moteurs servant des résultats dégradés aux robots. Dans tous
les cas on ne garde un profil que si son identifiant porte le nom de la
personne : jamais de profil deviné. LinkedIn lui-même n'est pas consulté :
ses conditions l'interdisent et il bloque vite.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from urllib.parse import unquote

import anthropic
import httpx
from bs4 import BeautifulSoup, Tag

from ..config import ANTHROPIC_API_KEY, CLAUDE_MODEL, REQUEST_TIMEOUT
from ..extract.people import strip_accents
from ..models import Contact

log = logging.getLogger(__name__)

PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9%._~-]+)", re.I)
SEARCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def canonical(url: str) -> str | None:
    """`https://www.linkedin.com/in/<identifiant>` ou None si ce n'est pas un profil."""
    match = PROFILE_RE.search(unquote(url))
    if not match:
        return None
    slug = match.group(1).rstrip("/").strip()
    return f"https://www.linkedin.com/in/{slug}" if slug else None


def profile_links(soup: BeautifulSoup | Tag) -> list[str]:
    """Les profils LinkedIn liés depuis une page, sans doublon, dans l'ordre."""
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        url = canonical(str(anchor["href"]))
        if url and url not in found:
            found.append(url)
    return found


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", strip_accents(text or "").lower()) if len(t) > 1}


def matches(url: str, first: str | None, last: str | None) -> bool:
    """L'identifiant du profil contient le nom entier et au moins un prénom."""
    match = PROFILE_RE.search(unquote(url))
    if not match or not first or not last:
        return False
    slug = _tokens(match.group(1))
    last_tokens = _tokens(last)
    first_tokens = _tokens(first)
    return bool(last_tokens) and last_tokens <= slug and bool(first_tokens & slug)


def attach(contacts: list[Contact], urls: list[str]) -> int:
    """Rattache aux contacts nominatifs les profils dont l'identifiant porte leur nom."""
    count = 0
    for contact in contacts:
        if contact.linkedin_url or not (contact.first_name and contact.last_name):
            continue
        for url in urls:
            if matches(url, contact.first_name, contact.last_name):
                contact.linkedin_url = url
                contact.reasons.append("profil LinkedIn lié depuis le site")
                count += 1
                break
    return count


_HEADERS = {"User-Agent": SEARCH_UA, "Accept-Language": "fr-FR,fr;q=0.9", "Accept": "text/html"}
_cooldown: set[str] = set()   # moteurs qui ont refusé (blocage, défi) : on n'insiste pas dans la session


def _bing_urls(html: str) -> list[str]:
    """Bing enveloppe ses résultats dans une redirection dont la cible est en base64."""
    out: list[str] = []
    for enc in re.findall(r"u=a1([A-Za-z0-9_-]+)", html):
        try:
            out.append(base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            continue
    return out


async def _engine(client: httpx.AsyncClient, name: str, query: str) -> list[str]:
    """Les adresses renvoyées par un moteur, ou [] s'il refuse (et on le retient)."""
    if name in _cooldown:
        return []
    try:
        if name == "bing":
            resp = await client.get("https://www.bing.com/search", params={"q": query, "setlang": "fr", "cc": "fr"},
                                    headers=_HEADERS, timeout=REQUEST_TIMEOUT)
            urls = _bing_urls(resp.text)
        elif name == "brave":
            resp = await client.get("https://search.brave.com/search", params={"q": query, "source": "web"},
                                    headers=_HEADERS, timeout=REQUEST_TIMEOUT)
            urls = [str(a["href"]) for a in BeautifulSoup(resp.text, "lxml").find_all("a", href=True)]
        else:
            resp = await client.post("https://lite.duckduckgo.com/lite/", data={"q": query},
                                     headers=_HEADERS, timeout=REQUEST_TIMEOUT)
            urls = re.findall(r"https?://[^\s\"'<>]+", unquote(resp.text))
    except httpx.HTTPError as exc:
        log.debug("moteur %s indisponible : %s", name, exc)
        return []
    if resp.status_code in (202, 403, 429):   # défi anti-robot ou limitation : inutile de continuer
        log.info("moteur %s refuse les requêtes (%s), on n'insiste pas", name, resp.status_code)
        _cooldown.add(name)
        return []
    return urls


async def _engines_profile(client: httpx.AsyncClient, first: str, last: str,
                           company_name: str) -> str | None:
    query = f'"{first} {last}" {company_name} linkedin'.strip()
    for name in ("bing", "brave", "ddg"):
        for raw in await _engine(client, name, query):
            url = canonical(raw)
            if url and matches(url, first, last):
                return url
    return None


# --- recherche web par Claude ---------------------------------------------

_SYSTEM = (
    "Tu retrouves l'adresse du profil LinkedIn public d'une personne à partir de son nom "
    "et de son entreprise. Fais une recherche web, puis réponds uniquement par l'adresse du "
    "profil (https://www.linkedin.com/in/...) si un résultat porte clairement le prénom et "
    "le nom de la personne, sinon réponds exactement INCONNU. Aucun autre texte."
)
_TOOLS = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 2,
           "allowed_domains": ["linkedin.com"]}]
_LOOKUPS = asyncio.Semaphore(3)   # recherches Claude simultanées, toutes entreprises confondues
_client: anthropic.AsyncAnthropic | None = None


def search_available() -> bool:
    """La recherche web par Claude est-elle possible (clé d'API renseignée) ?"""
    return bool(ANTHROPIC_API_KEY)


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
    return _client


def _urls_in(content: list) -> list[str]:
    """La réponse du modèle d'abord, puis toutes les adresses remontées par la recherche."""
    urls: list[str] = []
    for block in content:
        if block.type == "text":
            urls.extend(re.findall(r"https?://\S+", block.text))
    for block in content:
        results = getattr(block, "content", None)
        if block.type == "web_search_tool_result" and isinstance(results, list):
            urls.extend(url for url in (getattr(r, "url", None) for r in results) if url)
    return urls


async def _claude_profile(first: str, last: str, company_name: str) -> str | None:
    if not search_available():
        return None
    prompt = f"Profil LinkedIn de {first} {last}, qui travaille (ou a travaillé) chez {company_name}."
    try:
        async with _LOOKUPS:
            response = await _get_client().beta.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=300,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                tools=_TOOLS,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
    except anthropic.APIError as exc:
        log.warning("recherche LinkedIn par Claude impossible : %s", exc)
        return None
    if response.stop_reason == "refusal":
        return None
    for raw in _urls_in(response.content):
        url = canonical(raw)
        if url and matches(url, first, last):
            return url
    return None


async def find_profile(client: httpx.AsyncClient, first: str, last: str,
                       company_name: str) -> str | None:
    """Le profil de la personne, ou None. Recherche web par Claude si une clé est
    configurée, moteurs gratuits sinon ; dans les deux cas seul un profil dont
    l'identifiant porte le nom est retenu."""
    if search_available():
        return await _claude_profile(first, last, company_name)
    return await _engines_profile(client, first, last, company_name)
