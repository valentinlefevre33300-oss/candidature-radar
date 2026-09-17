"""Découverte d'entreprises via l'API publique « Recherche d'entreprises » (data.gouv.fr).

Source officielle adossée à la base Sirene de l'INSEE et au RNE : gratuite, sans clé,
sans anti-bot. Elle fournit le nom, l'activité, l'effectif, l'adresse et — c'est ce qui
nous intéresse le plus — les dirigeants nominatifs (donnée légalement publique).

Piège principal de cette API : le paramètre `q` cherche dans le *nom* de l'entreprise,
pas dans son secteur. Pour cibler un secteur, il faut filtrer par code NAF.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from ..config import REQUEST_TIMEOUT, SIRENE_API, USER_AGENT
from ..extract.people import strip_accents
from ..models import Company, Director, SearchQuery

log = logging.getLogger(__name__)

PER_PAGE = 25  # maximum autorisé par l'API

# Codes INSEE de tranche d'effectif -> (borne basse, borne haute, libellé)
HEADCOUNT_BANDS: dict[str, tuple[int, int, str]] = {
    "00": (0, 0, "0 salarié"),
    "01": (1, 2, "1 à 2 salariés"),
    "02": (3, 5, "3 à 5 salariés"),
    "03": (6, 9, "6 à 9 salariés"),
    "11": (10, 19, "10 à 19 salariés"),
    "12": (20, 49, "20 à 49 salariés"),
    "21": (50, 99, "50 à 99 salariés"),
    "22": (100, 199, "100 à 199 salariés"),
    "31": (200, 249, "200 à 249 salariés"),
    "32": (250, 499, "250 à 499 salariés"),
    "41": (500, 999, "500 à 999 salariés"),
    "42": (1000, 1999, "1 000 à 1 999 salariés"),
    "51": (2000, 4999, "2 000 à 4 999 salariés"),
    "52": (5000, 9999, "5 000 à 9 999 salariés"),
    "53": (10000, 10**9, "10 000 salariés et plus"),
}


def headcount_codes(min_h: int | None, max_h: int | None) -> list[str]:
    """Traduit une fourchette d'effectif en codes INSEE.

    Les entreprises dont l'effectif n'est pas renseigné (`NN`) sont volontairement
    exclues dès qu'un filtre est posé : sans filtre elles représentent l'essentiel
    du fichier et noieraient les résultats.
    """
    if min_h is None and max_h is None:
        return []
    lo = min_h if min_h is not None else 0
    hi = max_h if max_h is not None else 10**9
    return [code for code, (band_lo, band_hi, _) in HEADCOUNT_BANDS.items()
            if band_hi >= lo and band_lo <= hi]


# Qualités présentes au RNE mais sans pouvoir de décision sur un recrutement :
# les contacter n'a aucun sens pour une candidature.
IRRELEVANT_ROLES = (
    "commissaire aux comptes", "liquidateur", "mandataire", "administrateur judiciaire",
    "conciliateur", "curateur", "syndic",
    # Membres du conseil d'administration : ils siegent, ils ne recrutent pas.
    # Une SA cotee en declare une dizaine, qui noieraient tous les autres contacts.
    "censeur", "representant permanent", "membre du conseil", "membre du directoire",
    "vice-president du conseil", "administrateur delegue",
)

# « Administrateur » seul designe un membre du conseil. En revanche
# « President du conseil d'administration et directeur general » est un dirigeant
# operationnel : on ne filtre donc que la qualite exactement egale.
IRRELEVANT_EXACT = {"administrateur", "administratrice", "associe", "associee"}


def _parse_directors(raw: list[dict] | None) -> list[Director]:
    out: list[Director] = []
    for d in raw or []:
        # On ignore les personnes morales (holdings) : pas d'humain à contacter.
        if d.get("type_dirigeant") != "personne physique":
            continue
        last, first = (d.get("nom") or "").strip(), (d.get("prenoms") or "").strip()
        if not last:
            continue
        role = (d.get("qualite") or "").strip()
        normalized_role = strip_accents(role.lower()).strip()
        if any(bad in normalized_role for bad in IRRELEVANT_ROLES):
            continue
        if normalized_role in IRRELEVANT_EXACT:
            continue
        # Le registre note les noms d'usage entre parentheses :
        # « SAUNIER (MARTIN DIT NEUVILLE) ». Garde seulement le nom legal, sinon
        # la deduction produit « sauniermartinditneuville@ ».
        last = re.sub(r"\(.*?\)", "", last).strip(" -")
        first = re.sub(r"\(.*?\)", "", first).strip(" -")
        if not last:
            continue
        out.append(Director(last_name=last, first_names=first, role=role or None))
    return out


def _parse_company(raw: dict) -> Company:
    siege = raw.get("siege") or {}
    # Si un filtre géographique est actif, l'API renvoie l'établissement qui a matché.
    # C'est lui qui est pertinent (une antenne locale recrute localement), pas le siège.
    matched = (raw.get("matching_etablissements") or [None])[0] or siege
    code = raw.get("tranche_effectif_salarie")
    return Company(
        siren=raw.get("siren", ""),
        name=raw.get("nom_complet") or raw.get("nom_raison_sociale") or "",
        naf=raw.get("activite_principale"),
        city=(matched.get("libelle_commune") or "").title() or None,
        postal_code=matched.get("code_postal"),
        department=matched.get("departement"),
        headcount_code=code,
        size=HEADCOUNT_BANDS.get(code or "", (0, 0, None))[2],
        created_at=raw.get("date_creation"),
        directors=_parse_directors(raw.get("dirigeants")),
    )


async def search_companies(client: httpx.AsyncClient, query: SearchQuery) -> list[Company]:
    """Récupère jusqu'à `query.limit` entreprises correspondant aux critères."""
    params: dict[str, str] = {"per_page": str(PER_PAGE)}
    if query.naf_codes:
        params["activite_principale"] = ",".join(query.naf_codes)
    if query.keywords.strip():
        params["q"] = query.keywords.strip()
    if query.department:
        params["departement"] = query.department
    if query.postal_code:
        params["code_postal"] = query.postal_code
    if query.active_only:
        params["etat_administratif"] = "A"
    codes = headcount_codes(query.min_headcount, query.max_headcount)
    if codes:
        params["tranche_effectif_salarie"] = ",".join(codes)

    if "activite_principale" not in params and "q" not in params:
        raise ValueError("Il faut au moins un secteur (NAF) ou un mot-clé de nom d'entreprise.")

    companies: list[Company] = []
    seen: set[str] = set()
    page = 1
    while len(companies) < query.limit:
        params["page"] = str(page)
        try:
            resp = await client.get(SIRENE_API, params=params, timeout=REQUEST_TIMEOUT,
                                    headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("API entreprises injoignable (page %s) : %s", page, exc)
            break

        payload = resp.json()
        results = payload.get("results") or []
        if not results:
            break

        for raw in results:
            company = _parse_company(raw)
            if company.siren and company.siren not in seen:
                seen.add(company.siren)
                companies.append(company)
            if len(companies) >= query.limit:
                break

        if page >= int(payload.get("total_pages") or 1):
            break
        page += 1
        await asyncio.sleep(0.2)  # l'API est gratuite, on ne la martèle pas

    return companies
