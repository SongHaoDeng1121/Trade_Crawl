from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT,
    website TEXT NOT NULL UNIQUE,
    source TEXT,
    country TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    page_text TEXT,
    emails TEXT,
    phones TEXT,
    score INTEGER NOT NULL DEFAULT 0,
    llm_fit TEXT,
    llm_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS suppression_list (
    email TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country TEXT NOT NULL,
    query TEXT NOT NULL,
    base_query TEXT,
    city TEXT,
    round_no INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'manual',
    result_count INTEGER NOT NULL DEFAULT 0,
    unique_website_count INTEGER NOT NULL DEFAULT 0,
    new_website_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    searched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discovery_country_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    target_count INTEGER NOT NULL,
    lead_count_before INTEGER NOT NULL DEFAULT 0,
    query_count INTEGER NOT NULL DEFAULT 0,
    result_count INTEGER NOT NULL DEFAULT 0,
    new_website_count INTEGER NOT NULL DEFAULT 0,
    failed_query_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(country, round_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_email_drafts_lead_recipient
ON email_drafts (lead_id, lower(recipient));

CREATE INDEX IF NOT EXISTS idx_discovery_runs_country_query
ON discovery_runs (country, query, searched_at);

CREATE INDEX IF NOT EXISTS idx_discovery_country_rounds_country
ON discovery_country_rounds (country, round_no);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    ensure_column(conn, "leads", "country", "TEXT")
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def upsert_leads(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    count = 0
    for row in rows:
        website = normalize_url(row["website"])
        if not website:
            continue
        conn.execute(
            """
            INSERT INTO leads (company_name, website, source, country)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(website) DO UPDATE SET
                company_name = COALESCE(excluded.company_name, leads.company_name),
                source = COALESCE(excluded.source, leads.source),
                country = COALESCE(NULLIF(excluded.country, ''), leads.country),
                updated_at = CURRENT_TIMESTAMP
            """,
            (row.get("company_name"), website, row.get("source"), row.get("country", "")),
        )
        count += 1
    conn.commit()
    return count


def normalize_url(url: str) -> str:
    clean = (url or "").strip()
    if not clean:
        return ""
    if not clean.startswith(("http://", "https://")):
        clean = "https://" + clean
    return clean.rstrip("/")
