"""Parcours ciblé d'un site : on ne crawle pas tout, seulement ce qui paie.

Sur un site vitrine, les emails se concentrent dans une poignée de pages :
contact, mentions légales, équipe, recrutement. On les priorise au lieu de
parcourir le site en largeur, ce qui divise le nombre de requêtes par dix.
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin, urldefrag, urlparse

from bs4 import BeautifulSoup

from ..config import MAX_PAGES_PER_SITE
from .fetcher import PoliteFetcher

log = logging.getLogger(__name__)

# Mots-clés d'URL / d'ancre, du plus au moins rentable.
PAGE_HINTS: list[tuple[int, tuple[str, ...]]] = [
    (100, ("mentions-legales", "mentions_legales", "mentionslegales", "legal-notice", "impressum")),
    (95, ("contact", "nous-contacter", "contactez", "nous-ecrire", "joindre")),
    (90, ("equipe", "team", "notre-equipe", "our-team", "qui-sommes-nous", "about-us",
          "a-propos", "apropos", "about", "direction", "management", "fondateurs", "founders")),
    (85, ("recrutement", "carriere", "carrieres", "career", "careers", "jobs", "emploi",
          "nous-rejoindre", "rejoignez", "join-us", "talent", "rh", "candidature")),
    (40, ("presse", "press", "media", "partenaires")),
]

# Chemins testés à l'aveugle si aucun lien pertinent n'a été repéré en page d'accueil.
FALLBACK_PATHS = (
    "/contact", "/contact/", "/nous-contacter", "/mentions-legales", "/mentions-legales/",
    "/equipe", "/a-propos", "/about", "/recrutement", "/carrieres", "/jobs",
)

# Extensions et sections à ne jamais suivre.
SKIP_PATTERNS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".doc", ".docx",
    ".xls", ".xlsx", ".mp4", ".mp3", ".css", ".js", "/wp-content/", "/wp-admin/",
    "/wp-json/", "/feed", "/rss", "?add-to-cart", "/panier", "/cart", "/checkout",
    "/login", "/connexion", "/wp-login", "/tag/", "/category/", "/author/",
)


def _same_site(url: str, root_host: str) -> bool:
    """Autorise le domaine et ses sous-domaines, rien d'autre."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    root = root_host.lower().removeprefix("www.")
    return host == root or host.endswith("." + root)


def score_link(url: str, anchor: str) -> int:
    """Note un lien selon sa probabilité de contenir des contacts."""
    haystack = f"{urlparse(url).path.lower()} {anchor.lower()}"
    best = 0
    for weight, keywords in PAGE_HINTS:
        if any(kw in haystack for kw in keywords):
            best = max(best, weight)
    # Une URL courte est plus souvent une page institutionnelle qu'un article.
    depth = len([p for p in urlparse(url).path.split("/") if p])
    if best and depth <= 2:
        best += 5
    return best


def harvest_links(soup: BeautifulSoup, base_url: str, root_host: str) -> list[tuple[int, str]]:
    """Renvoie les liens internes intéressants, triés par score décroissant."""
    seen: dict[str, int] = {}
    for node in soup.find_all("a", href=True):
        href = node["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        if not absolute.startswith(("http://", "https://")):
            continue
        if not _same_site(absolute, root_host):
            continue
        low = absolute.lower()
        if any(pattern in low for pattern in SKIP_PATTERNS):
            continue
        score = score_link(absolute, node.get_text(" ", strip=True)[:120])
        if score <= 0:
            continue
        seen[absolute] = max(seen.get(absolute, 0), score)
    return sorted(((s, u) for u, s in seen.items()), reverse=True)


async def crawl_site(fetcher: PoliteFetcher, domain: str, *,
                     max_pages: int = MAX_PAGES_PER_SITE) -> list[tuple[str, str]]:
    """Récupère les pages exploitables d'un site. Renvoie [(url, html), ...]."""
    root_host = domain.lower().removeprefix("www.")
    pages: list[tuple[str, str]] = []
    visited: set[str] = set()

    # Beaucoup de domaines ne répondent que sur l'un des deux hôtes : certains
    # apex sont en timeout alors que le « www » fonctionne, et inversement.
    home = None
    for host in (root_host, f"www.{root_host}"):
        for scheme in ("https", "http"):
            home = await fetcher.get_html(f"{scheme}://{host}/")
            if home:
                break
        if home:
            break
    if not home:
        log.debug("site injoignable : %s", domain)
        return []

    home_url, home_html = home
    visited.add(home_url.rstrip("/"))
    pages.append(home)

    soup = BeautifulSoup(home_html, "lxml")
    candidates = harvest_links(soup, home_url, root_host)

    # Si la page d'accueil ne mène nulle part (menu en JS, par exemple),
    # on tente les chemins conventionnels.
    if len(candidates) < 3:
        base = f"{urlparse(home_url).scheme}://{urlparse(home_url).netloc}"
        known = {u for _, u in candidates}
        candidates += [(60, base + path) for path in FALLBACK_PATHS
                       if base + path not in known]

    for _score, url in candidates:
        if len(pages) >= max_pages:
            break
        key = url.rstrip("/")
        if key in visited:
            continue
        visited.add(key)
        result = await fetcher.get_html(url)
        if result:
            pages.append(result)

    return pages


def page_tagline(soup: BeautifulSoup, limit: int = 240) -> str | None:
    """Ce que le site dit de lui-même en une ligne : titre + meta description.

    Sert à la rédaction personnalisée : c'est le seul texte « officiel » sur
    l'activité qu'on ait sans lire tout le site.
    """
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string.strip())
    for name in ("description", "og:description"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        content = (tag.get("content") or "").strip() if tag else ""
        if content and content not in parts:
            parts.append(content)
            break
    text = " — ".join(p for p in parts if p)
    text = " ".join(text.split())
    return text[:limit] or None


ABOUT_HINTS = ("a-propos", "apropos", "about", "qui-sommes", "notre-histoire", "equipe", "team",
               "societe", "entreprise", "expertise", "services", "solutions")


def page_main_text(soup: BeautifulSoup, limit: int = 1400) -> str:
    """Le texte de fond d'une page, sans menus ni pieds de page.

    Sert à comprendre l'activité et les enjeux de l'entreprise : ce que le site
    dit de lui-même, pas le titre seul.
    """
    page = BeautifulSoup(str(soup), "lxml")
    for tag in page(["script", "style", "noscript", "nav", "header", "footer", "form",
                     "iframe", "svg", "button"]):
        tag.decompose()
    root = page.find("main") or page.find("article") or page.body or page
    chunks: list[str] = []
    seen: set[str] = set()
    for text in root.stripped_strings:
        text = " ".join(text.split())
        # Les fragments courts sont des libellés de boutons ou de menus.
        if len(text) < 25 or text in seen:
            continue
        seen.add(text)
        chunks.append(text)
        if sum(len(c) for c in chunks) >= limit:
            break
    return " ".join(chunks)[:limit].strip()


def is_about_page(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(hint in path for hint in ABOUT_HINTS)
