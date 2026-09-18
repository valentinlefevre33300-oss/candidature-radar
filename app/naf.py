"""Catalogue de secteurs -> codes NAF.

L'API entreprises ne sait pas chercher « une boîte qui fait du logiciel » : son
paramètre texte porte sur la *raison sociale*. Le ciblage sectoriel passe donc
obligatoirement par les codes d'activité NAF, que personne ne connaît par cœur.
Cette table fait la traduction pour l'interface.
"""
from __future__ import annotations

SECTORS: dict[str, dict[str, object]] = {
    "tech": {
        "label": "Tech / Logiciel / IT",
        "codes": ["62.01Z", "62.02A", "62.02B", "62.03Z", "62.09Z",
                  "63.11Z", "63.12Z", "58.29A", "58.29B", "58.29C", "58.21Z"],
    },
    "data_ia": {
        "label": "Data / IA / Hébergement",
        "codes": ["63.11Z", "62.01Z", "72.19Z", "71.12B"],
    },
    "conseil": {
        "label": "Conseil / Stratégie",
        "codes": ["70.22Z", "70.10Z", "71.12B", "74.90B"],
    },
    "marketing": {
        "label": "Marketing / Communication / Publicité",
        "codes": ["73.11Z", "73.12Z", "73.20Z", "70.21Z", "63.91Z"],
    },
    "design": {
        "label": "Design / Création / Audiovisuel",
        "codes": ["74.10Z", "74.20Z", "59.11A", "59.11B", "59.11C", "90.03A", "18.13Z"],
    },
    "ingenierie": {
        "label": "Ingénierie / Bureau d'études",
        "codes": ["71.12B", "71.20B", "71.11Z", "72.19Z"],
    },
    "industrie": {
        "label": "Industrie / Production",
        "codes": ["25.62B", "26.51B", "27.11Z", "28.99B", "29.10Z", "30.30Z",
                  "22.29A", "25.11Z", "33.20A"],
    },
    "btp": {
        "label": "BTP / Construction / Architecture",
        "codes": ["41.20A", "41.20B", "41.10A", "42.99Z", "43.21A", "43.22A",
                  "43.22B", "43.99C", "71.11Z"],
    },
    "sante": {
        "label": "Santé / Pharma / Biotech",
        "codes": ["86.10Z", "86.21Z", "86.90F", "21.20Z", "32.50A", "72.11Z"],
    },
    "finance": {
        "label": "Banque / Assurance / Finance",
        "codes": ["64.19Z", "64.20Z", "64.30Z", "65.11Z", "65.12Z", "66.19A",
                  "66.22Z", "64.99Z"],
    },
    "comptabilite": {
        "label": "Comptabilité / Audit / Juridique",
        "codes": ["69.20Z", "69.10Z"],
    },
    "rh": {
        "label": "RH / Recrutement / Intérim",
        "codes": ["78.10Z", "78.20Z", "78.30Z", "70.22Z", "85.59A"],
    },
    "formation": {
        "label": "Éducation / Formation",
        "codes": ["85.59A", "85.59B", "85.42Z", "85.32Z", "85.31Z"],
    },
    "media": {
        "label": "Médias / Édition / Presse",
        "codes": ["58.11Z", "58.13Z", "58.14Z", "60.20A", "60.10Z", "59.13A"],
    },
    "commerce": {
        "label": "Commerce / E-commerce / Retail",
        "codes": ["47.91A", "47.91B", "46.19B", "46.90Z", "47.11F", "47.19B"],
    },
    "logistique": {
        "label": "Transport / Logistique",
        "codes": ["49.41A", "49.41B", "52.10B", "52.29A", "52.29B", "53.20Z"],
    },
    "immobilier": {
        "label": "Immobilier / Promotion",
        "codes": ["68.20A", "68.20B", "68.31Z", "68.10Z", "41.10A"],
    },
    "energie": {
        "label": "Énergie / Environnement",
        "codes": ["35.11Z", "35.13Z", "38.11Z", "38.32Z", "39.00Z", "43.21A"],
    },
    "agro": {
        "label": "Agroalimentaire",
        "codes": ["10.71C", "10.13B", "10.51A", "11.02A", "10.89Z"],
    },
    "hotellerie": {
        "label": "Hôtellerie / Restauration / Tourisme",
        "codes": ["55.10Z", "56.10A", "56.10C", "79.11Z", "79.12Z"],
    },
}


