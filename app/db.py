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

# Définition de la table de suivi, partagée entre la création et la migration.
_OUTREACH_DDL = """
    email        TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'a_contacter',
    note         TEXT DEFAULT '',
    company_name TEXT,
    first_name   TEXT,
    last_name    TEXT,
    role_title   TEXT,
    category     TEXT,
    score        REAL,
    run_id       INTEGER,
    added_at     TEXT NOT NULL,
    updated_at   TEXT NOT NULL
"""

SCHEMA = f"""
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

-- Suivi des envois : renseigné à la main depuis l'interface, jamais par le
-- scraper. On y fige les infos du contact au moment de l'ajout, pour que le
-- suivi reste lisible même si les recherches d'origine sont purgées.
CREATE TABLE IF NOT EXISTS outreach ({_OUTREACH_DDL});

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


OUTREACH_STATUSES = ("a_contacter", "contacte", "relance", "repondu", "ecarte")

# Colonnes ajoutées après la première version de la table `outreach`, qui ne
# connaissait que « contacté ». `CREATE TABLE IF NOT EXISTS` n'altère pas une
# table existante : on complète à la main. Les anciennes lignes prennent le
# statut « contacté », c'est ce qu'elles signifiaient.
_OUTREACH_UPGRADE = {
    "status": "TEXT NOT NULL DEFAULT 'contacte'",
    "note": "TEXT DEFAULT ''",
    "company_name": "TEXT", "first_name": "TEXT", "last_name": "TEXT",
    "role_title": "TEXT", "category": "TEXT", "score": "REAL", "run_id": "INTEGER",
    "added_at": "TEXT", "updated_at": "TEXT",
}


def _upgrade_outreach(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(outreach)")}
    for column, decl in _OUTREACH_UPGRADE.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE outreach ADD COLUMN {column} {decl}")

    # L'ancienne colonne `contacted_at NOT NULL` ferait échouer toute insertion
    # qui ne la renseigne pas. SQLite ne sait pas relâcher une contrainte : on
    # reconstruit la table en reportant les dates, comme le veut la procédure
    # officielle de migration.
    if "contacted_at" in existing:
        columns = ("email, status, note, company_name, first_name, last_name, role_title, "
                   "category, score, run_id, added_at, updated_at")
        conn.executescript(f"""
            CREATE TABLE outreach_v2 ({_OUTREACH_DDL});
            INSERT INTO outreach_v2 ({columns})
            SELECT email, COALESCE(status, 'contacte'), COALESCE(note, ''), company_name,
                   first_name, last_name, role_title, category, score, run_id,
                   COALESCE(added_at, contacted_at), COALESCE(updated_at, contacted_at)
            FROM outreach;
            DROP TABLE outreach;
            ALTER TABLE outreach_v2 RENAME TO outreach;
        """)


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _upgrade_outreach(conn)


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


# ------------------------------------------------------------------ suivi ---

OUTREACH_SNAPSHOT = ("company_name", "first_name", "last_name", "role_title",
                     "category", "score", "run_id")


def list_outreach(status: str | None = None) -> list[dict]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM outreach WHERE status=? ORDER BY updated_at DESC", (status,))
        else:
            rows = conn.execute("SELECT * FROM outreach ORDER BY updated_at DESC")
        return [dict(r) for r in rows]


def upsert_outreach(email: str, status: str, note: str = "", **snapshot) -> None:
    """Ajoute un contact au suivi, ou met à jour son statut s'il y est déjà.

    Les champs de `snapshot` (entreprise, nom, fonction…) ne sont écrits qu'à
    la création : ils décrivent le contact tel qu'il a été trouvé. Une note
    vide ne remplace jamais une note existante.
    """
    values = {key: snapshot.get(key) for key in OUTREACH_SNAPSHOT}
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO outreach (email, status, note, company_name, first_name, last_name, "
            "role_title, category, score, run_id, added_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET status=excluded.status, "
            "updated_at=excluded.updated_at, "
            "note=CASE WHEN excluded.note != '' THEN excluded.note ELSE outreach.note END",
            (email, status, note or "", values["company_name"], values["first_name"],
             values["last_name"], values["role_title"], values["category"], values["score"],
             values["run_id"], now, now),
        )


def update_outreach(email: str, status: str | None = None, note: str | None = None) -> bool:
    """Change le statut et/ou la note. Renvoie False si l'adresse n'est pas suivie."""
    assignments, params = ["updated_at=?"], [_now()]
    if status is not None:
        assignments.append("status=?")
        params.append(status)
    if note is not None:
        assignments.append("note=?")
        params.append(note)
    params.append(email)
    with connect() as conn:
        cur = conn.execute(f"UPDATE outreach SET {', '.join(assignments)} WHERE email=?", params)
        return cur.rowcount > 0


def delete_outreach(email: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM outreach WHERE email=?", (email,))


def already_contacted() -> set[str]:
    """Adresses à qui un mail est effectivement parti (utilisé par le CLI)."""
    with connect() as conn:
        return {row["email"] for row in conn.execute(
            "SELECT email FROM outreach WHERE status IN ('contacte', 'relance', 'repondu')")}


def mark_contacted(email: str, note: str = "") -> None:
    upsert_outreach(email, "contacte", note)


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


def get_run(run_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def run_contacts(run_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT c.*, co.city, co.size, co.domain, "
            "o.status AS outreach_status, o.note AS outreach_note "
            "FROM contacts c "
            "LEFT JOIN companies co ON co.siren = c.company_siren "
            "LEFT JOIN outreach o ON o.email = c.email "
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


OUTREACH_COLUMNS = ["status", "email", "first_name", "last_name", "role_title",
                    "company_name", "score", "note", "added_at", "updated_at", "run_id"]


def export_outreach_csv() -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"suivi-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTREACH_COLUMNS, extrasaction="ignore",
                                delimiter=";")
        writer.writeheader()
        for row in list_outreach():
            writer.writerow(row)
    return path
