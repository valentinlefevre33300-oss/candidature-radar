"""Connexion à l'interface : « Continuer avec Google », un seul compte autorisé.

Le même client OAuth que pour Gmail sert ici, avec les portées d'identité
seulement (openid, email, profile). Après le retour de Google, une session
signée est posée en cookie ; aucune base d'utilisateurs, aucun mot de passe
stocké : l'outil est mono-utilisateur, il vérifie juste que l'adresse Google
est bien celle qui a le droit d'entrer.

Limite imposée par Google : les adresses de retour doivent être `localhost`
ou un vrai nom de domaine — jamais une adresse IP. Sur le réseau local
(téléphone en Wi-Fi), c'est donc le mot de passe HTTP qui reste la voie.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import ALLOWED_EMAILS, BASE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ROOT
from . import gmail

log = logging.getLogger(__name__)

COOKIE = "cr_session"
SESSION_DAYS = 30
SCOPES = ("openid", "email", "profile")
STATE_PREFIX = "login:"
_pending: set[str] = set()


class AuthError(RuntimeError):
    pass


# ------------------------------------------------------------------ secret ---

def _secret() -> bytes:
    """Clé de signature des sessions, générée une fois et gardée hors de git."""
    path = ROOT / "data" / "session_secret"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value.encode("utf-8")
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    return value.encode("utf-8")


# ---------------------------------------------------------------- session ---

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(email: str, name: str = "", picture: str = "") -> str:
    payload = json.dumps({"email": email.lower(), "name": name, "picture": picture,
                          "exp": int(time.time()) + SESSION_DAYS * 86400}, separators=(",", ":"))
    body = _b64(payload.encode("utf-8"))
    signature = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def read_session(cookie: str | None) -> dict | None:
    """Le contenu de la session si la signature et la date tiennent, sinon None."""
    if not cookie or "." not in cookie:
        return None
    body, _, signature = cookie.rpartition(".")
    expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return data


# --------------------------------------------------------------- allowlist ---

def allowed_emails() -> set[str]:
    """Les adresses qui ont le droit d'entrer : la Gmail connectée, plus la liste du .env."""
    emails = {e.strip().lower() for e in ALLOWED_EMAILS.split(",") if e.strip()}
    connected = gmail.connected_email()
    if connected:
        emails.add(connected.lower())
    return emails


def is_allowed(email: str | None) -> bool:
    return bool(email) and email.lower() in allowed_emails()


# ------------------------------------------------------------------ OAuth ---

def login_url(next_path: str = "/") -> str:
    """URL de consentement Google pour l'identité seule.

    On repasse par l'adresse de retour déjà déclarée pour Gmail : le `state`
    préfixé permet à la même route de distinguer les deux flux.
    """
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        raise AuthError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET manquants dans .env")
    state = STATE_PREFIX + secrets.token_urlsafe(24)
    _pending.add(state)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": BASE_URL + gmail.REDIRECT_PATH,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "prompt": "select_account",
    }
    return f"{gmail.AUTH_ENDPOINT}?{urlencode(params)}"


def is_login_state(state: str) -> bool:
    return state.startswith(STATE_PREFIX)


def _jwt_payload(id_token: str) -> dict:
    """Le contenu d'un id_token reçu directement de Google (échange code + secret,
    sur TLS) : il n'y a pas à re-vérifier la signature, seulement l'audience."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise AuthError("id_token illisible")
    data = json.loads(_unb64(parts[1]))
    if data.get("aud") != GOOGLE_CLIENT_ID:
        raise AuthError("id_token destiné à un autre client")
    if data.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise AuthError("id_token d'un émetteur inattendu")
    if int(data.get("exp", 0)) < time.time():
        raise AuthError("id_token expiré")
    return data


async def exchange(code: str, state: str) -> dict:
    """Échange le code contre l'identité Google : {email, name, picture}."""
    if state not in _pending:
        raise AuthError("état de connexion inconnu — recommence")
    _pending.discard(state)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(gmail.TOKEN_ENDPOINT, data={
            "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": BASE_URL + gmail.REDIRECT_PATH, "grant_type": "authorization_code",
        })
    if resp.status_code != 200:
        raise AuthError(f"échange refusé : {resp.text[:160]}")
    payload = _jwt_payload(resp.json().get("id_token", ""))
    if not payload.get("email_verified", False):
        raise AuthError("adresse Google non vérifiée")
    return {"email": str(payload.get("email", "")).lower(), "name": payload.get("name", ""),
            "picture": payload.get("picture", "")}


def cookie_kwargs() -> dict:
    return {"key": COOKIE, "httponly": True, "samesite": "lax", "max_age": SESSION_DAYS * 86400,
            "secure": BASE_URL.startswith("https://"), "path": "/"}
