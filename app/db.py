"""Stockage SQLite : historique des recherches et déduplication dans la durée.

L'intérêt principal n'est pas la persistance en soi, c'est de savoir ce qu'on a
déjà vu — et à qui on a déjà écrit. Relancer une recherche voisine ne doit pas
faire remonter les mêmes contacts comme s'ils étaient neufs.
"""
from __future__ import annotations

import csv
import sqlite3
from urllib.parse import quote
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    tagline     TEXT,
    about       TEXT,
    brief       TEXT,
    fit         INTEGER DEFAULT 0,
    fit_terms   TEXT,
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
    is_manager    INTEGER,
    linkedin_url  TEXT,
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

-- Réglages libres (expéditeur, signature, CV par défaut…), un couple clé/valeur.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Une campagne = un poste, une cible, un gabarit de mail, un CV, une liste
-- de candidatures programmées puis envoyées au rythme des quotas.
CREATE TABLE IF NOT EXISTS campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    job_title    TEXT NOT NULL,
    zone         TEXT,
    sectors      TEXT,
    run_id       INTEGER,
    status       TEXT NOT NULL DEFAULT 'brouillon',   -- brouillon | active | en_pause | terminee
    subject_tpl  TEXT,
    body_tpl     TEXT,
    personalize  INTEGER DEFAULT 1,
    cv_path      TEXT,
    daily_cap    INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    launched_at  TEXT
);

-- Une candidature = un mail vers une personne, pour une campagne. Le contact
-- et l'entreprise sont figés au moment de la création.
CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id      INTEGER NOT NULL,
    email            TEXT NOT NULL,
    first_name       TEXT,
    last_name        TEXT,
    role_title       TEXT,
    linkedin_url     TEXT,
    category         TEXT,
    score            REAL,
    company_siren    TEXT,
    company_name     TEXT,
    company_domain   TEXT,
    company_city     TEXT,
    company_size     TEXT,
    company_naf      TEXT,
    company_tagline  TEXT,
    company_about    TEXT,
    company_brief    TEXT,                           -- fiche « enjeux » rédigée
    subject          TEXT,
    body_text        TEXT,
    body_html        TEXT,
    hook             TEXT,                            -- paragraphe personnalisé
    status           TEXT NOT NULL DEFAULT 'programme', -- programme | envoye | ouvert | repondu | echec | annule
    scheduled_at     TEXT,
    sent_at          TEXT,
    gmail_message_id TEXT,
    gmail_thread_id  TEXT,
    token            TEXT UNIQUE,                     -- pixel d'ouverture
    opens            INTEGER DEFAULT 0,
    last_open_at     TEXT,
    replied_at       TEXT,
    error            TEXT,
    UNIQUE (campaign_id, email)
);

-- Journal d'activité : ce qui s'est passé, dans l'ordre.
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id    INTEGER,
    application_id INTEGER,
    kind           TEXT NOT NULL,   -- lancement | envoi | ouverture | reponse | erreur | pause | reprise
    at             TEXT NOT NULL,
    detail         TEXT
);

CREATE INDEX IF NOT EXISTS idx_app_campaign ON applications(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_app_due ON applications(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id, id DESC);
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


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Complète une table existante : `CREATE TABLE IF NOT EXISTS` n'ajoute rien."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column, decl in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _upgrade_outreach(conn)
        _add_missing_columns(conn, "companies", {"tagline": "TEXT", "about": "TEXT", "brief": "TEXT",
                                          "fit": "INTEGER DEFAULT 0", "fit_terms": "TEXT"})
        _add_missing_columns(conn, "contacts", {"is_manager": "INTEGER DEFAULT 0", "linkedin_url": "TEXT"})
        _add_missing_columns(conn, "applications", {
            "linkedin_url": "TEXT", "prepared_at": "TEXT", "validated_at": "TEXT",
            "company_about": "TEXT", "company_brief": "TEXT",
            "reply_kind": "TEXT",            # refus | interet | question | absence | autre
            "reply_excerpt": "TEXT",         # début de la réponse, sans le texte cité
            "last_reply_id": "TEXT",         # dernier message lu dans le fil
            "followups": "INTEGER DEFAULT 0",
            "last_followup_at": "TEXT",
        })


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
            "domain, domain_method, tagline, about, fit, fit_terms, seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(siren) DO UPDATE SET domain=excluded.domain, "
            "domain_method=excluded.domain_method, "
            "tagline=COALESCE(excluded.tagline, companies.tagline), "
            "about=COALESCE(excluded.about, companies.about), "
            "fit=excluded.fit, fit_terms=excluded.fit_terms, seen_at=excluded.seen_at",
            (company.siren, company.name, company.naf, company.city, company.postal_code,
             company.department, company.size, company.domain, company.domain_method,
             company.tagline, company.about, company.fit, company.fit_terms, _now()),
        )


