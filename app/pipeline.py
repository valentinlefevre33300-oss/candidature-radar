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
from .crawl.spider import crawl_site, is_about_page, page_main_text, page_tagline
from .domains import classify_role, is_manager, job_domain
from .extract.emails import extract_emails
from .extract.team import extract_people
from .resolve import linkedin
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
                   company: Company, domain: str | None = None) -> Contact:
    """Assemble un contact à partir d'une adresse et de son contexte de page."""
    first, last = guess_name_from_local(email.split("@", 1)[0])
    role_category, role_excerpt = find_role_near(page_text, email, domain)
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
    contact.is_manager = bool(role_excerpt) and is_manager(role_excerpt, domain)
    return contact


SMALL_COMPANY = {"00", "01", "02", "03", "11", "12"}
MAX_PEOPLE_PER_COMPANY = 6
GUESSED_PATTERN = "{first}.{last}"   # le motif le plus repandu en France


def _address_pattern(company: Company, observed: list[Contact],
                     extra_people: list[tuple[str, str]]) -> tuple[str | None, bool]:
    """(motif d'adressage, suppose ?) pour l'entreprise.

    Deduit des adresses nominatives observees quand il y en a. Sinon on suppose
    « prenom.nom », le plus courant, en le disant : l'adresse sera marquee et
    classee derriere celles qui reposent sur une preuve.
    """
    if not company.domain:
        return None, False
    own_domain = company.domain.lower().removeprefix("www.")
    own_emails = [c.email for c in observed if c.domain == own_domain]
    people: list[tuple[str, str]] = [
        (d.first_names.split()[0] if d.first_names else "", d.last_name)
        for d in company.directors if d.last_name
    ]
    people += [(c.first_name, c.last_name) for c in observed if c.first_name and c.last_name]
    people += extra_people
    if own_emails:
        pattern = infer_pattern(own_emails, [(f, l) for f, l in people if f and l])
        if pattern:
            return pattern, False
    return GUESSED_PATTERN, True


def _inferred_contact(company: Company, email: str, first: str, last: str, role: str | None,
                      category: str, manager: bool, pattern: str, guessed: bool,
                      source: str, director: bool) -> Contact:
    contact = Contact(
        email=email,
        company_siren=company.siren,
        company_name=company.name,
        source_url=source,
        category=category,
        first_name=first.title(),
        last_name=last.title(),
        role_title=role,
        is_nominative=True,
        inferred=True,
        pattern_used=pattern + ("?" if guessed else ""),
        matched_director=director,
        is_manager=manager,
    )
    return contact


def _infer_director_emails(company: Company, observed: list[Contact], domain: str | None,
                           pattern: str | None, guessed: bool) -> list[Contact]:
    """Reconstitue l'adresse des dirigeants de l'annuaire."""
    if not pattern or not company.domain or not company.directors:
        return []
    # Sans motif prouve, on ne devine l'adresse d'un dirigeant que dans une petite
    # structure : ailleurs il ne lit pas ses mails, et le rebond ne vaut pas le risque.
    if guessed and company.headcount_code not in SMALL_COMPANY:
        return []
    own_domain = company.domain.lower().removeprefix("www.")
    existing = {c.email.lower() for c in observed}
    label = PATTERN_LABELS.get(pattern, pattern) + (" suppose" if guessed else "")
    inferred: list[Contact] = []
    for director in sorted(company.directors, key=lambda d: _director_rank(d.role)):
        if len(inferred) >= MAX_INFERRED_PER_COMPANY:
            break
        first = director.first_names.split()[0] if director.first_names else ""
        if not first or not director.last_name:
            continue
        local = render(pattern, first, director.last_name)
        candidate = f"{local}@{own_domain}" if local else ""
        if not candidate or candidate.lower() in existing:
            continue
        existing.add(candidate.lower())
        category, _ = classify_role(director.role, domain)
        inferred.append(_inferred_contact(
            company, candidate, first, director.last_name, director.role,
            category or "direction", True, pattern, guessed,
            f"(deduit du motif {label})", director=True))
    return inferred


