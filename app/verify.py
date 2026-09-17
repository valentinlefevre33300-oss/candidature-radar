"""Vérification technique d'une adresse, sans jamais envoyer de message.

Deux niveaux :
  - MX : le domaine déclare-t-il un serveur de messagerie ? Rapide, fiable,
    et suffisant pour éliminer les adresses reconstruites sur un domaine mort.
  - SMTP : dialogue jusqu'au `RCPT TO` puis abandon. Beaucoup plus lent, souvent
    faussé (catch-all, greylisting, filtrage des IP résidentielles) et parfois mal
    vu du serveur d'en face. Désactivé par défaut, à n'utiliser qu'en connaissance
    de cause.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import socket
from functools import lru_cache

import dns.exception
import dns.resolver

from .config import SMTP_FROM

log = logging.getLogger(__name__)

_MX_SEMAPHORE = asyncio.Semaphore(12)


@lru_cache(maxsize=4096)
def _mx_hosts(domain: str) -> tuple[str, ...]:
    """Serveurs de messagerie déclarés par le domaine, par priorité croissante."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=6.0)
        records = sorted(((r.preference, str(r.exchange).rstrip(".")) for r in answers))
        return tuple(host for _pref, host in records)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers,
            dns.exception.Timeout, dns.name.EmptyLabel):
        return ()
    except Exception as exc:  # pragma: no cover - dépend du résolveur système
        log.debug("résolution MX impossible pour %s : %s", domain, exc)
        return ()


async def has_mx(domain: str) -> bool:
    """Le domaine peut-il recevoir du courrier ?"""
    async with _MX_SEMAPHORE:
        hosts = await asyncio.to_thread(_mx_hosts, domain)
    return bool(hosts)


def _smtp_probe(email: str, timeout: float = 8.0) -> bool | None:
    """Renvoie True/False si le serveur tranche, None s'il reste ambigu."""
    domain = email.rsplit("@", 1)[-1]
    hosts = _mx_hosts(domain)
    if not hosts:
        return False
    try:
        server = smtplib.SMTP(timeout=timeout)
        server.connect(hosts[0], 25)
        server.helo(socket.getfqdn() or "localhost")
        server.mail(SMTP_FROM)
        code, _message = server.rcpt(email)
        server.quit()
    except (smtplib.SMTPException, OSError) as exc:
        log.debug("sonde SMTP impossible pour %s : %s", email, exc)
        return None
    if code in (250, 251):
        return True
    if code in (550, 551, 553, 554):
        return False
    return None  # 450/451/452 : temporisation, on ne conclut pas


async def smtp_accepts(email: str) -> bool | None:
    return await asyncio.to_thread(_smtp_probe, email)
