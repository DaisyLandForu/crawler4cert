from __future__ import annotations

from dataclasses import dataclass, field, asdict
import hashlib
import re
from typing import Any


@dataclass
class Paper:
    title: str
    source: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    citations: int | None = None
    pdf_url: str | None = None
    oa_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    affiliations: list[str] = field(default_factory=list)
    venue: str | None = None
    venue_type: str | None = None
    arxiv_id: str | None = None
    dblp_id: str | None = None
    keywords: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    concepts: list[Any] = field(default_factory=list)
    code_url: str | None = None
    local_pdf: str | None = None
    sources: list[str] = field(default_factory=list)
    classification: dict[str, Any] = field(default_factory=dict)
    download_status: str | None = None
    publication_status: str | None = None
    versions: list[dict[str, Any]] = field(default_factory=list)
    paper_id: str | None = None

    def __post_init__(self):
        for name in ("year", "citations"):
            value = getattr(self, name)
            if value is not None:
                try:
                    setattr(self, name, int(value))
                except (TypeError, ValueError):
                    setattr(self, name, None)
        if self.doi:
            self.doi = normalize_doi(self.doi)
        for name in ("venue", "venue_type", "arxiv_id", "dblp_id", "code_url", "categories", "concepts", "affiliations", "keywords", "classification", "versions"):
            if not getattr(self, name) and self.metadata.get(name):
                setattr(self, name, self.metadata[name])
        if not self.code_url:
            match = re.search(r"https?://(?:github\.com|gitlab\.com|codeberg\.org)/[^\s<>\"{}]+", self.abstract or "")
            if match:
                self.code_url = match.group(0).rstrip(".,);]")
        self.sources = list(dict.fromkeys([self.source, *self.sources, *self.metadata.get("sources", [])]))
        publication_type = self.metadata.get("publication_type") or "unknown"
        self.publication_status = self.publication_status or publication_type
        if not self.venue_type:
            self.venue_type = {"preprint": "preprint", "conference": "conference", "proceedings-article": "conference", "journal": "journal", "journal-article": "journal"}.get(publication_type)
        self.paper_id = self.paper_id or hashlib.sha256(self.key.encode()).hexdigest()[:24]

    @property
    def key(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        if self.arxiv_id or self.metadata.get("arxiv_id"):
            return "arxiv:" + re.sub(r"v\d+$", "", self.arxiv_id or self.metadata["arxiv_id"])
        if self.dblp_id or self.metadata.get("dblp_id"):
            return "dblp:" + (self.dblp_id or self.metadata["dblp_id"])
        return f"title:{normalize_title(self.title)}|{self.year or ''}|{normalize_title(self.authors[0]) if self.authors else ''}"

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for name in ("classification", "versions", "venue", "arxiv_id", "dblp_id", "code_url", "affiliations", "keywords", "categories", "concepts"):
            if self.metadata.get(name):
                result[name] = self.metadata[name]
        result["sources"] = list(dict.fromkeys([*self.sources, *self.metadata.get("sources", [])]))
        return result


def normalize_doi(doi: str) -> str:
    doi = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
    return doi.strip()


def normalize_title(title: str) -> str:
    return " ".join(title.lower().strip().split())