def _infer_people_emails(company: Company, observed: list[Contact],
                         people: list[tuple[str, str, str, str]], domain: str | None,
                         pattern: str | None, guessed: bool) -> list[Contact]:
    """Adresses des personnes lues sur les pages Equipe : nom, fonction, page.

    Seules les fonctions qui comptent pour une candidature sont retenues (metier
    vise, direction, RH), les responsables d'abord. Un comptable trouve sur la
    page equipe n'interesse pas un developpeur.
    """
    if not pattern or not company.domain or not people:
        return []
    own_domain = company.domain.lower().removeprefix("www.")
    existing = {c.email.lower() for c in observed}
    rank = {"metier": 0, "direction": 1, "rh": 2}
    candidates = []
    for first, last, role, url in people:
        category, manager = classify_role(role, domain)
        if category not in rank:
            continue
        # Meme regle que pour l'annuaire : sans motif prouve, on ne devine pas
        # l'adresse d'un dirigeant hors petite structure. Le responsable du metier
        # vise, lui, vaut le risque d'un rebond.
        if guessed and category == "direction" and company.headcount_code not in SMALL_COMPANY:
            continue
        candidates.append((rank[category], 0 if manager else 1, first, last, role, url,
                           category, manager))
    label = PATTERN_LABELS.get(pattern, pattern) + (" suppose" if guessed else "")
    inferred: list[Contact] = []
    for _r, _m, first, last, role, url, category, manager in sorted(candidates):
        if len(inferred) >= MAX_PEOPLE_PER_COMPANY:
            break
        local = render(pattern, first, last)
        candidate = f"{local}@{own_domain}" if local else ""
        if not candidate or candidate.lower() in existing:
            continue
        existing.add(candidate.lower())
        contact = _inferred_contact(company, candidate, first, last, role, category, manager,
                                    pattern, guessed, url, director=False)
        contact.matched_director = matches_director(contact, company)
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

    domain = job_domain(query.job_title)
    observed: list[Contact] = []
    people: list[tuple[str, str, str, str]] = []   # (prenom, nom, fonction, page)
    profile_links: list[str] = []                   # profils LinkedIn lies depuis le site
    about_parts: list[str] = []
    for index, (url, html) in enumerate(pages):
        soup = BeautifulSoup(html, "lxml")
        if index == 0:
            # La page d'accueil dit en une ligne ce que fait l'entreprise, et son
            # texte de fond dit pour qui et comment : matiere premiere de la
            # fiche « enjeux » et de la redaction personnalisee.
            company.tagline = page_tagline(soup)
            about_parts.append(page_main_text(soup, 1400))
        elif is_about_page(url) and len(about_parts) < 3:
            about_parts.append(page_main_text(soup, 800))
        # Les pages Equipe donnent des noms et des fonctions sans adresse :
        # c'est la que se trouvent les responsables de service.
        for first, last, role in extract_people(soup, company.name):
            people.append((first, last, role, url))
        profile_links.extend(u for u in linkedin.profile_links(soup) if u not in profile_links)
        emails = extract_emails(soup, html)
        if not emails:
            continue
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        page_text = soup.get_text(" ", strip=True)
        for email, obfuscated in emails.items():
            observed.append(_build_contact(email, obfuscated, url, page_text, company, domain))

    company.about = " \n".join(part for part in about_parts if part)[:2400] or None
    pattern, guessed = _address_pattern(company, observed, [(p[0], p[1]) for p in people])
    contacts = (observed
                + _infer_director_emails(company, observed, domain, pattern, guessed)
                + _infer_people_emails(company, observed, people, domain, pattern, guessed))
    linkedin.attach(contacts, profile_links)   # profils liés depuis le site, portant le nom

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
                     companies: list[Company] | None = None,
                     ) -> tuple[list[Company], list[Contact]]:
    """Execute la recherche complete et renvoie (entreprises, contacts classes).

    `companies` : entreprises choisies a la main dans l'interface ; l'annuaire
    n'est alors pas interroge, on explore exactement celles-la.
    """
    async with build_client() as client:
        fetcher = PoliteFetcher(client)

        if companies is None:
            await _emit(callback, event="etape",
                        step="interrogation de l'annuaire des entreprises")
            companies = await search_companies(client, query)
        else:
            companies = list(companies)
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
