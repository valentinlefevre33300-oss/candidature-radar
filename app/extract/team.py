"""Lire une page « Équipe » : des noms et des fonctions, sans adresse.

Les responsables de service ne publient presque jamais leur adresse, mais
beaucoup de sites présentent l'équipe en cartes : une photo, un nom, une
fonction. C'est de là que viennent les vrais interlocuteurs ; l'adresse est
ensuite reconstituée avec le motif d'adressage de l'entreprise.

Heuristique volontairement prudente : on part d'un bloc de texte qui ressemble
à une fonction, on remonte au plus petit conteneur qui l'entoure (la « carte »),
et on y cherche un nom propre. Sans nom plausible, on n'invente rien.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from ..domains import ANY_ROLE_RE, _norm

# Mots qui ne sont jamais un prénom ou un nom, même capitalisés.
NOT_A_NAME = {
    "notre", "nos", "votre", "vos", "nous", "vous", "equipe", "team", "contact", "contactez",
    "voir", "lire", "plus", "savoir", "decouvrir", "suivez", "suivre", "mentions", "legales",
    "politique", "confidentialite", "accueil", "services", "service", "tous", "toutes", "tout",
    "site", "agence", "groupe", "societe", "entreprise", "france", "paris", "bordeaux", "lyon",
    "marseille", "toulouse", "nantes", "lille", "expert", "experts", "expertise", "solutions",
    "solution", "projet", "projets", "client", "clients", "offre", "offres", "emploi", "carriere",
    "rejoignez", "rejoindre", "candidature", "postuler", "nouveau", "nouvelle", "actualites",
    "actualite", "blog", "news", "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
    "aout", "septembre", "octobre", "novembre", "decembre", "lundi", "mardi", "mercredi", "jeudi",
    "vendredi", "samedi", "dimanche", "linkedin", "twitter", "facebook", "instagram", "email",
    "mail", "telephone", "adresse", "depuis", "avec", "sans", "pour", "chez", "the", "and",
    "our", "your", "about", "read", "more", "learn", "meet", "join", "us", "team", "member",
}
PARTICLES = {"de", "du", "des", "le", "la", "van", "von", "di", "da", "del", "della", "d'", "el", "al"}

# Un mot de nom : « Jean », « Jean-Claude », « Dumas-Ravon », « LABRUNE », « O'Neil ».
_PART = r"[A-ZÀ-ÝŒ][a-zà-ÿœ'’]{1,20}"
_WORD = rf"(?:{_PART}(?:-{_PART})?|[A-ZÀ-ÝŒ]{{2,20}}(?:-[A-ZÀ-ÝŒ]{{2,20}})?)"
NAME_RE = re.compile(rf"^{_WORD}(?:\s+(?:(?:de|du|des|le|la|van|von|di|da|del|della|el|al|d')\s+)?{_WORD}){{1,2}}$")

MAX_CARD_CHARS = 320
MAX_PEOPLE = 12


def looks_like_name(text: str, company_tokens: set[str]) -> bool:
    text = text.strip(" ,.;:-–|•·")
    if not (4 <= len(text) <= 40) or any(ch.isdigit() for ch in text):
        return False
    if not NAME_RE.match(text):
        return False
    words = [w for w in re.split(r"\s+", text) if w]
    normalized = [_norm(w).strip("'’") for w in words]
    if normalized[0] in PARTICLES or normalized[-1] in PARTICLES:
        return False
    if any(w in NOT_A_NAME or w in company_tokens for w in normalized):
        return False
    if ANY_ROLE_RE.search(" ".join(normalized)):
        return False
    return True


def looks_like_role(text: str) -> bool:
    text = text.strip()
    return 3 <= len(text) <= 90 and bool(ANY_ROLE_RE.search(_norm(text)))


def split_name(text: str) -> tuple[str, str]:
    """« Jean-Claude Labrune » -> (« Jean-Claude », « Labrune »), particules avec le nom."""
    words = text.strip(" ,.;:-–|•·").split()
    if len(words) == 2:
        return words[0], words[1]
    # « Marie de la Fontaine » : le prénom est le premier mot, le reste est le nom.
    return words[0], " ".join(words[1:])


def _leaf_texts(node: Tag) -> list[str]:
    """Textes « feuilles » d'un bloc, dans l'ordre du document."""
    out: list[str] = []
    for chunk in node.stripped_strings:
        chunk = " ".join(chunk.split())
        if chunk:
            out.append(chunk)
    return out


def _card_for(node: Tag) -> Tag | None:
    """Le plus petit ancêtre qui contienne quelques textes en plus de la fonction."""
    current = node
    for _ in range(5):
        parent = current.parent
        if parent is None or not isinstance(parent, Tag) or parent.name in ("body", "html"):
            return None
        text = parent.get_text(" ", strip=True)
        if len(text) > MAX_CARD_CHARS:
            return None
        if len(_leaf_texts(parent)) >= 2:
            return parent
        current = parent
    return None


def extract_people(soup: BeautifulSoup, company_name: str = "") -> list[tuple[str, str, str]]:
    """Renvoie [(prénom, nom, fonction), ...] trouvés dans la page."""
    company_tokens = {_norm(w) for w in re.split(r"[^\w]+", company_name) if len(w) > 2}
    page = BeautifulSoup(str(soup), "lxml")
    for tag in page(["script", "style", "noscript", "nav", "form", "footer"]):
        tag.decompose()

    found: dict[str, tuple[str, str, str]] = {}

    def keep(name: str, role: str) -> None:
        if not looks_like_name(name, company_tokens):
            return
        first, last = split_name(name)
        key = _norm(f"{first} {last}")
        if key not in found:
            found[key] = (first, last, " ".join(role.split())[:90])

    for node in page.find_all(string=True):
        text = " ".join(str(node).split())
        if not looks_like_role(text):
            continue
        parent = node.parent if isinstance(node.parent, Tag) else None
        if parent is None:
            continue

        # « Jean Dupont, Directeur technique » sur une seule ligne.
        for sep in (" — ", " – ", " - ", ", ", " | ", " · ", " : "):
            if sep in text:
                left, right = text.split(sep, 1)
                if looks_like_role(right) and looks_like_name(left, company_tokens):
                    keep(left, right)
                    break
        else:
            card = _card_for(parent)
            if card is None:
                continue
            leaves = _leaf_texts(card)
            try:
                index = leaves.index(text)
            except ValueError:
                index = len(leaves)
            # Le nom précède presque toujours la fonction ; on regarde juste avant,
            # puis juste après.
            for candidate in leaves[max(0, index - 2):index][::-1] + leaves[index + 1:index + 2]:
                if looks_like_name(candidate, company_tokens):
                    keep(candidate, text)
                    break
        if len(found) >= MAX_PEOPLE:
            break

    return list(found.values())