def set_company_brief(siren: str | None, brief: str | None) -> None:
    """Mémorise la fiche « enjeux » : une par entreprise, réutilisée d'un contact à l'autre."""
    if not siren or not brief:
        return
    with connect() as conn:
        conn.execute("UPDATE companies SET brief=? WHERE siren=?", (brief, siren))
        conn.execute("UPDATE applications SET company_brief=? WHERE company_siren=? "
                     "AND (company_brief IS NULL OR company_brief='')", (brief, siren))


def save_contacts(run_id: int, contacts: list[Contact]) -> None:
    if not contacts:
        return
    rows = [
        (c.email, run_id, c.company_siren, c.company_name, c.first_name, c.last_name,
         c.role_title, c.category, c.score, c.source_url, int(c.is_nominative),
         int(c.was_obfuscated), int(c.inferred), c.pattern_used, int(c.matched_director),
         int(c.is_manager), c.linkedin_url,
         None if c.mx_ok is None else int(c.mx_ok),
         None if c.smtp_ok is None else int(c.smtp_ok),
         " | ".join(c.reasons), c.found_at)
        for c in contacts
    ]
    with connect() as conn:
        conn.executemany(
            "INSERT INTO contacts (email, run_id, company_siren, company_name, first_name, "
            "last_name, role_title, category, score, source_url, is_nominative, was_obfuscated, "
            "inferred, pattern_used, matched_director, is_manager, linkedin_url, mx_ok, smtp_ok, reasons, found_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(email, run_id) DO UPDATE SET score=excluded.score, "
            "linkedin_url=COALESCE(excluded.linkedin_url, contacts.linkedin_url)",
            rows,
        )



def set_contact_linkedin(run_id: int, email: str, url: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE contacts SET linkedin_url=? WHERE run_id=? AND email=?", (url, run_id, email))
        conn.execute("UPDATE applications SET linkedin_url=? WHERE email=? AND linkedin_url IS NULL", (url, email))


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
            "SELECT c.*, co.city, co.size, co.domain, co.naf, co.tagline, co.about, co.brief, "
            "co.fit, co.fit_terms, "
            "o.status AS outreach_status, o.note AS outreach_note "
            "FROM contacts c "
            "LEFT JOIN companies co ON co.siren = c.company_siren "
            "LEFT JOIN outreach o ON o.email = c.email "
            "WHERE c.run_id=? ORDER BY c.score DESC", (run_id,))]


EXPORT_COLUMNS = [
    "score", "email", "category", "first_name", "last_name", "role_title",
    "company_name", "city", "size", "domain", "mx_ok", "matched_director", "is_manager",
    "inferred", "pattern_used", "linkedin_url", "linkedin_recherche", "source_url", "reasons", "found_at",
]


def export_csv(run_id: int, emails: list[str] | None = None) -> Path:
    """Écrit le CSV d'une exécution (ou d'une sélection d'adresses) et renvoie son chemin."""
    rows = run_contacts(run_id)
    if emails:
        wanted = {e.strip().lower() for e in emails}
        rows = [r for r in rows if str(r.get("email", "")).lower() in wanted]
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = EXPORT_DIR / f"contacts-run{run_id}-{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS, extrasaction="ignore",
                                delimiter=";")
        writer.writeheader()
        for row in rows:
            row = dict(row)
            row["linkedin_recherche"] = linkedin_search_url(row)
            writer.writerow(row)
    return path


def linkedin_search_url(row: dict) -> str:
    """Recherche LinkedIn préremplie (nom + entreprise), quand le profil n'est pas connu."""
    words = [row.get("first_name"), row.get("last_name"), row.get("company_name")]
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(" ".join(w for w in words if w))


# -------------------------------------------------------------- réglages ---

def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def all_settings() -> dict[str, str]:
    with connect() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


# ------------------------------------------------------------- campagnes ---

CAMPAIGN_STATUSES = ("brouillon", "active", "en_pause", "terminee")
# en_attente : dans la file, pas encore rédigée · a_valider : rédigée, attend ton accord ·
# programme : partira à son créneau sauf blocage.
APPLICATION_STATUSES = ("en_attente", "a_valider", "programme", "envoye", "ouvert", "repondu", "echec", "annule")
PENDING_STATUSES = ("en_attente", "a_valider", "programme")
SENT_STATUSES = ("envoye", "ouvert", "repondu")

