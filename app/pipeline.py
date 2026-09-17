"""Orchestration d'une recherche complète.

Enchaînement pour chaque entreprise retenue :
    annuaire -> domaine -> crawl ciblé -> extraction -> déduction du motif
    d'adressage -> vérification MX -> notation.

L'étape de déduction mérite un mot : les grandes entreprises masquent leurs
adresses derrière un formulaire, mais publient souvent une ou deux adresses
nominatives (presse, mentions légales). Il suffit d'une seule pour identifier le
motif maison — « prenom.nom@ », « pnom@ »… — et reconstituer l'adresse des
dirigeants que l'annuaire nous a donnés. Ces adresses sont marquées `inferred`
et ne sont jamais présentées comme certaines.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx
from bs4 import BeautifulSoup

from .config import GLOBAL_CONCURRENCY, MAX_PAGES_PER_SITE, SMTP_PROBE
from .crawl.fetcher import PoliteFetcher, build_client
from .crawl.spider import crawl_site, page_tagline
from .extract.emails import extract_emails
from .extract.people import (
    PATTERNS,
    classify_mailbox,
    find_role_near,
    guess_name_from_local,
    infer_pattern,
    render,
)
from .models import Company, Contact, SearchQuery
from .score import dedupe_and_rank, matches_director, score_contact
from .sources.sirene import search_companies
from .verify import has_mx, smtp_accepts

log = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], Awaitable[None]] | None

PATTERN_LABELS = dict(PATTERNS)

# Au-dela de quelques dirigeants, on n'ajoute plus de valeur : on noie les
# contacts des autres entreprises sous une seule raison sociale.
MAX_INFERRED_PER_COMPANY = 3

# Qualites operationnelles, par ordre d'interet pour une candidature.
ROLE_PRIORITY = (
    "president directeur general", "directeur general", "directrice generale",
    "gerant", "gerante", "president", "presidente", "directeur", "directrice",
)


def _director_rank(role: str | None) -> int:
    """Plus la valeur est basse, plus la personne est un interlocuteur utile."""
    if not role:
        return len(ROLE_PRIORITY) + 1
    normalized = role.lower()
    for index, keyword in enumerate(ROLE_PRIORITY):
        if keyword in normalized:
            return index
    return len(ROLE_PRIORITY)


async def _emit(callback: ProgressCallback, **payload) -> None:
    if callback is not None:
        await callback(payload)


def _build_contact(email: str, obfuscated: bool, url: str, page_text: str,
                   company: Company) -> Contact:
    """Assemble un contact à partir d'une adresse et de son contexte de page."""
    first, last = guess_name_from_local(email.split("@", 1)[0])
    role_category, role_excerpt = find_role_near(page_text, email)
    mailbox = classify_mailbox(email)

    # La fonction lue autour de l'adresse n'est fiable que si l'adresse elle-meme
    # ne dit rien. Un menu de navigation contenant « Recrutement » suffisait a
    # faire passer « contact@ » pour une boite RH : le mot est proche, mais il ne
    # qualifie pas l'adresse. On ne laisse donc le contexte trancher que pour les
    # adresses nominatives ou indeterminees.
    if mailbox in ("nominatif", "inconnu"):
        category = role_category or mailbox
    else:
        category = mailbox
        if role_category != mailbox:
            role_excerpt = None  # extrait trompeur, on ne l'affiche pas

    contact = Contact(
        email=email,
        company_siren=company.siren,
        company_name=company.name,
        source_url=url,
        category=category,
        first_name=first,
        last_name=last,
        role_title=role_excerpt,
        is_nominative=bool(first and last) or mailbox == "nominatif",
        was_obfuscated=obfuscated,
    )
    contact.matched_director = matches_director(contact, company)
    return contact


def _infer_director_emails(company: Company, observed: list[Contact]) -> list[Contact]:
    """Reconstitue l'adresse des dirigeants à partir du motif maison."""
    if not company.domain or not company.directors:
        return []

    own_domain = company.domain.lower().removeprefix("www.")
    own_emails = [c.email for c in observed if c.domain == own_domain]
    if not own_emails:
        return []

    # Noms connus : dirigeants de l'annuaire + personnes déduites des adresses vues.
    people: list[tuple[str, str]] = [
        (d.first_names.split()[0] if d.first_names else "", d.last_name)
        for d in company.directors if d.last_name
    ]
    people += [(c.first_name, c.last_name) for c in observed
               if c.first_name and c.last_name]

    pattern = infer_pattern(own_emails, [(f, l) for f, l in people if f and l])
    if not pattern:
        return []

    existing = {e.lower() for e in own_emails}
    inferred: list[Contact] = []
    ranked = sorted(company.directors, key=lambda d: _director_rank(d.role))
    for director in ranked:
        if len(inferred) >= MAX_INFERRED_PER_COMPANY:
            break
        first = director.first_names.split()[0] if director.first_names else ""
        if not first or not director.last_name:
            continue
        local = render(pattern, first, director.last_name)
        if not local:
            continue
        candidate = f"{local}@{own_domain}"
        if candidate.lower() in existing:
            continue
        existing.add(candidate.lower())
        label = PATTERN_LABELS.get(pattern, pattern)
        contact = Contact(
            email=candidate,
            company_siren=company.siren,
            company_name=company.name,
            source_url=f"(deduit du motif {label})",
            category="direction",
            first_name=first.title(),
            last_name=director.last_name.title(),
            role_title=director.role,
            is_nominative=True,
            inferred=True,
            pattern_used=pattern,
            matched_director=True,
        )
        inferred.append(contact)
    return inferred


