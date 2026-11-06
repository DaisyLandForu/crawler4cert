"""Resolve public PDF alternatives without a citation-count prerequisite."""
from __future__ import annotations

import re
from dataclasses import replace
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from .downloaders.common import DownloadResult, PaperTarget, safe_filename, ensure_pdf_bytes
from .downloaders.pdf_url import download_pdf_url, target_pdf_url
from .http import HttpClient, urlencode
from .pdf_validation import inspect_pdf, DEFAULT_MAX_BYTES
from .search import paper_from_openalex, search_arxiv


PUBLIC_PAGE_HOSTS = ("usenix.org", "ndss-symposium.org", "thecvf.com", "openreview.net", "arxiv.org")


class PDFLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []
        self.citation_urls: list[str] = []
        self.anchor_url = None
        self.anchor_text = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "meta" and (fields.get("name") or "").lower() == "citation_pdf_url":
            if fields.get("content"):
                self.citation_urls.append(fields["content"])
        if tag == "a" and re.search(r"\.pdf(?:[?#]|$)", fields.get("href") or "", re.I):
            self.anchor_url = fields["href"]
            self.anchor_text = []

    def handle_data(self, data):
        if self.anchor_url:
            self.anchor_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor_url:
            description = " ".join(self.anchor_text).lower()
            if any(word in description for word in ("paper", "full text", "pdf", "download")) and not any(word in (description + self.anchor_url.lower()) for word in ("slides", "poster", "supplement", "appendix", "presentation")):
                self.urls.append(self.anchor_url)
            self.anchor_url = None


def usable_url(url: str | None) -> bool:
    return bool(url and urlparse(url).scheme in {"http", "https"} and urlparse(url).hostname)


def prioritize_urls(urls: list[str]) -> list[str]:
    unique = list(dict.fromkeys(url for url in urls if usable_url(url)))
    # Public preprint and conference copies are preferable to publisher URLs.
    return sorted(unique, key=lambda url: 0 if any(host == (urlparse(url).hostname or "") or (urlparse(url).hostname or "").endswith("." + host) for host in ("arxiv.org", "openreview.net", "usenix.org", "thecvf.com")) else 1)


def metadata_urls(target: PaperTarget) -> list[str]:
    urls = [target_pdf_url(target), *target.metadata.get("pdf_urls", [])]
    arxiv_id = target.metadata.get("arxiv_id")
    if arxiv_id:
        urls.append(f"https://arxiv.org/pdf/{arxiv_id}")
    elif target.url:
        match = re.search(r"arxiv\.org/abs/([^?#]+)", target.url)
        if match:
            urls.append(f"https://arxiv.org/pdf/{match.group(1)}")
    return prioritize_urls(urls)


def resolve_public_urls(target: PaperTarget, client: HttpClient, email: str | None, attempts: list[dict]) -> list[str]:
    urls = []
    if target.doi:
        try:
            data = client.get_json("https://api.openalex.org/works/" + quote("https://doi.org/" + target.doi, safe="") + "?" + urlencode({"mailto": email}))
            paper = paper_from_openalex(data)
            urls.extend([paper.pdf_url, *paper.metadata.get("pdf_urls", [])])
            attempts.append({"stage": "openalex_lookup", "status": "ok"})
        except Exception as exc:
            attempts.append({"stage": "openalex_lookup", "status": "request_failed", "reason": str(exc)})
        if email:
            try:
                data = client.get_json("https://api.unpaywall.org/v2/" + quote(target.doi, safe="") + "?" + urlencode({"email": email}))
                for location in [data.get("best_oa_location"), *(data.get("oa_locations") or [])]:
                    if location and location.get("url_for_pdf"):
                        urls.append(location["url_for_pdf"])
                attempts.append({"stage": "unpaywall_lookup", "status": "ok"})
            except Exception as exc:
                attempts.append({"stage": "unpaywall_lookup", "status": "request_failed", "reason": str(exc)})

    host = (urlparse(target.url or "").hostname or "").lower()
    if target.url and any(host == allowed or host.endswith("." + allowed) for allowed in PUBLIC_PAGE_HOSTS):
        try:
            parser = PDFLinks()
            parser.feed(client.get_bytes(target.url, headers={"Accept": "text/html"}).decode("utf-8", errors="replace"))
            urls.extend(urljoin(target.url, url) for url in (parser.citation_urls or parser.urls))
            attempts.append({"stage": "public_page_lookup", "status": "ok", "url": target.url})
        except Exception as exc:
            attempts.append({"stage": "public_page_lookup", "status": "request_failed", "reason": str(exc)})

    # Exact-title fallback is deliberately conservative: an unrelated preprint
    # must never be silently substituted for the requested paper.
    return prioritize_urls(urls)


