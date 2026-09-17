# -*- coding: utf-8 -*-
"""Suite de non-regression, sans reseau.

Chaque bloc correspond a un defaut rencontre en conditions reelles pendant la
construction de l'outil. Lancer : .venv/Scripts/python.exe tests/test_unit.py
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from app.extract.emails import EMAIL_RE, OBFUSCATED_RE, decode_cloudflare, extract_emails
from app.extract.people import (classify_mailbox, find_role_near, guess_name_from_local,
                                infer_pattern, render)
from app.models import Company, Contact, Director
from app.pipeline import _build_contact, _infer_director_emails
from app.resolve.domain import candidate_domains, company_tokens, name_matches_page
from app.score import dedupe_and_rank, matches_director, score_contact
from app.sources.sirene import _parse_directors, headcount_codes

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    status = "ok  " if condition else "ECHEC"
    print(f"  [{status}] {label}")
    if not condition:
        FAILURES.append(label)


def cf_encode(email: str, key: int = 0x7A) -> str:
    return format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in email)


# --------------------------------------------------------------------------
print("\n== Extraction d'adresses ==")

CF_BLOB = cf_encode("camille.roux@acme.fr")
HTML = f"""
<html><body>
  <a href="mailto:Marie.Dupont@acme.fr?subject=Hello">Marie Dupont</a>
  <a href="mailto:rh@acme.fr,jobs@acme.fr">RH</a>
  <p>Ecrivez a contact [at] acme [dot] fr pour toute question.</p>
  <p>Notre DG : j.martin AT acme DOT fr</p>
  <p>Support : support (arobase) acme (point) fr</p>
  <p>Direct : paul.bernard&#64;acme.fr</p>
  <span data-cfemail="{CF_BLOB}">[email protected]</span>
  <div data-email="sophie.leroy@acme.fr">Sophie</div>
  <img src="logo@2x.png"><img src="hero@3x.webp">
  <script>var dsn="https://abcdef0123456789abcdef0123456789@sentry.io/42";</script>
  <p>exemple : prenom.nom@example.com</p>
  <p>Saisissez votre adresse, par exemple nom@exemple.fr</p>
  <p>no-reply@acme.fr ne pas repondre</p>
  <p>webmaster@acme.fr</p>
