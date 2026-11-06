from __future__ import annotations

import re
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..pdf_validation import inspect_pdf, content_duplicate, DEFAULT_MAX_BYTES
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
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> DownloadResult:
    url = target_pdf_url(target)
    if not url:
        return DownloadResult(status="skipped_no_pdf_url", provider=provider_name, doi=target.doi, title=target.title)

    pdf_dir.mkdir(parents=True, exist_ok=True)
    output_path = pdf_dir / safe_filename(target)
    if output_path.exists() and output_path.stat().st_size > 0 and ensure_pdf_bytes(output_path):
        return DownloadResult(status="exists", provider=provider_name, doi=target.doi, title=target.title, input_url=url, path=str(output_path), **inspect_pdf(output_path, max_bytes))

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
                if "text/html" in response.headers.get("Content-Type", "").lower():
                    return DownloadResult(status="failed_not_pdf", provider=provider_name, doi=target.doi, title=target.title, input_url=url, final_url=response.url, reason="站点返回 HTML 页面，未保存为 PDF")
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError(f"PDF 超过下载上限 {max_bytes} bytes")
                written = 0
                with tmp_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            written += len(chunk)
                            if written > max_bytes:
                                raise ValueError(f"PDF 超过下载上限 {max_bytes} bytes")
                            handle.write(chunk)
                info = inspect_pdf(tmp_path, max_bytes)
                duplicate = content_duplicate(tmp_path, info)
                if duplicate:
                    tmp_path.unlink(missing_ok=True)
                    try:
                        os.link(duplicate, tmp_path)
                    except OSError:
                        import shutil
                        shutil.copy2(duplicate, tmp_path)
                tmp_path.replace(output_path)
                return DownloadResult(status="downloaded", provider=provider_name, doi=target.doi, title=target.title, input_url=url, final_url=response.url,
                                      path=str(output_path), duplicate_of=str(duplicate) if duplicate else None, **info)
        except ValueError as exc:
            tmp_path.unlink(missing_ok=True)
            return DownloadResult(status="failed_not_pdf", provider=provider_name, doi=target.doi, title=target.title, input_url=url, reason=str(exc))
        except (requests.RequestException, OSError) as exc:
            tmp_path.unlink(missing_ok=True)
            last_error = str(exc)
            if attempt < retries:
                time.sleep(max(sleep_seconds, 2 ** attempt))
                continue
    return DownloadResult(status="failed", provider=provider_name, doi=target.doi, title=target.title, input_url=url, reason=last_error)


def is_cloudflare_challenge(response: requests.Response) -> bool:
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    text = response.raw.read(2048, decode_content=True).decode("utf-8", errors="replace").lower() if "text/html" in response.headers.get("Content-Type", "").lower() else ""
    return "challenges.cloudflare.com" in text or "enable javascript and cookies to continue" in text