_CAMPAIGN_COUNTS = """
    (SELECT COUNT(*) FROM applications a WHERE a.campaign_id=c.id) AS total,
    (SELECT COUNT(*) FROM applications a WHERE a.campaign_id=c.id
        AND a.status IN ('en_attente','a_valider','programme')) AS scheduled,
    (SELECT COUNT(*) FROM applications a WHERE a.campaign_id=c.id AND a.status='a_valider') AS to_review,
    (SELECT COUNT(*) FROM applications a WHERE a.campaign_id=c.id
        AND a.status IN ('envoye','ouvert','repondu')) AS sent,
    (SELECT COUNT(*) FROM applications a WHERE a.campaign_id=c.id AND a.opens > 0) AS opened,
    (SELECT COUNT(*) FROM applications a WHERE a.campaign_id=c.id AND a.status='repondu') AS replied,
    (SELECT COUNT(*) FROM applications a WHERE a.campaign_id=c.id AND a.status='echec') AS failed
"""


def create_campaign(**fields) -> int:
    now = _now()
    cols = ["name", "job_title", "zone", "sectors", "run_id", "status", "subject_tpl",
            "body_tpl", "personalize", "cv_path", "daily_cap"]
    values = [fields.get(c) for c in cols]
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO campaigns ({', '.join(cols)}, created_at, updated_at) "
            f"VALUES ({', '.join('?' * len(cols))}, ?, ?)", (*values, now, now))
        return int(cur.lastrowid)


