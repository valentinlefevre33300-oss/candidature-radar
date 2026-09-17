"""Extraction d'adresses email dans une page HTML.

Les sites vitrines protègent presque toujours leurs adresses. Ce module gère les
quatre techniques rencontrées en pratique :
  1. `mailto:` (le cas facile)
  2. l'obfuscation textuelle : « nom [at] domaine [dot] fr »
  3. l'obfuscation Cloudflare (`data-cfemail`, XOR hexadécimal)
  4. les entités HTML (`&#64;`) décodées en amont par BeautifulSoup
"""
from __future__ import annotations

import html
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup

# Volontairement un peu stricte : le TLD fait 2 caractères minimum et on refuse
# les points consécutifs, ce qui élimine déjà beaucoup de faux positifs.
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}\b"
)

# « contact (at) boite (dot) fr », « contact AT boite DOT fr », « contact[arobase]... »
#
# Le séparateur DOIT être délimité : soit entre crochets/parenthèses, soit entouré
# d'espaces. Sans cette contrainte, « internATional » produit « intern@ional » et
# « candidATure » produit « candid@ure » — la regex découpe les mots français.
_AT_SEP = r"(?:\s*[\[\(\{]\s*(?:@|at|arobase|chez)\s*[\]\)\}]\s*|\s+(?:at|arobase|chez|@)\s+)"
_DOT_SEP = r"(?:\s*[\[\(\{]\s*(?:\.|dot|point)\s*[\]\)\}]\s*|\s+(?:dot|point)\s+|\.)"

OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+-]{1,64})"
    + _AT_SEP
    + r"((?:[A-Za-z0-9-]{1,63}" + _DOT_SEP + r"){1,3}[A-Za-z]{2,24})",
    re.IGNORECASE,
)

# Domaines de premier niveau acceptés pour une adresse *reconstruite*. Sur ces
# matches la probabilité de faux positif est élevée, donc on n'accepte qu'une
# liste connue. Les adresses trouvées en clair ne passent pas par ce filtre.
KNOWN_TLDS = {
    "fr", "com", "net", "org", "eu", "io", "co", "dev", "app", "info", "biz",
    "tech", "agency", "studio", "design", "digital", "solutions", "consulting",
    "group", "online", "site", "shop", "store", "pro", "name", "tv", "cc", "xyz",
    "media", "immo", "paris", "bzh", "alsace", "corsica", "bio", "cloud", "email",
    "be", "ch", "lu", "ca", "uk", "de", "es", "it", "nl", "pt", "us", "ie", "at",
    "dk", "se", "no", "fi", "pl", "cz", "gr", "ro", "hu", "ma", "tn", "dz", "sn",
    "ci", "re", "gp", "mq", "gf", "yt", "pm", "nc", "pf", "wf", "tf", "mc", "ad",
    "ai", "me", "sh", "gg", "je", "int", "edu", "gov", "asso", "tm", "coop", "mobi",
}

# Extensions de fichiers : « logo@2x.png », « sprite@3x.svg » matchent la regex email.
FILE_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|avif|ico|css|js|mjs|json|xml|pdf|zip|woff2?|ttf|eot|mp4|webm)$",
    re.IGNORECASE,
)

# Domaines qui ne sont jamais un contact réel : exemples de doc, outils tiers,
# trackers et plateformes injectant leur propre adresse dans le HTML.
JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "email.com",
    "votredomaine.com", "votresite.com", "monsite.com", "adresse.com",
    "sentry.io", "wixpress.com", "wix.com", "squarespace.com", "shopify.com",
    "godaddy.com", "cloudflare.com", "jquery.com", "gravatar.com", "w3.org",
    "schema.org", "googleapis.com", "gstatic.com", "fontawesome.com",
    "sentry.wixpress.com", "2x.png", "3x.png",
    # placeholders de formulaires
    "exemple.fr", "exemple.com", "monentreprise.fr", "votreentreprise.fr",
    "mail.com", "email.fr", "test.com", "test.fr", "acme.com",
}

# Parties locales utilisées comme exemple de saisie dans les formulaires.
PLACEHOLDER_LOCALS = {
    "nom", "prenom", "prenom.nom", "votrenom", "votre-nom", "votreemail",
    "votre-email", "votre.email", "email", "adresse", "john.doe", "jane.doe",
    "johndoe", "janedoe", "utilisateur", "exemple", "example", "monemail",
}

# Préfixes techniques : boîtes automatiques ou juridiques, sans humain derrière.
NEVER_CONTACT_LOCALS = {
    "noreply", "no-reply", "ne-pas-repondre", "nepasrepondre", "donotreply",
    "postmaster", "abuse", "webmaster", "hostmaster", "mailer-daemon",
    "unsubscribe", "desabonnement", "bounce", "notification", "notifications",
}


