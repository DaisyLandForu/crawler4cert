from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Paper


SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    authors_json TEXT NOT NULL,
    year INTEGER,
    doi TEXT,
    url TEXT,
    abstract TEXT,
    citations INTEGER,
    pdf_url TEXT,
    oa_status TEXT,
    download_status TEXT,
    file_path TEXT,
    reason TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    return conn


def upsert_paper(
    conn: sqlite3.Connection,
    paper: Paper,
    *,
    download_status: str | None,
    file_path: str | None,
    reason: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO papers (
            title, source, authors_json, year, doi, url, abstract, citations,
            pdf_url, oa_status, download_status, file_path, reason, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(title) DO UPDATE SET
            source=excluded.source,
            authors_json=excluded.authors_json,
            year=excluded.year,
            doi=COALESCE(papers.doi, excluded.doi),
            url=COALESCE(papers.url, excluded.url),
            abstract=COALESCE(papers.abstract, excluded.abstract),
            citations=MAX(COALESCE(papers.citations, 0), COALESCE(excluded.citations, 0)),
            pdf_url=COALESCE(papers.pdf_url, excluded.pdf_url),
            oa_status=COALESCE(papers.oa_status, excluded.oa_status),
            download_status=excluded.download_status,
            file_path=excluded.file_path,
            reason=excluded.reason,
            metadata_json=excluded.metadata_json
        """,
        (
            paper.title,
            paper.source,
            json.dumps(paper.authors, ensure_ascii=False),
            paper.year,
            paper.doi,
            paper.url,
            paper.abstract,
            paper.citations,
            paper.pdf_url,
            paper.oa_status,
            download_status,
            file_path,
            reason,
            json.dumps(paper.metadata, ensure_ascii=False),
        ),
    )
    conn.commit()
