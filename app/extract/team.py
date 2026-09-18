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
from ..resolve import linkedin
from .names import is_first_name

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

# Ce qui suit une virgule dans une fonction et n'est pas une autre entreprise :
# formes juridiques, groupes, et lieux (« Head of Product, France »).
ORG_NOISE = {
    "groupe", "group", "inc", "sas", "sa", "sarl", "sasu", "eurl", "ltd", "llc", "gmbh", "ag",
    "the", "and", "france", "paris", "bordeaux", "lyon", "toulouse", "nantes", "lille", "marseille",
    "europe", "emea", "monde", "world", "international", "global", "region", "sud", "ouest",
    "nord", "est", "nouvelle", "aquitaine", "idf", "ile", "siege", "hq",
}
_ORG_SEP_RE = re.compile(r"\s*(?:,|;|\|| chez | at | @ | – | — | - )\s*", re.I)


def looks_like_name(text: str, company_tokens: set[str], *, require_first_name: bool = True) -> bool:
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
    # La forme ne suffit pas : « Life Sciences » ou « Php Symfony » ont l'air
    # d'un nom. Le premier mot doit être un prénom connu.
    if require_first_name and not is_first_name(words[0]):
        return False
    return True


def is_external(role: str, company_tokens: set[str]) -> bool:
    """« Digital Product Manager, Dunlop Protective Footwear » : un client cité en
    témoignage, pas un salarié de l'entreprise explorée.

    Une fonction suivie d'un nom d'organisation étranger à l'entreprise désigne
    quelqu'un d'ailleurs. « DRH, groupe Nomios » sur le site de Nomios reste
    interne ; « Head of Product, France » aussi.
    """
    parts = [p for p in _ORG_SEP_RE.split(role) if p and p.strip()]
    if len(parts) < 2:
        return False
    org = parts[-1].strip(" .")
    words = org.split()
    if not org or not (1 <= len(words) <= 6) or not words[0][:1].isupper():
        return False
    if looks_like_role(org):
        return False
    tokens = {_norm(w) for w in re.split(r"[^\w]+", org) if len(w) > 2} - ORG_NOISE
    return bool(tokens) and not (tokens & company_tokens)


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


def _linked_profile(card: Tag | None, first: str, last: str) -> bool:
    """La carte contient-elle un lien vers un profil LinkedIn portant ce nom ?"""
    if card is None:
        return False
    return any(linkedin.matches(url, first, last) for url in linkedin.profile_links(card))


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


def company_tokens_for(company_name: str, domain: str | None = None) -> set[str]:
    """Les mots de la raison sociale et du domaine, pour reconnaître l'entreprise
    quand elle se cite elle-même (« DRH, groupe Nomios »)."""
    tokens = {_norm(w) for w in re.split(r"[^\w]+", company_name) if len(w) > 2}
    if domain:
        label = domain.lower().removeprefix("www.").split(".")[0]
        if len(label) > 2:
            tokens.add(label)
    return tokens


def extract_people(soup: BeautifulSoup, company_name: str = "",
                   domain: str | None = None) -> list[tuple[str, str, str]]:
    """Renvoie [(prénom, nom, fonction), ...] trouvés dans la page.

    Une personne n'est retenue que si son prénom est connu — ou, à défaut, si la
    carte qui la présente pointe vers un profil LinkedIn à son nom : la preuve
    qu'il s'agit bien de quelqu'un.
    """
    company_tokens = company_tokens_for(company_name, domain)
    page = BeautifulSoup(str(soup), "lxml")
    for tag in page(["script", "style", "noscript", "nav", "form", "footer"]):
        tag.decompose()

    found: dict[str, tuple[str, str, str]] = {}

    def keep(name: str, role: str, card: Tag | None) -> None:
        if not looks_like_name(name, company_tokens, require_first_name=False):
            return
        first, last = split_name(name)
        if not is_first_name(first) and not _linked_profile(card, first, last):
            return
        if is_external(role, company_tokens):
            return
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
                if looks_like_role(right) and looks_like_name(left, company_tokens, require_first_name=False):
                    keep(left, right, parent)
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
                if looks_like_name(candidate, company_tokens, require_first_name=False):
                    keep(candidate, text, card)
                    break
        if len(found) >= MAX_PEOPLE:
            break

    return list(found.values())