def _clean_obfuscated(local: str, domain_blob: str) -> str | None:
    """Reconstruit « boite  (dot) fr » en « boite.fr »."""
    domain = re.sub(r"\s*(?:\[|\(|\{)?\s*(?:dot|point)\s*(?:\]|\)|\})?\s*", ".", domain_blob,
                    flags=re.IGNORECASE)
    domain = re.sub(r"\s+", "", domain).strip(".")
    domain = re.sub(r"\.{2,}", ".", domain)
    if "." not in domain or len(domain) < 4:
        return None
    return f"{local.strip()}@{domain}".lower()


def decode_cloudflare(hex_blob: str) -> str | None:
    """Décode un `data-cfemail`. Le 1er octet est la clé XOR des suivants."""
    try:
        data = bytes.fromhex(hex_blob.strip())
    except ValueError:
        return None
    if len(data) < 3:
        return None
    key, out = data[0], []
    for byte in data[1:]:
        out.append(chr(byte ^ key))
    candidate = "".join(out)
    return candidate if EMAIL_RE.fullmatch(candidate) else None


def is_plausible(email: str, *, reconstructed: bool = False) -> bool:
    """Écarte les faux positifs structurels (fichiers, domaines de démo, robots).

    `reconstructed=True` pour une adresse issue d'une désobfuscation : on applique
    alors une liste blanche de TLD, car ces matches sont bien plus bruités.
    """
    email = email.lower()
    if email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or len(email) > 254:
        return False
    if FILE_EXT_RE.search(domain) or FILE_EXT_RE.search(email):
        return False
    if domain in JUNK_DOMAINS or any(domain.endswith("." + j) for j in JUNK_DOMAINS):
        return False
    if local in NEVER_CONTACT_LOCALS or local in PLACEHOLDER_LOCALS:
        return False
    # Une suite hexadécimale longue est un identifiant (clé Sentry, hash), pas un nom.
    if len(local) >= 24 and re.fullmatch(r"[0-9a-f]+", local):
        return False
    if domain.split(".")[-1].isdigit():  # adresse IP
        return False
    if reconstructed and domain.rsplit(".", 1)[-1] not in KNOWN_TLDS:
        return False
    return True


def extract_emails(soup: BeautifulSoup, raw_html: str) -> dict[str, bool]:
    """Renvoie {email: était_obfusqué} pour une page donnée.

    `raw_html` sert au repli texte brut ; `soup` donne accès aux attributs.
    """
    found: dict[str, bool] = {}

    def add(email: str | None, obfuscated: bool) -> None:
        if not email:
            return
        email = html.unescape(email).strip().strip(".,;:()<>\"'").lower()
        if EMAIL_RE.fullmatch(email) and is_plausible(email, reconstructed=obfuscated):
            # Une trouvaille en clair prime sur une trouvaille obfusquée.
            found[email] = found.get(email, True) and obfuscated

    # 1. liens mailto:
    for node in soup.select('a[href^="mailto:"], a[href^="MAILTO:"]'):
        href = unquote(node.get("href", ""))
        address = href.split(":", 1)[-1].split("?", 1)[0]
        for part in re.split(r"[,;]", address):
            add(part.strip(), obfuscated=False)

    # 2. protection Cloudflare
    for node in soup.select("[data-cfemail]"):
        add(decode_cloudflare(node.get("data-cfemail", "")), obfuscated=True)
    for match in re.finditer(r'data-cfemail="([0-9a-fA-F]+)"', raw_html):
        add(decode_cloudflare(match.group(1)), obfuscated=True)

    # 3. texte visible (on retire scripts et styles pour éviter le bruit)
    body = soup.__copy__()
    for tag in body(["script", "style", "noscript"]):
        tag.decompose()
    text = body.get_text(" ", strip=True)

    for match in EMAIL_RE.finditer(text):
        add(match.group(0), obfuscated=False)
    for match in OBFUSCATED_RE.finditer(text):
        if "@" in match.group(0):  # déjà capturé en clair au-dessus
            continue
        add(_clean_obfuscated(match.group(1), match.group(2)), obfuscated=True)

    # 4. attributs qui transportent parfois l'adresse (data-email, content=…)
    for attr in ("data-email", "data-mail", "content", "value", "title", "aria-label"):
        for node in soup.select(f"[{attr}]"):
            val = str(node.get(attr, ""))
            if "@" in val and len(val) < 200:
                for match in EMAIL_RE.finditer(val):
                    add(match.group(0), obfuscated=False)

    return found
