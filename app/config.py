"""Réglages globaux du crawler. Tout est surchargeable via variables d'environnement."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("CR_DB_PATH", ROOT / "data" / "candidature-radar.sqlite3"))
EXPORT_DIR = Path(os.getenv("CR_EXPORT_DIR", ROOT / "data" / "exports"))

# Identité déclarée aux serveurs. On s'annonce honnêtement : pas d'usurpation
# de navigateur, et une adresse de contact pour que l'on puisse nous joindre.
CONTACT_EMAIL = os.getenv("CR_CONTACT_EMAIL", "")
USER_AGENT = os.getenv(
    "CR_USER_AGENT",
    "candidature-radar/0.1 (+https://github.com/Valentinlefevreepitech/candidature-radar)"
    + (f"; {CONTACT_EMAIL}" if CONTACT_EMAIL else ""),
)

# Politesse réseau
REQUEST_TIMEOUT = float(os.getenv("CR_TIMEOUT", "12"))
PER_DOMAIN_DELAY = float(os.getenv("CR_DOMAIN_DELAY", "1.5"))   # secondes entre 2 hits d'un même hôte
GLOBAL_CONCURRENCY = int(os.getenv("CR_CONCURRENCY", "8"))       # domaines traités en parallèle
MAX_PAGES_PER_SITE = int(os.getenv("CR_MAX_PAGES", "12"))
MAX_PAGE_BYTES = int(os.getenv("CR_MAX_PAGE_BYTES", str(2_000_000)))
RESPECT_ROBOTS = os.getenv("CR_RESPECT_ROBOTS", "1") != "0"

# Vérification des emails
SMTP_PROBE = os.getenv("CR_SMTP_PROBE", "0") == "1"  # désactivé par défaut (lent + peu fiable)
SMTP_FROM = os.getenv("CR_SMTP_FROM", "verify@example.org")

# Sources
SIRENE_API = "https://recherche-entreprises.api.gouv.fr/search"


# --- Campagnes : envoi, suivi, rédaction ---------------------------------
BASE_URL = os.getenv("CR_BASE_URL", "http://localhost:8010").rstrip("/")
# URL joignable depuis Internet, pour le pixel d'ouverture. Vide = pas de pixel
# (l'app tourne en local) ; à renseigner au déploiement.
PUBLIC_URL = os.getenv("CR_PUBLIC_URL", "").rstrip("/")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GMAIL_TOKEN_PATH = Path(os.getenv("CR_GMAIL_TOKEN", ROOT / "data" / "gmail_token.json"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

MONTHLY_CAP = int(os.getenv("CR_MONTHLY_CAP", "200"))   # envois par mois, toutes campagnes
DAILY_CAP = int(os.getenv("CR_DAILY_CAP", "25"))        # envois par jour, toutes campagnes
SEND_INTERVAL = int(os.getenv("CR_SEND_INTERVAL", "240"))  # secondes minimum entre deux envois
SENDER_NAME = os.getenv("CR_SENDER_NAME", "")
CLAUDE_MODEL = os.getenv("CR_CLAUDE_MODEL", "claude-opus-5")
# Mode simulation : la chaîne d'envoi tourne entièrement (rédaction, quotas,
# journal) mais aucun mail ne part. Pour tester une campagne à blanc.
DRY_RUN = os.getenv("CR_DRY_RUN", "0") == "1"