# Secteurs où un métier a des chances d'exister. Un product owner ne travaille pas
# dans le BTP ni l'immobilier ; un développeur, rarement dans l'hôtellerie.
DOMAIN_SECTORS: dict[str, list[str]] = {
    "produit": ["tech", "data_ia", "design", "marketing", "media", "commerce", "finance"],
    "tech": ["tech", "data_ia", "conseil", "ingenierie", "media", "finance", "commerce", "sante"],
    "design": ["design", "marketing", "media", "tech", "commerce"],
    "marketing": ["marketing", "media", "commerce", "tech", "design", "formation"],
    "projet": ["tech", "data_ia", "conseil", "ingenierie", "marketing", "media"],
}


# En dessous de cet effectif, le métier n'existe pas dans l'entreprise : inutile
# d'y chercher un interlocuteur, et encore moins de le compter.
DOMAIN_MIN_HEADCOUNT: dict[str, int] = {
    "produit": 10, "projet": 10, "rh": 10, "finance": 10, "juridique": 10, "ops": 10,
    "tech": 3, "design": 3, "marketing": 3, "commercial": 3, "support": 3,
}


def min_headcount_for(job_title: str) -> int:
    """Effectif minimum où le poste visé a un sens (1 = pas d'avis)."""
    from .domains import job_domain
    return DOMAIN_MIN_HEADCOUNT.get(job_domain(job_title or "") or "", 1)


def apply_floor(min_h: int | None, max_h: int | None, job_title: str) -> tuple[int | None, int | None]:
    """Applique le plancher du poste à une fourchette d'effectif.

    Une fourchette entièrement sous le plancher (« 1–9 » pour un product owner)
    devient « à partir du plancher » : sans ça, minimum > maximum annulait tout
    filtre et ramenait les cent mille micro-entreprises.
    """
    floor = min_headcount_for(job_title)
    lo = max(min_h or 0, floor) or None
    hi = None if (max_h is not None and lo is not None and max_h < lo) else max_h
    return lo, hi


def relevant_sectors(job_title: str) -> list[str] | None:
    """Les secteurs à présélectionner pour ce poste ; None = pas d'avis (tout)."""
    from .domains import job_domain   # import tardif : domains importe naf
    domain = job_domain(job_title or "")
    return list(DOMAIN_SECTORS[domain]) if domain in DOMAIN_SECTORS else None


def codes_for(sector_keys: list[str]) -> list[str]:
    """Agrège les codes NAF de plusieurs secteurs, sans doublon."""
    out: list[str] = []
    for key in sector_keys:
        for code in SECTORS.get(key, {}).get("codes", []):  # type: ignore[union-attr]
            if code not in out:
                out.append(code)
    return out


def catalogue() -> list[dict[str, object]]:
    """Forme sérialisable pour l'interface web."""
    return [{"key": k, "label": v["label"], "codes": v["codes"]} for k, v in SECTORS.items()]


def sector_for_code(code: str | None) -> str | None:
    """Clé du premier secteur qui contient ce code NAF."""
    if not code:
        return None
    for key, value in SECTORS.items():
        if code in value["codes"]:
            return key
    return None


def label_for_code(code: str | None) -> str | None:
    """Libellé lisible du premier secteur qui contient ce code NAF."""
    if not code:
        return None
    for value in SECTORS.values():
        if code in value["codes"]:
            return str(value["label"]).split(" / ")[0]
    return None