async def process_company(fetcher: PoliteFetcher, client: httpx.AsyncClient,
                          company: Company, query: SearchQuery, *,
                          use_search: bool, verify_smtp: bool,
                          callback: ProgressCallback) -> list[Contact]:
    from .resolve.domain import resolve_domain  # import tardif : evite un cycle

    await _emit(callback, event="entreprise", name=company.name,
                step="resolution du domaine")
    company = await resolve_domain(fetcher, client, company, use_search=use_search)

    if not company.domain:
        await _emit(callback, event="entreprise", name=company.name,
                    step="site introuvable", contacts=0)
        return []

    await _emit(callback, event="entreprise", name=company.name,
                step=f"exploration de {company.domain}")
    pages = await crawl_site(fetcher, company.domain, max_pages=MAX_PAGES_PER_SITE)

    observed: list[Contact] = []
    for index, (url, html) in enumerate(pages):
        soup = BeautifulSoup(html, "lxml")
        if index == 0:
            # La page d'accueil dit en une ligne ce que fait l'entreprise :
            # c'est la matiere premiere de la redaction personnalisee.
            company.tagline = page_tagline(soup)
        emails = extract_emails(soup, html)
        if not emails:
            continue
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        page_text = soup.get_text(" ", strip=True)
        for email, obfuscated in emails.items():
            observed.append(_build_contact(email, obfuscated, url, page_text, company))

    contacts = observed + _infer_director_emails(company, observed)

    # Verification MX : une seule resolution par domaine, partagee.
    domains = {c.domain for c in contacts}
    if domains:
        ordered = list(domains)
        results = await asyncio.gather(*(has_mx(d) for d in ordered))
        mx_results = dict(zip(ordered, results))
        for contact in contacts:
            contact.mx_ok = mx_results.get(contact.domain)

    if verify_smtp:
        # Uniquement sur les adresses deduites : ce sont les seules incertaines.
        targets = [c for c in contacts if c.inferred and c.mx_ok]
        if targets:
            verdicts = await asyncio.gather(*(smtp_accepts(c.email) for c in targets))
            for contact, verdict in zip(targets, verdicts):
                contact.smtp_ok = verdict

    for contact in contacts:
        score_contact(contact, company, query.job_title)

    await _emit(callback, event="entreprise", name=company.name,
                step=f"{len(contacts)} contact(s)", contacts=len(contacts),
                domain=company.domain)
    return contacts


async def run_search(query: SearchQuery, *, use_search: bool = True,
                     verify_smtp: bool = SMTP_PROBE,
                     callback: ProgressCallback = None,
                     ) -> tuple[list[Company], list[Contact]]:
    """Execute la recherche complete et renvoie (entreprises, contacts classes)."""
    async with build_client() as client:
        fetcher = PoliteFetcher(client)

        await _emit(callback, event="etape",
                    step="interrogation de l'annuaire des entreprises")
        companies = await search_companies(client, query)
        await _emit(callback, event="etape",
                    step=f"{len(companies)} entreprise(s) a explorer",
                    total=len(companies))

        if not companies:
            return [], []

        semaphore = asyncio.Semaphore(GLOBAL_CONCURRENCY)

        async def guarded(company: Company) -> list[Contact]:
            async with semaphore:
                try:
                    return await process_company(
                        fetcher, client, company, query,
                        use_search=use_search, verify_smtp=verify_smtp,
                        callback=callback)
                except Exception as exc:
                    # Une entreprise en echec ne doit pas faire tomber la recherche.
                    log.exception("echec sur %s", company.name)
                    await _emit(callback, event="erreur", name=company.name,
                                detail=str(exc)[:200])
                    return []

        harvested = await asyncio.gather(*(guarded(c) for c in companies))

    contacts = dedupe_and_rank([c for batch in harvested for c in batch])
    await _emit(callback, event="fin", total_contacts=len(contacts))
    return companies, contacts
