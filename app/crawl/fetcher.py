"""Client HTTP poli : robots.txt, débit limité par hôte, garde-fous de taille.

Un scraper qui martèle un serveur se fait bannir et, accessoirement, coûte de
l'argent à la personne en face. Toutes les requêtes sortantes passent par ici.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import (
    MAX_PAGE_BYTES,
    PER_DOMAIN_DELAY,
    REQUEST_TIMEOUT,
    RESPECT_ROBOTS,
    USER_AGENT,
)

log = logging.getLogger(__name__)

HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class PoliteFetcher:
    """Encapsule un `httpx.AsyncClient` et fait respecter les règles de politesse."""

    def __init__(self, client: httpx.AsyncClient, *, delay: float = PER_DOMAIN_DELAY,
                 respect_robots: bool = RESPECT_ROBOTS) -> None:
        self.client = client
        self.delay = delay
        self.respect_robots = respect_robots
        self._last_hit: dict[str, float] = defaultdict(float)
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- robots.txt ---------------------------------------------------------

    async def _robots_for(self, host_root: str) -> RobotFileParser | None:
        if host_root in self._robots:
            return self._robots[host_root]
        async with self._robots_locks[host_root]:
            if host_root in self._robots:  # un autre worker a pu le charger entre-temps
                return self._robots[host_root]
            parser: RobotFileParser | None = None
            try:
                resp = await self.client.get(
                    f"{host_root}/robots.txt",
                    timeout=REQUEST_TIMEOUT,
                    headers={"User-Agent": USER_AGENT},
                )
                if resp.status_code == 200 and len(resp.content) < 500_000:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except (httpx.HTTPError, UnicodeDecodeError) as exc:
                # Pas de robots.txt lisible = pas d'interdiction explicite.
                log.debug("robots.txt illisible pour %s : %s", host_root, exc)
            self._robots[host_root] = parser
            return parser

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlparse(url)
        if not parts.scheme or not parts.netloc:
            return False
        parser = await self._robots_for(f"{parts.scheme}://{parts.netloc}")
        if parser is None:
            return True
        return parser.can_fetch(USER_AGENT, url)

    # -- débit --------------------------------------------------------------

    async def _throttle(self, host: str) -> None:
        """Garantit `delay` secondes entre deux requêtes vers le même hôte."""
        async with self._host_locks[host]:
            wait = self.delay - (time.monotonic() - self._last_hit[host])
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    # -- récupération -------------------------------------------------------

    async def get_html(self, url: str) -> tuple[str, str] | None:
        """Renvoie (url_finale, html) ou None si la page est inexploitable."""
        if not await self.allowed(url):
            log.debug("robots.txt interdit %s", url)
            return None

        host = urlparse(url).netloc
        await self._throttle(host)

        try:
            resp = await self.client.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
                },
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            log.debug("échec %s : %s", url, type(exc).__name__)
            return None

        if resp.status_code != 200:
            return None

        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and not any(ctype.startswith(t) for t in HTML_TYPES):
            return None
        if len(resp.content) > MAX_PAGE_BYTES:
            log.debug("page trop lourde, ignorée : %s", url)
            return None

        try:
            return str(resp.url), resp.text
        except UnicodeDecodeError:
            return str(resp.url), resp.content.decode("utf-8", errors="replace")

    async def head_ok(self, url: str) -> bool:
        """Vérifie qu'une URL répond, sans télécharger le corps (test de domaine)."""
        host = urlparse(url).netloc
        await self._throttle(host)
        try:
            resp = await self.client.head(url, timeout=REQUEST_TIMEOUT,
                                          headers={"User-Agent": USER_AGENT},
                                          follow_redirects=True)
            if resp.status_code < 400:
                return True
            # Certains serveurs refusent HEAD mais acceptent GET.
            if resp.status_code in (403, 405, 501):
                resp = await self.client.get(url, timeout=REQUEST_TIMEOUT,
                                             headers={"User-Agent": USER_AGENT},
                                             follow_redirects=True)
                return resp.status_code < 400
        except httpx.HTTPError:
            return False
        return False


def build_client() -> httpx.AsyncClient:
    """Client partagé : HTTP/2, pool borné, redirections suivies."""
    return httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        headers={"User-Agent": USER_AGENT},
        verify=True,
    )
