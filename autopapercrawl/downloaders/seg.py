from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse

import requests

from .common import DownloadResult, PaperTarget, normalize_doi, safe_filename
from .geoscienceworld import DEFAULT_USER_AGENT, DownloadError, load_cookie_file


PDF_PATTERNS = [
    re.compile(r'href=["\']([^"\']+?/(?:doi/)?(?:pdf|epdf)/[^"\']+)["\']', re.I),
    re.compile(r'href=["\']([^"\']+?\.pdf(?:\?[^"\']*)?)["\']', re.I),
    re.compile(r'content=["\']([^"\']+?\.pdf(?:\?[^"\']*)?)["\']', re.I),
]


def build_session(cookie_file: str | None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
            "Accept-Encoding": "identity",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "close",
        }
    )
    load_cookie_file(session, cookie_file)
    return session


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def resolve_doi(session: requests.Session, doi: str, timeout: int) -> str | None:
    response = session.get(
        f"https://doi.org/{quote(doi, safe='/')}",
        allow_redirects=True,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.url


def find_pdf_candidates(page_url: str, html: str, doi: str | None = None) -> list[str]:
    candidates: list[str] = []
    text = unescape(html)
    for pattern in PDF_PATTERNS:
        candidates.extend(urljoin(page_url, match) for match in pattern.findall(text))

    parsed = urlparse(page_url)
    if "/doi/" in parsed.path:
        candidates.append(urljoin(page_url, parsed.path.replace("/doi/", "/doi/pdf/")))
        candidates.append(urljoin(page_url, parsed.path.replace("/doi/", "/doi/epdf/")))
    if doi:
        encoded = quote(doi, safe="/")
        candidates.append(f"https://library.seg.org/doi/pdf/{encoded}")
        candidates.append(f"https://library.seg.org/doi/epdf/{encoded}")
    return unique(candidates)


def looks_like_pdf(response: requests.Response, content: bytes) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    return content.startswith(b"%PDF-") or "application/pdf" in content_type


def try_download_pdf(
    session: requests.Session,
    candidates: list[str],
    output_path: Path,
    timeout: int,
) -> tuple[str, str]:
    errors: list[str] = []
    for url in candidates:
        try:
            response = session.get(url, allow_redirects=True, timeout=timeout)
            response.raise_for_status()
            content = response.content
            if not looks_like_pdf(response, content):
                content_type = response.headers.get("Content-Type", "")
                errors.append(f"{url}: not PDF ({content_type or 'unknown content-type'})")
                continue
            output_path.write_bytes(content)
            return url, response.url
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
    raise DownloadError("\n".join(errors[-5:]) if errors else "no SEG PDF candidates found")


def download_seg_pdf(
    target: PaperTarget,
    pdf_dir: Path,
    *,
    timeout: int = 30,
    cookie_file: str | None = None,
) -> DownloadResult:
    if not target.doi and not (target.url or target.proxy_access_url):
        return DownloadResult(status="skipped_no_doi_or_url", provider="seg", title=target.title)

    doi = normalize_doi(target.doi)
    output_path = pdf_dir / safe_filename(PaperTarget(title=target.title, doi=doi, url=target.url or target.proxy_access_url))
    if output_path.exists() and output_path.stat().st_size > 0:
        return DownloadResult(
            status="exists",
            provider="seg",
            doi=doi,
            title=target.title,
            path=str(output_path),
            bytes=output_path.stat().st_size,
        )

    try:
        session = build_session(cookie_file)
        page_url = target.url or target.proxy_access_url
        candidates: list[str] = []
        if target.pdf_url:
            candidates.append(target.pdf_url)
        if doi:
            candidates.extend(find_pdf_candidates("https://library.seg.org", "", doi))
            try:
                resolved = resolve_doi(session, doi, timeout)
                if resolved:
                    page_url = resolved
            except requests.RequestException:
                pass
        if page_url:
            page = session.get(page_url, allow_redirects=True, timeout=timeout)
            page.raise_for_status()
            candidates.extend(find_pdf_candidates(page.url, page.text, doi))
            page_url = page.url

        candidates = unique(candidates)
        if not candidates:
            raise DownloadError("no SEG PDF candidates found")

        pdf_url, final_url = try_download_pdf(session, candidates, output_path, timeout)
        return DownloadResult(
            status="downloaded",
            provider="seg",
            doi=doi,
            title=target.title,
            page_url=page_url,
            pdf_url=pdf_url,
            final_url=final_url,
            path=str(output_path),
            bytes=output_path.stat().st_size if output_path.exists() else None,
        )
    except Exception as exc:
        return DownloadResult(status="failed", provider="seg", doi=doi, title=target.title, reason=str(exc))
