from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .common import DownloadResult, PaperTarget, ensure_pdf_bytes, safe_filename


ARXIV_ABS_RE = re.compile(r"arxiv\.org/abs/([^?#]+)", re.I)
DEFAULT_USER_AGENT = "AutoPaperCrawl/0.1 (PDF URL downloader)"


def target_pdf_url(target: PaperTarget) -> str | None:
    if target.pdf_url:
        return target.pdf_url
    arxiv_id = target.metadata.get("arxiv_id") if isinstance(target.metadata, dict) else None
    if arxiv_id:
        return f"https://arxiv.org/pdf/{arxiv_id}"
    if target.url:
        match = ARXIV_ABS_RE.search(target.url)
        if match:
            return f"https://arxiv.org/pdf/{match.group(1)}"
    return None


def download_pdf_url(
    target: PaperTarget,
    pdf_dir: Path,
    *,
    timeout: float,
    retries: int,
    sleep_seconds: float,
    user_agent: str = DEFAULT_USER_AGENT,
    provider_name: str = "pdf_url",
) -> DownloadResult:
    url = target_pdf_url(target)
    if not url:
        return DownloadResult(status="skipped_no_pdf_url", provider=provider_name, doi=target.doi, title=target.title)

    pdf_dir.mkdir(parents=True, exist_ok=True)
    output_path = pdf_dir / safe_filename(target)
    if output_path.exists() and output_path.stat().st_size > 0 and ensure_pdf_bytes(output_path):
        return DownloadResult(status="exists", provider=provider_name, doi=target.doi, title=target.title, input_url=url, path=str(output_path), bytes=output_path.stat().st_size)

    headers = {"User-Agent": user_agent, "Accept": "application/pdf,*/*;q=0.8"}
    last_error = None
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    for attempt in range(retries + 1):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as response:
                if is_cloudflare_challenge(response):
                    return DownloadResult(
                        status="failed_cloudflare_challenge",
                        provider=provider_name,
                        doi=target.doi,
                        title=target.title,
                        input_url=url,
                        final_url=response.url,
                        reason="Cloudflare managed challenge requires a browser session/cookies.",
                    )
                response.raise_for_status()
                with tmp_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            handle.write(chunk)
                if not ensure_pdf_bytes(tmp_path):
                    tmp_path.unlink(missing_ok=True)
                    return DownloadResult(status="failed_not_pdf", provider=provider_name, doi=target.doi, title=target.title, input_url=url, final_url=response.url, reason=f"content_type={response.headers.get('Content-Type')}")
                tmp_path.replace(output_path)
                return DownloadResult(status="downloaded", provider=provider_name, doi=target.doi, title=target.title, input_url=url, final_url=response.url, path=str(output_path), bytes=output_path.stat().st_size)
        except (requests.RequestException, OSError) as exc:
            tmp_path.unlink(missing_ok=True)
            last_error = str(exc)
            if attempt < retries:
                time.sleep(sleep_seconds)
                continue
    return DownloadResult(status="failed", provider=provider_name, doi=target.doi, title=target.title, input_url=url, reason=last_error)


def is_cloudflare_challenge(response: requests.Response) -> bool:
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    text = response.text[:2000].lower() if "text/html" in response.headers.get("Content-Type", "").lower() else ""
    return "challenges.cloudflare.com" in text or "enable javascript and cookies to continue" in text