def update_campaign(campaign_id: int, **fields) -> bool:
    allowed = {"name", "job_title", "zone", "sectors", "run_id", "status", "subject_tpl",
               "body_tpl", "personalize", "cv_path", "daily_cap", "launched_at"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        return False
    changes["updated_at"] = _now()
    assignments = ", ".join(f"{k}=?" for k in changes)
    with connect() as conn:
        cur = conn.execute(f"UPDATE campaigns SET {assignments} WHERE id=?",
                           (*changes.values(), campaign_id))
        return cur.rowcount > 0


def get_campaign(campaign_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT c.*, {_CAMPAIGN_COUNTS} FROM campaigns c WHERE c.id=?", (campaign_id,)
        ).fetchone()
        return dict(row) if row else None


def list_campaigns() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT c.*, {_CAMPAIGN_COUNTS} FROM campaigns c ORDER BY c.id DESC")]


def delete_campaign(campaign_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM events WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM applications WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


# ---------------------------------------------------------- candidatures ---

APPLICATION_FIELDS = (
    "email", "first_name", "last_name", "role_title", "linkedin_url", "category", "score",
    "company_siren", "company_name", "company_domain", "company_city", "company_size",
    "company_naf", "company_tagline", "company_about", "company_brief",
    "subject", "body_text", "body_html", "hook",
    "status", "scheduled_at", "token",
)


def add_applications(campaign_id: int, rows: list[dict]) -> int:
    """Ajoute des candidatures ; celles déjà présentes (même adresse) sont ignorées."""
    if not rows:
        return 0
    cols = ("campaign_id", *APPLICATION_FIELDS)
    with connect() as conn:
        cur = conn.executemany(
            f"INSERT OR IGNORE INTO applications ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            [(campaign_id, *[row.get(f) for f in APPLICATION_FIELDS]) for row in rows])
        return cur.rowcount


def update_application(application_id: int, **fields) -> None:
    allowed = set(APPLICATION_FIELDS) | {"sent_at", "gmail_message_id", "gmail_thread_id",
                                         "opens", "last_open_at", "replied_at", "error",
                                         "reply_kind", "reply_excerpt", "last_reply_id",
                                         "followups", "last_followup_at", "prepared_at", "validated_at"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        return
    assignments = ", ".join(f"{k}=?" for k in changes)
    with connect() as conn:
        conn.execute(f"UPDATE applications SET {assignments} WHERE id=?",
                     (*changes.values(), application_id))


def get_application(application_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        return dict(row) if row else None


def list_applications(campaign_id: int, status: str | None = None, q: str = "",
                      page: int = 1, size: int = 10) -> tuple[list[dict], int]:
    """Candidatures d'une campagne, paginées. Renvoie (lignes, total filtré)."""
    where, params = ["campaign_id=?"], [campaign_id]
    if status:
        where.append("status=?")
        params.append(status)
    if q:
        where.append("(company_name LIKE ? OR email LIKE ? OR first_name LIKE ? OR last_name LIKE ?)")
        params += [f"%{q}%"] * 4
    clause = " AND ".join(where)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM applications WHERE {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM applications WHERE {clause} "
            "ORDER BY CASE status WHEN 'a_valider' THEN 0 WHEN 'repondu' THEN 1 WHEN 'ouvert' THEN 2 "
            "WHEN 'envoye' THEN 3 WHEN 'programme' THEN 4 ELSE 5 END, "
            "CASE WHEN status IN ('a_valider','programme') THEN scheduled_at END ASC, "
            "COALESCE(sent_at, scheduled_at) DESC "
            "LIMIT ? OFFSET ?", (*params, size, (page - 1) * size))
        return [dict(r) for r in rows], int(total)


def application_status_counts(campaign_id: int) -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM applications WHERE campaign_id=? "
                            "GROUP BY status", (campaign_id,))
        counts = {s: 0 for s in APPLICATION_STATUSES}
        for r in rows:
            counts[r["status"]] = r["n"]
        return counts


def pending_prepared(campaign_id: int) -> list[dict]:
    """Candidatures rédigées et pas encore parties (à valider ou programmées), par créneau."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM applications WHERE campaign_id=? AND status IN ('a_valider','programme') "
            "ORDER BY scheduled_at", (campaign_id,))]


def queued_applications(campaign_id: int, limit: int) -> list[dict]:
    """Les prochaines candidatures de la file, les mieux placées d'abord."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM applications WHERE campaign_id=? AND status='en_attente' "
            "ORDER BY score DESC, id LIMIT ?", (campaign_id, limit))]


def due_applications(limit: int = 5) -> list[dict]:
    """Candidatures programmées dont l'heure est passée, campagne active seulement."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.* FROM applications a JOIN campaigns c ON c.id = a.campaign_id "
            "WHERE a.status='programme' AND c.status='active' AND a.scheduled_at <= ? "
            "ORDER BY a.scheduled_at LIMIT ?", (_now(), limit))]


def sent_since(prefix: str, campaign_id: int | None = None) -> int:
    """Nombre d'envois dont `sent_at` commence par `prefix` (« 2026-09 » ou « 2026-09-17 »)."""
    where, params = ["sent_at LIKE ?"], [prefix + "%"]
    if campaign_id is not None:
        where.append("campaign_id=?")
        params.append(campaign_id)
    with connect() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM applications WHERE {' AND '.join(where)}", params).fetchone()[0])


def last_sent_at() -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT MAX(sent_at) AS m FROM applications").fetchone()
        return row["m"] if row else None


def record_open(token: str) -> dict | None:
    """Enregistre une ouverture. Renvoie la candidature touchée, ou None si jeton inconnu."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM applications WHERE token=?", (token,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE applications SET opens = opens + 1, last_open_at = ?, "
            "status = CASE WHEN status = 'envoye' THEN 'ouvert' ELSE status END WHERE id = ?",
            (_now(), row["id"]))
        return dict(row)


def pending_reply_checks(limit: int = 40) -> list[dict]:
    """Candidatures envoyées dont on n'a pas encore vu de réponse."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM applications WHERE gmail_thread_id IS NOT NULL "
            "AND gmail_message_id != 'simulation' "
            "AND status IN ('envoye', 'ouvert') ORDER BY sent_at DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- journal ---

def add_event(kind: str, detail: str = "", campaign_id: int | None = None,
              application_id: int | None = None) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO events (campaign_id, application_id, kind, at, detail) "
                     "VALUES (?,?,?,?,?)", (campaign_id, application_id, kind, _now(), detail))


def count_events(kind: str, prefix: str) -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM events WHERE kind=? AND at LIKE ?",
                                (kind, prefix + "%")).fetchone()[0])


def list_events(campaign_id: int | None = None, limit: int = 50) -> list[dict]:
    with connect() as conn:
        if campaign_id is None:
            rows = conn.execute(
                "SELECT e.*, a.company_name, a.email FROM events e "
                "LEFT JOIN applications a ON a.id = e.application_id "
                "ORDER BY e.id DESC LIMIT ?", (limit,))
        else:
            rows = conn.execute(
                "SELECT e.*, a.company_name, a.email FROM events e "
                "LEFT JOIN applications a ON a.id = e.application_id "
                "WHERE e.campaign_id=? ORDER BY e.id DESC LIMIT ?", (campaign_id, limit))
        return [dict(r) for r in rows]


REPLY_KINDS = ("refus", "interet", "question", "absence", "autre")


