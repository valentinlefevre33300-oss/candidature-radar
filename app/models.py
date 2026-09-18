"""Structures de données partagées par tout le pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Director:
    """Dirigeant déclaré au registre du commerce (donnée publique, API Sirene)."""
    last_name: str
    first_names: str
    role: str | None = None

    @property
    def display(self) -> str:
        return f"{self.first_names.title()} {self.last_name.title()}".strip()


@dataclass
class Company:
    siren: str
    name: str
    naf: str | None = None
    city: str | None = None
    postal_code: str | None = None
    department: str | None = None
    size: str | None = None            # libellé de tranche d'effectif
    headcount_code: str | None = None  # code INSEE brut
    created_at: str | None = None
    directors: list[Director] = field(default_factory=list)
    domain: str | None = None
    domain_confidence: float = 0.0
    domain_method: str | None = None   # comment le domaine a été trouvé
    tagline: str | None = None         # titre + description de la page d'accueil
    about: str | None = None           # texte utile du site (accueil, à propos), pour comprendre l'activité

    @property
    def slug_source(self) -> str:
        """Nom nettoyé des suffixes juridiques, utile pour deviner le domaine."""
        return self.name

    @classmethod
    def from_dict(cls, data: dict) -> "Company":
        """Reconstruit une entreprise telle que l'interface la renvoie (choix à la main)."""
        known = {f.name for f in fields(cls)}
        raw = {k: v for k, v in data.items() if k in known and k != "directors"}
        raw["directors"] = [Director(last_name=str(d.get("last_name") or ""), first_names=str(d.get("first_names") or ""),
                                     role=d.get("role"))
                            for d in (data.get("directors") or []) if isinstance(d, dict)]
        return cls(**raw)


@dataclass
class Contact:
    """Un email trouvé, rattaché à une entreprise."""
    email: str
    company_siren: str
    company_name: str
    source_url: str
    category: str = "inconnu"          # rh | direction | metier | generique | technique
    first_name: str | None = None
    last_name: str | None = None
    role_title: str | None = None      # intitulé repéré à proximité dans la page
    is_nominative: bool = False        # prenom.nom@ vs contact@
    was_obfuscated: bool = False       # "nom [at] domaine" décodé
    inferred: bool = False             # reconstituée depuis le motif d'adressage,
                                       # jamais vue telle quelle sur le site
    pattern_used: str | None = None    # motif appliqué, si inferred
    matched_director: bool = False     # correspond à un dirigeant Sirene
    is_manager: bool = False           # dirige une équipe (responsable, head of, lead…)
    mx_ok: bool | None = None
    smtp_ok: bool | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    found_at: str = field(default_factory=_now)

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower()

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["reasons"] = " | ".join(self.reasons)
        return row


@dataclass
class SearchQuery:
    """Ce que l'utilisateur saisit dans l'UI."""
    job_title: str                      # "développeur python", sert au scoring métier
    keywords: str = ""                  # secteur/activité recherché côté Sirene
    department: str | None = None
    postal_code: str | None = None
    cities: list[dict] = field(default_factory=list)   # communes choisies (code INSEE, nom, centre)
    agglomeration: bool = False                        # etendre chaque commune a son intercommunalite (EPCI)
    naf_codes: list[str] = field(default_factory=list)
    min_headcount: int | None = None
    max_headcount: int | None = None
    limit: int = 40                     # nombre d'entreprises à traiter
    active_only: bool = True

