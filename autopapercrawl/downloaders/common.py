from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TITLE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "based", "by", "for", "from", "in",
    "into", "of", "on", "or", "the", "to", "using", "with",
}


class DownloadError(RuntimeError):
    pass


@dataclass
class PaperTarget:
    title: str | None = None
    doi: str | None = None
    pdf_url: str | None = None
    url: str | None = None
    source: str | None = None
    publisher: str | None = None
    proxy_access_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        if self.url:
            return f"url:{self.url}"
        return f"title:{normalize_title(self.title or '')}"


@dataclass
class DownloadResult:
    status: str
    provider: str
    doi: str | None = None
    title: str | None = None
    input_url: str | None = None
    final_url: str | None = None
    path: str | None = None
    reason: str | None = None
    bytes: int | None = None
    page_url: str | None = None
    pdf_url: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    sha256: str | None = None
    pages: int | None = None
    validation: str | None = None
    duplicate_of: str | None = None

    @property
    def outcome(self) -> str:
        if self.status in {"downloaded", "exists"}:
            return "success"
        if self.status == "failed_not_pdf":
            return "invalid_pdf"
        if self.status.startswith("failed"):
            return "request_failed"
        return "no_fulltext"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "doi": self.doi,
            "title": self.title,
            "input_url": self.input_url,
            "final_url": self.final_url,
            "page_url": self.page_url,
            "pdf_url": self.pdf_url,
            "path": self.path,
            "reason": self.reason,
            "bytes": self.bytes,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "sha256": self.sha256,
            "pages": self.pages,
            "validation": self.validation,
            "duplicate_of": self.duplicate_of,
        }


def normalize_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    return value.lower() if value else None


def normalize_title(title: str) -> str:
    return " ".join(title.lower().strip().split())


def safe_filename(target: PaperTarget, suffix: str = ".pdf") -> str:
    identifier = normalize_doi(target.doi) or normalize_title(target.title or "") or target.url or target.pdf_url or "paper"
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
    stem = normalize_doi(target.doi) or normalize_title(target.title or "")
    if not stem:
        stem = "paper"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")
    title = title_keyphrase_slug(target.title or "")
    if title and title.lower() not in stem.lower():
        stem = f"{stem}__{title}"
    return (stem[:164] or "paper") + "__" + digest + suffix


def title_keyphrase_slug(title: str, *, max_words: int = 6, max_len: int = 64) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", title)
    keywords = [
        word.lower()
        for word in words
        if len(word) > 2 and word.lower() not in TITLE_STOPWORDS
    ]
    if not keywords:
        keywords = [word.lower() for word in words[:max_words]]
    phrase = "_".join(keywords[:max_words])
    phrase = re.sub(r"[^A-Za-z0-9._-]+", "_", phrase).strip("_")
    return phrase[:max_len].strip("_")


def ensure_pdf_bytes(path: Path) -> bool:
    from ..pdf_validation import inspect_pdf
    try:
        inspect_pdf(path)
        return True
    except (OSError, ValueError):
        return False


def merge_target(primary: PaperTarget, secondary: PaperTarget) -> PaperTarget:
    return PaperTarget(
        title=primary.title or secondary.title,
        doi=normalize_doi(primary.doi) or normalize_doi(secondary.doi),
        pdf_url=primary.pdf_url or secondary.pdf_url,
        url=primary.url or secondary.url,
        source=primary.source or secondary.source,
        publisher=primary.publisher or secondary.publisher,
        proxy_access_url=primary.proxy_access_url or secondary.proxy_access_url,
        metadata={**secondary.metadata, **primary.metadata},
    )
