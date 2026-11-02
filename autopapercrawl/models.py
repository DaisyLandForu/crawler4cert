from __future__ import annotations

from dataclasses import dataclass, field
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

    @property
    def key(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        return f"title:{normalize_title(self.title)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "authors": self.authors,
            "year": self.year,
            "doi": self.doi,
            "url": self.url,
            "abstract": self.abstract,
            "citations": self.citations,
            "pdf_url": self.pdf_url,
            "oa_status": self.oa_status,
            "metadata": self.metadata,
        }


def normalize_doi(doi: str) -> str:
    doi = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
    return doi.strip()


def normalize_title(title: str) -> str:
    return " ".join(title.lower().strip().split())
