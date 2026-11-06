"""Optional, isolated bridge to a user's existing paperCrawler checkout."""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from .models import Paper, normalize_doi


def conference_record(item: dict) -> Paper | None:
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    conf = str(item.get("conf") or "")
    year_match = re.search(r"(\d{4})$", conf)
    pdf_urls = [url.strip() for url in str(item.get("pdf_url") or "").split(",") if url.strip()]
    authors = item.get("authors") or item.get("author") or []
    if isinstance(authors, str):
        authors = [part.strip() for part in authors.split(",") if part.strip()]
    return Paper(title=title, source="paperCrawler", authors=authors,
                 year=int(year_match.group(1)) if year_match else None,
                 doi=normalize_doi(item["doi"]) if item.get("doi") else None, abstract=item.get("abstract") or None,
                 pdf_url=pdf_urls[0] if pdf_urls else None, oa_status="open" if pdf_urls else None,
                 citations=item.get("citation_count") or item.get("citations"), code_url=item.get("code_url"), categories=item.get("categories") or [],
                 metadata={"raw_records": [{"source": "papercrawler", "record": item}], "conference": conf, "pdf_urls": pdf_urls, "sources": ["paperCrawler"], "publication_type": "conference"})


def load_conference_file(path: Path) -> list[Paper]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            items = list(csv.DictReader(handle))
    else:
        items = json.loads(path.read_text(encoding="utf-8-sig"))
    return [paper for item in items if (paper := conference_record(item)) is not None]


def crawl_conferences(checkout: Path, venues: list[str], years: list[int], output_dir: Path, *, timeout: int = 1800, max_items: int = 0) -> tuple[list[Paper], dict]:
    if not (checkout / "crawl_conf" / "scrapy.cfg").is_file():
        raise ValueError(f"Not a paperCrawler checkout: {checkout}")
    # A fresh subdirectory prevents a timed-out run from importing stale feeds.
    output_dir = output_dir / uuid.uuid4().hex[:12]
    output_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve().parents[1] / "scripts" / "papercrawler_bridge.py"
    output = (output_dir / "papers.json").resolve()
    stats = (output_dir / "stats.json").resolve()
    command = [sys.executable, str(script), "--checkout", str(checkout.resolve()), "--venues", ",".join(venues),
               "--years", ",".join(map(str, years)), "--output", str(output), "--stats", str(stats), "--max-items", str(max_items)]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with (output_dir / "crawl.log").open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run(command, stdout=log, stderr=log, env=env, timeout=timeout, check=False)
            report = json.loads(stats.read_text(encoding="utf-8")) if stats.exists() else {}
            report["returncode"] = result.returncode
        except subprocess.TimeoutExpired:
            report = {"status": "timeout", "reason": f"Conference crawl exceeded {timeout}s; see crawl.log"}
    report["log"] = str((output_dir / "crawl.log").resolve())
    try:
        papers = load_conference_file(output) if output.exists() else []
    except json.JSONDecodeError:
        papers = []
        report["reason"] = "Conference export incomplete; see crawl.log"
    if not papers:
        # JSONL feeds preserve completed records when the timeout kills Scrapy
        # before its final combined JSON and statistics can be written.
        for venue in venues:
            feed = output_dir / f"{venue.strip()}.jsonl"
            if not feed.exists():
                continue
            for line in feed.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                paper = conference_record(item)
                if paper:
                    papers.append(paper)
        report["recovered_records"] = len(papers)
    return papers, report
