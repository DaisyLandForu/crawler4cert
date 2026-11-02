from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DOI_RE = re.compile(
    r"(?:doi[:\s]*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
    re.IGNORECASE,
)
ARXIV_RE = re.compile(
    r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
    re.IGNORECASE,
)


def extract_seed_identifiers(paths: list[Path]) -> tuple[list[str], list[str]]:
    dois: set[str] = set()
    arxiv_ids: set[str] = set()

    for path in paths:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            try:
                text = flatten_json_text(json.loads(text))
            except json.JSONDecodeError:
                pass
        for doi in DOI_RE.findall(text):
            dois.add(clean_doi(doi))
        for arxiv_id in ARXIV_RE.findall(text):
            arxiv_ids.add(arxiv_id.rstrip("."))

    return sorted(dois), sorted(arxiv_ids)


def flatten_json_text(value: Any) -> str:
    chunks: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for item in node.values():
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, str):
            chunks.append(node)
        elif node is not None:
            chunks.append(str(node))

    visit(value)
    return "\n".join(chunks)


def clean_doi(doi: str) -> str:
    return doi.strip().rstrip(".,;:)]}>").lower()