def resolve_arxiv_title_urls(target: PaperTarget, client: HttpClient, attempts: list[dict]) -> list[str]:
    urls = []
    if target.title:
        title = re.sub(r'[^\w\s-]', " ", target.title).strip()
        if title:
            try:
                candidates = search_arxiv(f'ti:"{title}"', 3, None, None, client, page_size=3, sort_by="relevance")
                wanted = normalize_title(target.title)
                for paper in candidates:
                    got = normalize_title(paper.title)
                    authors = target.metadata.get("authors") or []
                    shared_author = bool({normalize_title(author) for author in authors} & {normalize_title(author) for author in paper.authors})
                    year = target.metadata.get("year")
                    compatible_year = not year or not paper.year or abs(int(year) - paper.year) <= 2
                    compatible_doi = not target.doi or not paper.doi or target.doi.lower() == paper.doi.lower()
                    if compatible_year and compatible_doi and (wanted == got or (shared_author and SequenceMatcher(None, wanted, got).ratio() >= 0.96)):
                        urls.append(paper.pdf_url)
                attempts.append({"stage": "arxiv_title_lookup", "status": "ok"})
            except Exception as exc:
                attempts.append({"stage": "arxiv_title_lookup", "status": "request_failed", "reason": str(exc)})
    return prioritize_urls(urls)


def normalize_title(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.lower()))


def download_public_pdf(
    target: PaperTarget, pdf_dir: Path, client: HttpClient, *, email: str | None = None,
    lookup: bool = True, retries: int = 2, sleep_seconds: float = 3.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> DownloadResult:
    existing = pdf_dir / safe_filename(target)
    if ensure_pdf_bytes(existing) and existing.stat().st_size <= max_bytes:
        return DownloadResult(status="exists", provider="open_access", title=target.title, doi=target.doi,
                              path=str(existing), **inspect_pdf(existing))
    attempts: list[dict] = []
    tried: set[str] = set()
    failures: list[DownloadResult] = []

    def try_urls(urls):
        for url in urls:
            if url in tried:
                continue
            tried.add(url)
            client.wait(url)
            result = download_pdf_url(replace(target, pdf_url=url), pdf_dir, timeout=client.timeout_seconds,
                                      retries=retries, sleep_seconds=sleep_seconds, provider_name="open_access", max_bytes=max_bytes)
            attempts.append({"stage": "pdf_download", "url": url, "status": result.status, "reason": result.reason})
            if result.status in {"downloaded", "exists"}:
                result.attempts = attempts
                return result
            failures.append(result)
        return None

    result = try_urls(metadata_urls(target))
    if result:
        return result
    if lookup:
        result = try_urls(resolve_public_urls(target, client, email, attempts))
        if result:
            return result
        result = try_urls(resolve_arxiv_title_urls(target, client, attempts))
        if result:
            return result
    if failures:
        # Keep an actual request failure when a later lookup found no URL.
        result = failures[-1]
        result.attempts = attempts
        return result
    lookup_failed = any(attempt["status"] == "request_failed" for attempt in attempts)
    return DownloadResult(status="failed_lookup" if lookup_failed else "no_fulltext", provider="open_access",
                          title=target.title, doi=target.doi, attempts=attempts,
                          reason="Public full-text lookup failed; see attempts" if lookup_failed else "No accessible public PDF route found; this does not prove no full text exists")
