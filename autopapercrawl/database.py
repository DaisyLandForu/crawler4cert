"""Additive SQLite migration, backups, indexed filters and portable exports."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .topics import normalized_text

SCHEMA_VERSION = 3
PAPER_COLUMNS = {"year": "INTEGER", "venue_text": "TEXT", "author_text": "TEXT", "abstract_text": "TEXT", "citations": "INTEGER",
                 "outcome": "TEXT", "publication_status": "TEXT", "has_code": "INTEGER", "direction_text": "TEXT", "search_text": "TEXT", "updated_at": "TEXT"}


@contextmanager
def connection(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def backup_database(database):
    database = Path(database)
    destination = database.parent / "backups" / (database.stem + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6] + ".sqlite3")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with connection(database) as source, connection(destination) as target:
        source.backup(target)
    return str(destination)


def migrate(database):
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    with connection(database) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        existing = conn.execute("SELECT name FROM sqlite_master WHERE name='papers'").fetchone()
    if version > SCHEMA_VERSION:
        raise ValueError("数据库版本高于程序版本，请先升级程序")
    if version == SCHEMA_VERSION:
        return None
    backup = backup_database(database) if existing else None
    with connection(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY,paper_key TEXT UNIQUE,title_key TEXT,doi TEXT,document TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,document TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT)")
        present = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
        for name, sql_type in PAPER_COLUMNS.items():
            if name not in present:
                conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {sql_type}")
        present = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name in ("status", "created"):
            if name not in present:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
        for field in ("doi", "year", "title_key", "venue_text", "outcome", "publication_status"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS papers_{field}_idx ON papers({field})")
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status,created)")
        conn.execute("CREATE TABLE IF NOT EXISTS source_states (source TEXT,venue TEXT,last_success TEXT,job_id TEXT,document TEXT,PRIMARY KEY(source,venue))")
        conn.execute("CREATE TABLE IF NOT EXISTS checkpoints (plan_key TEXT PRIMARY KEY,last_success TEXT,job_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS deleted_papers (id TEXT,document TEXT,deleted_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS paper_identifiers (identifier TEXT PRIMARY KEY,paper_id TEXT NOT NULL)")
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(id UNINDEXED,title,authors,abstract)")
        except sqlite3.OperationalError:
            pass
        for identifier, doc in conn.execute("SELECT id,document FROM papers").fetchall():
            index_record(conn, identifier, json.loads(doc))
        for identifier, doc in conn.execute("SELECT id,document FROM jobs").fetchall():
            item = json.loads(doc)
            conn.execute("UPDATE jobs SET status=?,created=? WHERE id=?", (item.get("status"), item.get("created"), identifier))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return backup


def index_record(conn, identifier, record):
    meta = record.get("metadata") or {}
    download = record.get("download") or {}
    outcome = download.get("outcome") or ("success" if download.get("status") in {"downloaded", "exists"} else "pending")
    values = {"year": record.get("year"), "venue_text": " | ".join(record.get("venues") or [meta.get("venue") or ""]),
              "author_text": normalized_text(" ".join(record.get("authors") or [])), "abstract_text": normalized_text(record.get("abstract") or ""),
              "citations": record.get("citations") or 0, "outcome": outcome,
              "publication_status": record.get("publication_status") or meta.get("publication_type") or "unknown",
              "has_code": int(bool(record.get("code_url") or meta.get("code_url"))), "direction_text": "|" + "|".join(record.get("directions") or []) + "|",
              "search_text": normalized_text(record.get("title", "") + " " + (record.get("abstract") or "") + " " + " ".join(record.get("authors") or [])),
              "updated_at": datetime.now(timezone.utc).isoformat()}
    conn.execute("UPDATE papers SET " + ",".join(f"{name}=?" for name in values) + " WHERE id=?", [*values.values(), identifier])
    try:
        conn.execute("DELETE FROM papers_fts WHERE id=?", (identifier,))
        conn.execute("INSERT INTO papers_fts VALUES (?,?,?,?)", (identifier, record.get("title", ""), " ".join(record.get("authors") or []), record.get("abstract") or ""))
    except sqlite3.OperationalError:
        pass
    from .identity import identifiers
    from .models import Paper
    paper = Paper(**{key: record[key] for key in Paper.__dataclass_fields__ if key in record})
    for value in identifiers(paper):
        conn.execute("INSERT OR IGNORE INTO paper_identifiers VALUES (?,?)", (value, identifier))


def sql_filter(conn, filters, registry, job=None):
    where, values = [], []
    def add(expression, value):
        where.append(expression)
        values.append(value)
    if filters.get("start_year"):
        add("year>=?", int(filters["start_year"]))
    if filters.get("end_year"):
        add("year<=?", int(filters["end_year"]))
    for field, column in (("title", "title_key"), ("author", "author_text"), ("abstract", "abstract_text"), ("query", "search_text")):
        if filters.get(field):
            add(f"instr({column},?)>0", normalized_text(filters[field]))
    for key, column in (("status", "outcome"), ("publication_status", "publication_status")):
        if filters.get(key):
            add(f"{column}=?", filters[key])
    if str(filters.get("has_code", "")).lower() in {"true", "1", "false", "0"}:
        add("has_code=?", int(str(filters["has_code"]).lower() in {"true", "1"}))
    directions = filters.get("directions") or []
    if isinstance(directions, str):
        directions = directions.split(",")
    if directions:
        where.append("(" + " OR ".join("instr(direction_text,?)>0" for _ in directions) + ")")
        values.extend("|" + key + "|" for key in directions)
    venues = filters.get("venues") or []
    if isinstance(venues, str):
        venues = venues.split(";")
    if venues:
        from .venues import venue_matches
        conn.create_function("matches_venue", 1, lambda doc: int(any(venue_matches(v, wanted, registry) for v in json.loads(doc).get("venues", []) for wanted in venues)))
        where.append("matches_venue(document)=1")
    if filters.get("confidence"):
        confidence = filters["confidence"]
        def has_confidence(doc):
            meta = json.loads(doc).get("metadata", {})
            levels = {value.get("confidence") for value in meta.get("classification", {}).values()}
            overall = meta.get("review_confidence") or ("high" if "high" in levels else "medium" if "medium" in levels else "low")
            return int(overall == confidence)
        conn.create_function("has_confidence", 1, has_confidence)
        where.append("has_confidence(document)=1")
    if filters.get("ids"):
        requested = filters["ids"].split(",") if isinstance(filters["ids"], str) else filters["ids"]
        requested = set(requested)
        conn.create_function("in_selection", 1, lambda value: int(value in requested))
        where.append("in_selection(id)=1")
    if job is not None:
        wanted = set(job.get("paper_ids", []))
        conn.create_function("in_job", 1, lambda value: int(value in wanted))
        where.append("in_job(id)=1")
    return (" WHERE " + " AND ".join(where)) if where else "", values
