"""Gmail : connexion OAuth, envoi, détection des réponses.

Tout passe par l'API REST de Gmail avec `httpx`, sans SDK Google : le flux OAuth
tient en trois requêtes, l'envoi en une. Le jeton est stocké dans un fichier
local (`data/gmail_token.json`) que seul ce poste lit.

Portées demandées, et pourquoi :
  - gmail.send      : envoyer en ton nom, pièce jointe comprise ;
  - gmail.metadata  : lire les EN-TÊTES des fils (jamais les corps) pour
                      détecter qu'une personne a répondu.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import secrets
import time
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import BASE_URL, GMAIL_TOKEN_PATH, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = ("https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/gmail.metadata")
REDIRECT_PATH = "/api/gmail/callback"

_pending_states: set[str] = set()


class GmailNotConnected(RuntimeError):
    pass


class GmailError(RuntimeError):
    pass


# ------------------------------------------------------------------ jeton ---

def _load() -> dict | None:
    try:
        return json.loads(Path(GMAIL_TOKEN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(token: dict) -> None:
    path = Path(GMAIL_TOKEN_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2), encoding="utf-8")


def configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def is_connected() -> bool:
    token = _load()
    return bool(token and token.get("refresh_token") and not token.get("expired"))


def needs_reconnect() -> bool:
    """L'autorisation a existé mais Google ne la renouvelle plus (7 jours en mode test)."""
    token = _load()
    return bool(token and token.get("expired"))


def connected_email() -> str | None:
    token = _load()
    return token.get("email") if token else None


def disconnect() -> None:
    try:
        Path(GMAIL_TOKEN_PATH).unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ OAuth ---

def auth_url() -> str:
    """URL de consentement Google. `state` protège le retour contre la forgerie."""
    if not configured():
        raise GmailError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET manquants dans .env")
    state = secrets.token_urlsafe(24)
    _pending_states.add(state)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": BASE_URL + REDIRECT_PATH,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # obtenir un refresh_token
        "prompt": "consent",        # le forcer même si déjà accordé
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code(code: str, state: str) -> str:
    """Échange le code contre des jetons, les enregistre, renvoie l'adresse connectée."""
    if state not in _pending_states:
        raise GmailError("état OAuth inconnu — recommence la connexion")
    _pending_states.discard(state)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(TOKEN_ENDPOINT, data={
            "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": BASE_URL + REDIRECT_PATH, "grant_type": "authorization_code",
        })
        if resp.status_code != 200:
            raise GmailError(f"échange du code refusé : {resp.text[:200]}")
        token = resp.json()
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
        profile = await client.get(f"{API}/profile",
                                   headers={"Authorization": f"Bearer {token['access_token']}"})
        token["email"] = profile.json().get("emailAddress") if profile.status_code == 200 else None
    if not token.get("refresh_token"):
        raise GmailError("Google n'a pas fourni de refresh_token : révoque l'accès dans ton "
                         "compte Google puis recommence")
    _save(token)
    return token.get("email") or ""


async def _access_token(client: httpx.AsyncClient) -> str:
    token = _load()
    if not token or not token.get("refresh_token"):
        raise GmailNotConnected("Gmail n'est pas connecté")
    if token.get("access_token") and time.time() < float(token.get("expires_at", 0)):
        return token["access_token"]
    resp = await client.post(TOKEN_ENDPOINT, data={
        "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
    })
    if resp.status_code != 200:
        # Typiquement `invalid_grant` : une application Google en mode « test »
        # voit ses autorisations expirer au bout de 7 jours. On le mémorise pour
        # que l'interface demande une reconnexion, et on ne bloque rien d'autre.
        token["expired"] = True
        _save(token)
        raise GmailNotConnected("Google ne renouvelle plus l'autorisation : reconnecte ton Gmail "
                                f"({resp.text[:120]})")
    fresh = resp.json()
    token["access_token"] = fresh["access_token"]
    token["expires_at"] = time.time() + int(fresh.get("expires_in", 3600)) - 60
    _save(token)
    return token["access_token"]


# ------------------------------------------------------------------ envoi ---

def build_message(*, sender: str, sender_name: str, to: str, subject: str,
                  text: str, html: str | None = None,
                  attachments: list[tuple[str, bytes]] | None = None) -> EmailMessage:
    """Construit un mail multipart (texte + HTML) avec pièces jointes."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, payload in attachments or []:
        ctype, _ = mimetypes.guess_type(filename)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return msg


async def send(message: EmailMessage) -> tuple[str, str]:
    """Envoie le message. Renvoie (id du message, id du fil) côté Gmail."""
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    async with httpx.AsyncClient(timeout=40) as client:
        access = await _access_token(client)
        resp = await client.post(f"{API}/messages/send", json={"raw": raw},
                                 headers={"Authorization": f"Bearer {access}"})
        if resp.status_code != 200:
            raise GmailError(f"envoi refusé ({resp.status_code}) : {resp.text[:300]}")
        data = resp.json()
        return data.get("id", ""), data.get("threadId", "")


# --------------------------------------------------------------- réponses ---

async def thread_has_reply(thread_id: str, own_email: str) -> bool:
    """Le fil contient-il un message d'un autre expéditeur que nous ?"""
    async with httpx.AsyncClient(timeout=20) as client:
        access = await _access_token(client)
        resp = await client.get(f"{API}/threads/{thread_id}",
                                params={"format": "metadata", "metadataHeaders": "From"},
                                headers={"Authorization": f"Bearer {access}"})
        if resp.status_code != 200:
            log.debug("lecture du fil %s impossible : %s", thread_id, resp.text[:120])
            return False
        own = own_email.lower()
        for message in resp.json().get("messages", []):
            for header in message.get("payload", {}).get("headers", []):
                if header.get("name", "").lower() == "from" and own not in header.get("value", "").lower():
                    return True
    return False
