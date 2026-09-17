"""Stockage SQLite : historique des recherches et déduplication dans la durée.

L'intérêt principal n'est pas la persistance en soi, c'est de savoir ce qu'on a
déjà vu — et à qui on a déjà écrit. Relancer une recherche voisine ne doit pas
faire remonter les mêmes contacts comme s'ils étaient neufs.
"""
from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import DB_PATH, EXPORT_DIR
from .models import Company, Contact

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    job_title   TEXT NOT NULL,
    sectors     TEXT,
    department  TEXT,
    params      TEXT,
    companies   INTEGER DEFAULT 0,
    contacts    INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'en cours'
);

CREATE TABLE IF NOT EXISTS companies (
    siren       TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    naf         TEXT,
    city        TEXT,
    postal_code TEXT,
    department  TEXT,
    size        TEXT,
    domain      TEXT,
    domain_method TEXT,
    seen_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    email         TEXT NOT NULL,
    run_id        INTEGER NOT NULL,
    company_siren TEXT,
    company_name  TEXT,
    first_name    TEXT,
    last_name     TEXT,
    role_title    TEXT,
    category      TEXT,
    score         REAL,
    source_url    TEXT,
    is_nominative INTEGER,
    was_obfuscated INTEGER,
    inferred      INTEGER,
    pattern_used  TEXT,
    matched_director INTEGER,
    mx_ok         INTEGER,
    smtp_ok       INTEGER,
    reasons       TEXT,
    found_at      TEXT,
    PRIMARY KEY (email, run_id)
);

-- Journal des envois : renseigné à la main, jamais par le scraper.
CREATE TABLE IF NOT EXISTS outreach (
    email      TEXT PRIMARY KEY,
    contacted_at TEXT NOT NULL,
    note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_contacts_run ON contacts(run_id);
CREATE INDEX IF NOT EXISTS idx_contacts_score ON contacts(score DESC);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = Path(path or DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def start_run(job_title: str, sectors: str, department: str | None, params: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, job_title, sectors, department, params) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), job_title, sectors, department, params),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, companies: int, contacts: int, status: str = "terminé") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, companies=?, contacts=?, status=? WHERE id=?",
            (_now(), companies, contacts, status, run_id),
        )


def save_company(company: Company) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO companies (siren, name, naf, city, postal_code, department, size, "
            "domain, domain_method, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(siren) DO UPDATE SET domain=excluded.domain, "
            "domain_method=excluded.domain_method, seen_at=excluded.seen_at",
            (company.siren, company.name, company.naf, company.city, company.postal_code,
             company.department, company.size, company.domain, company.domain_method, _now()),
        )


def save_contacts(run_id: int, contacts: list[Contact]) -> None:
    if not contacts:
        return
    rows = [
        (c.email, run_id, c.company_siren, c.company_name, c.first_name, c.last_name,
         c.role_title, c.category, c.score, c.source_url, int(c.is_nominative),
         int(c.was_obfuscated), int(c.inferred), c.pattern_used, int(c.matched_director),
         None if c.mx_ok is None else int(c.mx_ok),
         None if c.smtp_ok is None else int(c.smtp_ok),
         " | ".join(c.reasons), c.found_at)
        for c in contacts
    ]
    with connect() as conn:
        conn.executemany(
            "INSERT INTO contacts (email, run_id, company_siren, company_name, first_name, "
            "last_name, role_title, category, score, source_url, is_nominative, was_obfuscated, "
            "inferred, pattern_used, matched_director, mx_ok, smtp_ok, reasons, found_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(email, run_id) DO UPDATE SET score=excluded.score",
            rows,
        )


def already_contacted() -> set[str]:
    """Adresses déjà démarchées, à exclure des nouveaux résultats."""
    with connect() as conn:
        return {row["email"] for row in conn.execute("SELECT email FROM outreach")}


def mark_contacted(email: str, note: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO outreach (email, contacted_at, note) VALUES (?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET contacted_at=excluded.contacted_at, "
            "note=excluded.note",
            (email, _now(), note),
        )


def seen_before(emails: list[str]) -> set[str]:
    """Adresses déjà rencontrées lors d'une exécution précédente."""
    if not emails:
        return set()
    placeholders = ",".join("?" * len(emails))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT email FROM contacts WHERE email IN ({placeholders})", emails)
        return {r["email"] for r in rows}


def list_runs(limit: int = 30) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def run_contacts(run_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT c.*, co.city, co.size, co.domain FROM contacts c "
            "LEFT JOIN companies co ON co.siren = c.company_siren "
            "WHERE c.run_id=? ORDER BY c.score DESC", (run_id,))]


EXPORT_COLUMNS = [
    "score", "email", "category", "first_name", "last_name", "role_title",
    "company_name", "city", "size", "domain", "mx_ok", "matched_director",
    "inferred", "pattern_used", "source_url", "reasons", "found_at",
]


def export_csv(run_id: int) -> Path:
    """Écrit le CSV d'une exécution et renvoie son chemin."""
    rows = run_contacts(run_id)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = EXPORT_DIR / f"contacts-run{run_id}-{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS, extrasaction="ignore",
                                delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
