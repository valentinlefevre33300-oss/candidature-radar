"""Gmail : connexion OAuth, envoi, détection des réponses.

Tout passe par l'API REST de Gmail avec `httpx`, sans SDK Google : le flux OAuth
tient en trois requêtes, l'envoi en une. Le jeton est stocké dans un fichier
local (`data/gmail_token.json`) que seul ce poste lit.

Portées demandées, et pourquoi :
  - gmail.send      : envoyer en ton nom, pièce jointe comprise ;
  - gmail.readonly  : lire les réponses — uniquement dans les fils des mails
                      envoyés par l'outil, pour distinguer un refus d'un intérêt
                      et écarter les réponses automatiques. Le reste de la boîte
                      n'est jamais consulté.
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
          "https://www.googleapis.com/auth/gmail.readonly")
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


def granted_scopes() -> set[str]:
    token = _load() or {}
    return set(str(token.get("scope") or "").split())


def missing_scopes() -> list[str]:
    """Portées demandées par cette version mais absentes de l'autorisation en place."""
    granted = granted_scopes()
    return [s for s in SCOPES if granted and s not in granted]


def needs_reconnect() -> bool:
    """Autorisation expirée (7 jours en mode test) ou incomplète (nouvelle portée)."""
    token = _load()
    return bool(token and (token.get("expired") or missing_scopes()))


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
                  attachments: list[tuple[str, bytes]] | None = None,
                  in_reply_to: str | None = None) -> EmailMessage:
    """Construit un mail multipart (texte + HTML) avec pièces jointes.

    `in_reply_to` (Message-ID RFC du mail d'origine) fait de ce message une
    réponse dans le même fil, côté Gmail comme chez le destinataire.
    """
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, payload in attachments or []:
        ctype, _ = mimetypes.guess_type(filename)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return msg


async def send(message: EmailMessage, thread_id: str | None = None) -> tuple[str, str]:
    """Envoie le message (dans un fil existant si `thread_id`). Renvoie (id message, id fil)."""
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    body: dict = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    async with httpx.AsyncClient(timeout=40) as client:
        access = await _access_token(client)
        resp = await client.post(f"{API}/messages/send", json=body,
                                 headers={"Authorization": f"Bearer {access}"})
        if resp.status_code != 200:
            raise GmailError(f"envoi refusé ({resp.status_code}) : {resp.text[:300]}")
        data = resp.json()
        return data.get("id", ""), data.get("threadId", "")


async def message_rfc_id(message_id: str) -> str | None:
    """Le Message-ID RFC d'un mail envoyé, pour y répondre dans le même fil."""
    async with httpx.AsyncClient(timeout=20) as client:
        access = await _access_token(client)
        resp = await client.get(f"{API}/messages/{message_id}",
                                params={"format": "metadata", "metadataHeaders": "Message-ID"},
                                headers={"Authorization": f"Bearer {access}"})
        if resp.status_code != 200:
            return None
        for header in resp.json().get("payload", {}).get("headers", []):
            if header.get("name", "").lower() == "message-id":
                return header.get("value")
    return None


# --------------------------------------------------------------- réponses ---

def _header(message: dict, name: str) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _body_text(payload: dict) -> str:
    """Texte d'un message : la partie text/plain, sinon le HTML dépouillé."""
    plain, html_part = "", ""

    def walk(part: dict) -> None:
        nonlocal plain, html_part
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain" and not plain:
            plain = _decode(data)
        elif data and mime == "text/html" and not html_part:
            html_part = _decode(data)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if plain.strip():
        return plain
    import re as _re
    text = _re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_part, flags=_re.S | _re.I)
    text = _re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=_re.I)
    text = _re.sub(r"<[^>]+>", " ", text)
    return html_lib_unescape(text)


def html_lib_unescape(text: str) -> str:
    import html as _html
    return _html.unescape(text)


async def thread_replies(thread_id: str, own_email: str) -> list[dict]:
    """Les messages du fil qui ne viennent pas de nous : [{id, from, date, text}].

    Lit le fil d'UN mail envoyé par l'outil — c'est la seule lecture faite
    dans la boîte.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        access = await _access_token(client)
        resp = await client.get(f"{API}/threads/{thread_id}", params={"format": "full"},
                                headers={"Authorization": f"Bearer {access}"})
        if resp.status_code != 200:
            log.debug("lecture du fil %s impossible : %s", thread_id, resp.text[:120])
            return []
    own = own_email.lower()
    replies: list[dict] = []
    for message in resp.json().get("messages", []):
        sender = _header(message, "From")
        if not sender or own in sender.lower():
            continue
        replies.append({
            "id": message.get("id"),
            "from": sender,
            "date": message.get("internalDate"),
            "auto": bool(_header(message, "Auto-Submitted") and
                         _header(message, "Auto-Submitted").lower() != "no")
                    or bool(_header(message, "X-Autoreply")) or bool(_header(message, "X-Autorespond")),
            "text": _body_text(message.get("payload") or {}),
            "snippet": message.get("snippet", ""),
        })
    return replies


async def thread_has_reply(thread_id: str, own_email: str) -> bool:
    return bool(await thread_replies(thread_id, own_email))
