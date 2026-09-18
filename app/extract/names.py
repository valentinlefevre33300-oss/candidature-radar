"""Un prénom connu, ou pas : ce qui sépare une personne d'un titre de rubrique.

L'extraction de l'équipe repose sur des heuristiques de forme (deux mots
capitalisés près d'une fonction). Elles laissent passer « Life Sciences »,
« Php Symfony » ou « Architecture Mach » : des intitulés de pages, jamais
des gens. Le seul garde-fou solide est un dictionnaire de prénoms : ~44 000
prénoms d'Europe, du Maghreb, d'Inde, d'Asie et d'Amérique (base
firstname-database, licence GNU FDL), normalisés sans accent ni majuscule.
"""
from __future__ import annotations

import gzip
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_RESOURCE = Path(__file__).resolve().parent.parent / "resources" / "first_names.txt.gz"
_TOKEN_RE = re.compile(r"[a-z][a-z'-]{1,24}")


def _norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    flat = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    return flat.replace("’", "'").strip(" .'-")


@lru_cache(maxsize=1)
def known_first_names() -> frozenset[str]:
    with gzip.open(_RESOURCE, "rt", encoding="utf-8") as handle:
        return frozenset(line.strip() for line in handle if line.strip())


def is_first_name(value: str | None) -> bool:
    """« Jean-Claude », « Isaure », « Sunil » : oui. « Life », « Php », « Previous » : non.

    Un prénom composé inconnu en bloc passe si chacune de ses parties est un
    prénom (« Marie-Laure »).
    """
    token = _norm(value or "")
    if not _TOKEN_RE.fullmatch(token):
        return False
    known = known_first_names()
    if token in known:
        return True
    parts = [p for p in token.split("-") if p]
    return len(parts) > 1 and all(p in known for p in parts)
