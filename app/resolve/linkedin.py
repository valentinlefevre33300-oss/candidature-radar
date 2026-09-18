"""Le profil LinkedIn des personnes trouvées — sans jamais interroger LinkedIn.

Deux sources, dans l'ordre : les liens de profil présents sur le site de
l'entreprise (pages Équipe, signatures), rattachés à une personne quand
l'identifiant du profil contient son prénom et son nom ; puis, à la demande,
une recherche par moteur (`"Prénom Nom" Entreprise linkedin`) dont on ne
garde un résultat que s'il passe le même contrôle. LinkedIn lui-même n'est
pas consulté : ses conditions l'interdisent et il bloque vite.
"""
from __future__ import annotations

import base64
import logging
import re
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from ..config import REQUEST_TIMEOUT
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


def profile_links(soup: BeautifulSoup) -> list[str]:
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


async def find_profile(client: httpx.AsyncClient, first: str, last: str,
                       company_name: str) -> str | None:
    """Cherche le profil par moteur, au mieux : les moteurs gratuits bloquent vite les
    requêtes automatiques, et on ne renvoie que ce qui porte le nom de la personne."""
    query = f'"{first} {last}" {company_name} linkedin'.strip()
    for name in ("bing", "brave", "ddg"):
        for raw in await _engine(client, name, query):
            url = canonical(raw)
            if url and matches(url, first, last):
                return url
    return None