</body></html>
"""
found = extract_emails(BeautifulSoup(HTML, "lxml"), HTML)

EXPECTED = {
    "marie.dupont@acme.fr", "rh@acme.fr", "jobs@acme.fr", "contact@acme.fr",
    "j.martin@acme.fr", "support@acme.fr", "paul.bernard@acme.fr",
    "sophie.leroy@acme.fr", "camille.roux@acme.fr",
}
REJECTED = {
    "logo@2x.png", "hero@3x.webp", "prenom.nom@example.com", "nom@exemple.fr",
    "no-reply@acme.fr", "webmaster@acme.fr",
}

check(EXPECTED <= set(found), f"les 9 adresses attendues sont trouvees (manque {EXPECTED - set(found)})")
check(not (REJECTED & set(found)), f"les faux positifs sont ecartes (fuite {REJECTED & set(found)})")
check(set(found) == EXPECTED, f"rien d'inattendu (surplus {set(found) - EXPECTED})")
check(decode_cloudflare(CF_BLOB) == "camille.roux@acme.fr", "decodage Cloudflare")
check(found.get("contact@acme.fr") is True, "l'adresse obfusquee est signalee comme telle")
check(found.get("rh@acme.fr") is False, "l'adresse en clair n'est pas signalee obfusquee")

# Regression : la regex decoupait les mots francais contenant « at ».
print("\n== Obfuscation : pas de decoupage des mots francais ==")
for word in ("international", "candidature", "information", "automatisation",
             "rationnelle", "statistiques", "utilisateur", "partenariat"):
    check(not OBFUSCATED_RE.findall(word), f"« {word} » ne produit pas de fausse adresse")

# Regression : un TLD invente ne doit pas passer via la desobfuscation.
soup_junk = BeautifulSoup("<p>intern (at) ional (dot) ce</p>", "lxml")
check(not extract_emails(soup_junk, "<p>intern (at) ional (dot) ce</p>"),
      "un TLD inconnu est refuse sur une adresse reconstituee")

# --------------------------------------------------------------------------
print("\n== Classement des boites mail ==")
for email, expected in [("rh@acme.fr", "rh"), ("recrutement@acme.fr", "rh"),
                        ("jobs@acme.fr", "rh"), ("contact@acme.fr", "generique"),
                        ("info@acme.fr", "generique"), ("dpo@acme.fr", "juridique"),
                        ("support@acme.fr", "technique"), ("ceo@acme.fr", "direction"),
                        ("marie.dupont@acme.fr", "nominatif"), ("compta@acme.fr", "commercial")]:
    check(classify_mailbox(email) == expected,
          f"{email} -> {expected} (obtenu {classify_mailbox(email)})")

check(guess_name_from_local("marie.dupont") == ("Marie", "Dupont"), "prenom.nom analyse")
check(guess_name_from_local("m.dupont") == (None, "Dupont"), "p.nom : seul le nom est sur")
check(guess_name_from_local("contact") == (None, None), "une boite generique n'a pas de nom")

# --------------------------------------------------------------------------
print("\n== Motif d'adressage ==")
check(render("{first}.{last}", "Frederic", "Le Gall") == "frederic.legall", "rendu prenom.nom")
check(render("{first}.{last}", "Frédéric", "Le Gall") == "frederic.legall", "les accents sont normalises")
check(infer_pattern(["frederic.legall@a.fr", "contact@a.fr"],
                    [("Frédéric", "Le Gall")]) == "{first}.{last}", "motif prenom.nom deduit")
check(infer_pattern(["flegall@a.fr"], [("Frederic", "Le Gall")]) == "{f}{last}", "motif pnom deduit")
check(infer_pattern(["contact@a.fr"], [("Marie", "Dupont")]) is None,
      "aucun motif deduit sans adresse nominative")

# --------------------------------------------------------------------------
print("\n== Annuaire : dirigeants retenus ==")
RAW = [
    {"nom": "SAUNIER (MARTIN DIT NEUVILLE)", "prenoms": "BEATRICE",
     "qualite": "Administrateur", "type_dirigeant": "personne physique"},
    {"nom": "LABRUNE", "prenoms": "JEAN-CLAUDE",
     "qualite": "President du conseil d'administration et directeur general",
     "type_dirigeant": "personne physique"},
    {"nom": "DUPONT (NEE MARTIN)", "prenoms": "MARIE",
     "qualite": "Directeur General", "type_dirigeant": "personne physique"},
    {"nom": "AUDIT & CIE", "prenoms": "", "qualite": "Commissaire aux comptes",
     "type_dirigeant": "personne physique"},
    {"nom": "HOLDING XY", "prenoms": "", "qualite": "President",
     "type_dirigeant": "personne morale"},
]
kept = _parse_directors(RAW)
names = {d.last_name for d in kept}
check(len(kept) == 2, f"seuls les 2 dirigeants operationnels sont gardes (obtenu {len(kept)})")
check("SAUNIER" not in names, "un administrateur de conseil est ecarte")
check("DUPONT" in names, "le nom d'usage entre parentheses est retire")
check(all("(" not in d.last_name for d in kept), "aucun nom ne conserve de parenthese")

check(set(headcount_codes(10, 49)) == {"11", "12"}, "fourchette 10-49 -> codes 11 et 12")
check(headcount_codes(None, None) == [], "sans filtre d'effectif, aucun code impose")

# --------------------------------------------------------------------------
print("\n== Resolution de domaine ==")
check("chapsvision.fr" in candidate_domains("CHAPSVISION SAS"), "candidat evident genere")
check("groupe" not in company_tokens("GROUPE SAPH"), "la forme juridique est retiree")
check(name_matches_page("CHAPSVISION", "<h1>ChapsVision</h1>") > 0.9, "nom present -> confiance haute")
check(name_matches_page("CHAPSVISION", "<h1>Autre chose</h1>") == 0.0, "nom absent -> confiance nulle")
# Regression : « LOCAL.FR » matchait n'importe quelle page francaise.
check(name_matches_page("LOCAL.FR", "<p>commerce local en france</p>") == 0.0,
      "des mots banals seuls ne suffisent pas a valider un domaine")

# --------------------------------------------------------------------------
print("\n== Scoring ==")
firm = Company(siren="1", name="ACME", domain="acme.fr", headcount_code="12")
firm.directors = [Director(last_name="DUPONT", first_names="MARIE", role="Directeur General")]


def make(email, **kw):
    base = dict(company_siren="1", company_name="ACME", source_url="https://acme.fr/contact")
    base.update(kw)
    return Contact(email=email, **base)


rh = score_contact(make("aude.balleydier@acme.fr", category="rh", is_nominative=True,
                        role_title="Responsable Ressources Humaines", mx_ok=True), firm, "developpeur")
boss = score_contact(make("marie.dupont@acme.fr", category="direction", is_nominative=True,
                          inferred=True, matched_director=True, mx_ok=True,
                          source_url="(deduit)"), firm, "developpeur")
generic = score_contact(make("contact@acme.fr", category="generique", mx_ok=True), firm, "developpeur")
vendor = score_contact(make("info@studiometa.fr", category="generique", mx_ok=True), firm, "developpeur")
dead = score_contact(make("rh@acme.fr", category="rh", mx_ok=False), firm, "developpeur")

check(rh.score > boss.score, f"un contact RH observe ({rh.score}) passe devant un dirigeant deduit ({boss.score})")
check(boss.score > generic.score, f"un dirigeant deduit ({boss.score}) passe devant une boite generique ({generic.score})")
check(vendor.score < generic.score, f"un domaine tiers ({vendor.score}) est depriorise")
check(any("prestataire" in r for r in vendor.reasons), "la raison du declassement est explicite")
check(dead.score < generic.score, f"une adresse sans MX ({dead.score}) est depriorisee")
check(all(c.reasons for c in (boss, vendor, dead)), "chaque declassement est justifie")

check(matches_director(make("marie.dupont@acme.fr"), firm), "l'adresse d'un dirigeant est reconnue")
check(not matches_director(make("contact@acme.fr"), firm), "une boite generique n'est pas un dirigeant")

ranked = dedupe_and_rank([generic, rh, boss, vendor, rh])
check(len(ranked) == 4, f"les doublons sont fusionnes (obtenu {len(ranked)})")
check(ranked[0].email == rh.email, "le meilleur contact arrive en tete")

# --------------------------------------------------------------------------
print("\n== Fonction lue autour de l'adresse ==")
# Regression : « coo » matchait dans « coordonnees » et promouvait tibco@ en direction.
cat, _ = find_role_near("Saint-Aignan-de-Grandlieu coordonnees de contact tibco le "
                        "tibco@tibco.fr 02 40 00 00 00", "tibco@tibco.fr")
check(cat is None, f"« coordonnees » n'est pas lu comme la fonction COO (obtenu {cat})")
cat, ex = find_role_near("Aude Balleydier Responsable Ressources Humaines +33(0)1 49 09 68 81 "
                         "aude.b@x.fr Cegedim recrutement 137 rue d'Aguesseau 92100 Boulogne",
                         "aude.b@x.fr")
check(cat == "rh", f"la fonction RH est lue (obtenu {cat})")
check(not re.search(r"\d", ex or ""), f"l'extrait ne contient ni telephone ni adresse ({ex!r})")
check((ex or "").startswith("aude balleydier responsable"), f"l'extrait commence sur un mot entier ({ex!r})")
cat, _ = find_role_near("Jean Martin, CEO - jean@acme.fr", "jean@acme.fr")
check(cat == "direction", "un sigle isole (CEO) est bien reconnu")

# --------------------------------------------------------------------------
print("\n== Contexte de page : pas de promotion abusive ==")
NAV = ("Solutions & Co achats responsables Recrutement Contact Acces espace client "
       "contact@acme.fr Mentions legales")
c1 = _build_contact("contact@acme.fr", False, "https://acme.fr/", NAV, firm)
check(c1.category == "generique", f"« contact@ » reste generique malgre le mot Recrutement (obtenu {c1.category})")
check(c1.role_title is None, "l'extrait trompeur n'est pas conserve")

BIO = "Marie Dupont, Responsable Ressources Humaines - marie.dupont@acme.fr"
c2 = _build_contact("marie.dupont@acme.fr", False, "https://acme.fr/equipe", BIO, firm)
check(c2.category == "rh", f"une adresse nominative prend la fonction lue (obtenu {c2.category})")
check(c2.is_nominative, "l'adresse est marquee nominative")
check(c2.matched_director is True,
      "l'adresse recoupe la dirigeante declaree au registre")

c3 = _build_contact("recrutement@acme.fr", False, "https://acme.fr/", NAV, firm)
check(c3.category == "rh", "une boite de recrutement reste RH")

# --------------------------------------------------------------------------
print("\n== Deduction plafonnee ==")
crowded = Company(siren="2", name="BIGCO", domain="bigco.fr", headcount_code="32")
crowded.directors = [
    Director(last_name=f"NOM{i}", first_names=f"PRENOM{i}",
             role="Directeur General" if i < 2 else "Directeur")
    for i in range(9)
]
observed = [make("jean.martin@bigco.fr", company_siren="2", company_name="BIGCO")]
observed[0].first_name, observed[0].last_name = "Jean", "Martin"
produced = _infer_director_emails(crowded, observed)
check(len(produced) <= 3, f"au plus 3 adresses deduites par entreprise (obtenu {len(produced)})")
check(all(c.inferred and c.pattern_used for c in produced), "chaque adresse deduite est tracee")
check(all(c.source_url.startswith("(deduit") for c in produced), "la provenance indique la deduction")

# --------------------------------------------------------------------------
print("\n" + "=" * 62)
if FAILURES:
    print(f"{len(FAILURES)} ECHEC(S) :")
    for item in FAILURES:
        print("  -", item)
    sys.exit(1)
print("Tous les controles passent.")
