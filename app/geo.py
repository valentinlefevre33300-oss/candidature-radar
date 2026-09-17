"""Villes : autocomplétion et coordonnées, via l'API géographique de l'État.

`geo.api.gouv.fr` est gratuite, sans clé, et connaît chaque commune avec son
code INSEE, son département, ses codes postaux et son centre — tout ce qu'il
faut pour cibler l'annuaire des entreprises par ville ou par rayon.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import REQUEST_TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

GEO_API = "https://geo.api.gouv.fr/communes"


@dataclass
class City:
    code: str            # code INSEE de la commune
    name: str
    department: str
    lat: float
    lon: float
    population: int = 0
    postal_codes: list[str] | None = None
    epci: str | None = None   # intercommunalité (« Bordeaux Métropole ») : l'agglomération

    @property
    def label(self) -> str:
        return f"{self.name} ({self.department})"

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "department": self.department,
                "lat": self.lat, "lon": self.lon, "population": self.population,
                "postal_codes": self.postal_codes or [], "epci": self.epci}

    @classmethod
    def from_dict(cls, raw: dict) -> "City":
        return cls(code=str(raw.get("code") or ""), name=str(raw.get("name") or ""),
                   department=str(raw.get("department") or ""),
                   lat=float(raw.get("lat") or 0), lon=float(raw.get("lon") or 0),
                   population=int(raw.get("population") or 0),
                   postal_codes=list(raw.get("postal_codes") or []),
                   epci=raw.get("epci") or None)


def _parse(raw: dict) -> City | None:
    centre = (raw.get("centre") or {}).get("coordinates") or []
    if len(centre) != 2 or not raw.get("code"):
        return None
    return City(code=raw["code"], name=raw.get("nom") or "", department=raw.get("codeDepartement") or "",
                lat=float(centre[1]), lon=float(centre[0]), population=int(raw.get("population") or 0),
                postal_codes=list(raw.get("codesPostaux") or []), epci=raw.get("codeEpci") or None)


async def suggest_cities(client: httpx.AsyncClient, query: str, limit: int = 6) -> list[City]:
    """Communes dont le nom commence par `query`, les plus peuplées d'abord."""
    query = query.strip()
    if len(query) < 2:
        return []
    params = {"nom": query, "fields": "code,nom,codeDepartement,codesPostaux,population,centre,codeEpci",
              "boost": "population", "limit": str(limit)}
    try:
        resp = await client.get(GEO_API, params=params, timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("API géographique injoignable : %s", exc)
        return []
    cities = [_parse(raw) for raw in resp.json()]
    return [c for c in cities if c is not None]


def describe(cities: list[City] | list[dict], radius_km: int | None) -> str:
    """« Bordeaux, Mérignac · 35 km » — pour les en-têtes et l'historique."""
    names = [c.name if isinstance(c, City) else str(c.get("name") or "") for c in cities]
    names = [n for n in names if n]
    if not names:
        return ""
    shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    return f"{shown} · {radius_km} km" if radius_km else shown