def followup_candidates(days_sent: int = 7, days_opened: int = 5, max_followups: int = 1,
                        limit: int = 30) -> list[dict]:
    """Candidatures sans réponse qui méritent une relance.

    Après `days_opened` jours si le mail a été ouvert (la personne a vu, n'a
    pas répondu), après `days_sent` jours sinon. Jamais sur un refus, jamais
    au-delà de `max_followups`.
    """
    now = datetime.now(timezone.utc)
    since_sent = (now - timedelta(days=days_sent)).isoformat(timespec="seconds")
    since_open = (now - timedelta(days=days_opened)).isoformat(timespec="seconds")
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, c.name AS campaign_name, c.job_title FROM applications a "
            "JOIN campaigns c ON c.id = a.campaign_id "
            "WHERE a.status IN ('envoye', 'ouvert') AND a.gmail_thread_id IS NOT NULL "
            "AND COALESCE(a.followups, 0) < ? "
            "AND ((a.opens > 0 AND a.sent_at <= ?) OR a.sent_at <= ?) "
            "ORDER BY a.opens DESC, a.sent_at ASC LIMIT ?",
            (max_followups, since_open, since_sent, limit))]


def recent_replies(limit: int = 30) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, c.name AS campaign_name, c.job_title FROM applications a "
            "JOIN campaigns c ON c.id = a.campaign_id "
            "WHERE a.replied_at IS NOT NULL ORDER BY a.replied_at DESC LIMIT ?", (limit,))]


def outcome_totals(month_prefix: str | None = None) -> dict:
    """Envoyés, ouverts, réponses par nature, relances — en tout ou sur un mois."""
    where, params = "", []
    if month_prefix:
        where, params = "WHERE sent_at LIKE ?", [month_prefix + "%"]
    with connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FILTER (WHERE status IN ('envoye','ouvert','repondu')) AS sent, "
            f"COUNT(*) FILTER (WHERE opens > 0) AS opened, "
            f"COUNT(*) FILTER (WHERE replied_at IS NOT NULL) AS replied, "
            f"COUNT(*) FILTER (WHERE reply_kind = 'refus') AS refus, "
            f"COUNT(*) FILTER (WHERE reply_kind = 'interet') AS interet, "
            f"COUNT(*) FILTER (WHERE reply_kind = 'question') AS question, "
            f"COALESCE(SUM(followups), 0) AS followups, "
            f"COUNT(*) FILTER (WHERE status = 'programme') AS scheduled, "
            f"COUNT(*) FILTER (WHERE status = 'echec') AS failed "
            f"FROM applications {where}", params).fetchone()
        return dict(row)


def outcomes_by_campaign() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT c.id, c.name, c.job_title, c.status, "
            "COUNT(a.id) FILTER (WHERE a.status IN ('envoye','ouvert','repondu')) AS sent, "
            "COUNT(a.id) FILTER (WHERE a.opens > 0) AS opened, "
            "COUNT(a.id) FILTER (WHERE a.replied_at IS NOT NULL) AS replied, "
            "COUNT(a.id) FILTER (WHERE a.reply_kind = 'refus') AS refus, "
            "COUNT(a.id) FILTER (WHERE a.reply_kind = 'interet') AS interet, "
            "COALESCE(SUM(a.followups), 0) AS followups "
            "FROM campaigns c LEFT JOIN applications a ON a.campaign_id = c.id "
            "GROUP BY c.id ORDER BY c.id DESC")]


def sends_by_day(days: int = 14) -> list[dict]:
    """Envois (et relances) par jour sur les derniers jours, pour la frise du tableau de bord."""
    start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with connect() as conn:
        sent = {r["d"]: r["n"] for r in conn.execute(
            "SELECT substr(sent_at, 1, 10) AS d, COUNT(*) AS n FROM applications "
            "WHERE sent_at >= ? GROUP BY d", (start,))}
        opened = {r["d"]: r["n"] for r in conn.execute(
            "SELECT substr(last_open_at, 1, 10) AS d, COUNT(*) AS n FROM applications "
            "WHERE last_open_at >= ? GROUP BY d", (start,))}
        replied = {r["d"]: r["n"] for r in conn.execute(
            "SELECT substr(replied_at, 1, 10) AS d, COUNT(*) AS n FROM applications "
            "WHERE replied_at >= ? GROUP BY d", (start,))}
    out = []
    for i in range(days):
        day = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        out.append({"day": day, "sent": sent.get(day, 0), "opened": opened.get(day, 0),
                    "replied": replied.get(day, 0)})
    return out


def priority_followups(campaign_id: int, limit: int = 8) -> list[dict]:
    """Les personnes qui ouvrent le plus sans répondre : à relancer en priorité."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM applications WHERE campaign_id=? AND opens > 0 AND status != 'repondu' "
            "ORDER BY opens DESC, last_open_at DESC LIMIT ?", (campaign_id, limit))]


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
