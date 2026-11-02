#!/usr/bin/env python3
"""
Download GeoScienceWorld papers by DOI.

Default mode is designed for a campus VPN client: resolve the DOI to the real
publisher URL, then download from pubs.geoscienceworld.org. WebVPN rewritten
URLs are supported only when you provide an already-authenticated cookie file.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import re
import sys
import time
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse

import requests

from .common import DownloadResult, PaperTarget, safe_filename as target_safe_filename


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

WEBVPN_LOGIN_HOST = "vpn.cdut.edu.cn"
WEBVPN_GSW_HOST = "pubs-geoscienceworld-org-s.vpn.cdut.edu.cn"
PDF_PATTERNS = [
    re.compile(r'href=["\']([^"\']+?/(?:article-pdf|pdf|content-pdf)/[^"\']+)["\']', re.I),
    re.compile(r'href=["\']([^"\']+?\.pdf(?:\?[^"\']*)?)["\']', re.I),
    re.compile(r'content=["\']([^"\']+?\.pdf(?:\?[^"\']*)?)["\']', re.I),
]
JS_COMMENT_PATTERN = re.compile(r"/\*.*?\*/|//[^\r\n]*", re.S)
KEY_COOKIE_PATTERN = re.compile(
    r'document\.cookie\s*=\s*"KEY="\s*\+\s*n\s*\+\s*"\*"\s*\+\s*p/n\s*\+\s*":'
    r'"\s*\+\s*s\s*\+\s*":(\d+):1;path=/;"',
    re.S,
)


class DownloadError(RuntimeError):
    pass


def normalize_doi(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    if not value or "/" not in value:
        raise DownloadError(f"DOI 格式不对: {raw!r}")
    return value


def safe_filename(doi: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", doi).strip("_")
    return f"{name}.pdf"


def load_cookie_file(session: requests.Session, cookie_file: str | None) -> None:
    if not cookie_file:
        return

    path = Path(cookie_file)
    if not path.exists():
        raise DownloadError(f"Cookie 文件不存在: {path}")

    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(str(path), ignore_discard=True, ignore_expires=True)
    except Exception as exc:  # noqa: BLE001 - keep CLI error readable
        raise DownloadError(
            "Cookie 文件读取失败。请导出为 Netscape/Mozilla cookies.txt 格式。"
        ) from exc
    session.cookies.update(jar)


def build_session(cookie_file: str | None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/pdf,*/*;q=0.8"
            ),
            "Accept-Encoding": "identity",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "close",
        }
    )
    load_cookie_file(session, cookie_file)
    return session


def explain_if_webvpn_login(response: requests.Response) -> None:
    location = response.headers.get("Location", "")
    host = urlparse(location).netloc.lower()
    if response.status_code in {301, 302, 303, 307, 308} and WEBVPN_LOGIN_HOST in host:
        raise DownloadError(
            "当前请求被 WebVPN 重定向到登录入口，脚本没有可用登录态。\n"
            f"Location: {location}\n"
            "处理办法：优先使用 Sangfor 客户端 VPN 后访问原站；"
            "或者用 --cookie-file 传入浏览器导出的 WebVPN cookies.txt。"
        )


def least_factor(value: int) -> int:
    if value == 0:
        return 0
    if value % 2 == 0:
        return 2
    if value % 3 == 0:
        return 3
    if value % 5 == 0:
        return 5
    factor = 7
    while factor * factor <= value:
        for offset in (0, 4, 6, 10, 12, 16, 22, 24):
            candidate = factor + offset
            if value % candidate == 0:
                return candidate
        factor += 30
    return value


def solve_key_cookie(html: str) -> str | None:
    if "document.cookie=\"KEY=\"" not in html and 'document.cookie="KEY="' not in html:
        return None

    p_match = re.search(r"var\s+p\s*=\s*(\d+)", html)
    s_match = re.search(r"var\s+s\s*=\s*(\d+)", html)
    salt_match = KEY_COOKIE_PATTERN.search(html)
    if not p_match or not s_match or not salt_match:
        return None

    p = int(p_match.group(1))
    s = int(s_match.group(1))
    salt = salt_match.group(1)

    script = JS_COMMENT_PATTERN.sub("", html)
    script = re.sub(r"\s+", " ", script)
    operations = re.findall(
        r"if\s*\(\s*\(s\s*>>\s*(\d+)\)\s*&\s*1\s*\)\s*"
        r"p\s*([+-])=\s*(\d+)\s*\*\s*(\d+)\s*;\s*"
        r"else\s*p\s*([+-])=\s*(\d+)\s*\*\s*(\d+)\s*;",
        script,
    )
    for bit, true_op, true_left, true_right, false_op, false_left, false_right in operations:
        if (s >> int(bit)) & 1:
            delta = int(true_left) * int(true_right)
            p = p + delta if true_op == "+" else p - delta
        else:
            delta = int(false_left) * int(false_right)
            p = p + delta if false_op == "+" else p - delta

    final_constant = re.search(r";\s*p\s*([+-])=\s*(\d+)\s*;\s*n\s*=\s*leastFactor", script)
    if final_constant:
        delta = int(final_constant.group(2))
        p = p + delta if final_constant.group(1) == "+" else p - delta

    n = least_factor(p)
    if not n:
        return None
    return f"{n}*{p // n}:{s}:{salt}:1"


def apply_key_challenge(session: requests.Session, response: requests.Response) -> bool:
    key = solve_key_cookie(response.text)
    if not key:
        return False
    session.cookies.set("KEY", key, path="/")
    return True


def resolve_doi(session: requests.Session, doi: str, timeout: int) -> str:
    doi_url = f"https://doi.org/{quote(doi, safe='/')}"
    response = session.get(doi_url, allow_redirects=True, timeout=timeout)
    explain_if_webvpn_login(response)
    response.raise_for_status()
    return response.url


def to_webvpn_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() == "pubs.geoscienceworld.org":
        return parsed._replace(
            scheme="http",
            netloc=f"{WEBVPN_GSW_HOST}:8118",
        ).geturl()
    return url


def fetch_page(session: requests.Session, url: str, timeout: int) -> requests.Response:
    response = session.get(url, allow_redirects=False, timeout=timeout)
    explain_if_webvpn_login(response)

    for _ in range(3):
        if not apply_key_challenge(session, response):
            break
        response = session.get(url, allow_redirects=False, timeout=timeout)
        explain_if_webvpn_login(response)

    if response.is_redirect:
        next_url = urljoin(url, response.headers.get("Location", ""))
        response = session.get(next_url, allow_redirects=True, timeout=timeout)
        explain_if_webvpn_login(response)
        for _ in range(3):
            if not apply_key_challenge(session, response):
                break
            response = session.get(response.url, allow_redirects=True, timeout=timeout)
            explain_if_webvpn_login(response)
    response.raise_for_status()
    return response


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def find_pdf_candidates(page_url: str, html: str) -> list[str]:
    candidates: list[str] = []
    text = unescape(html)
    for pattern in PDF_PATTERNS:
        candidates.extend(urljoin(page_url, match) for match in pattern.findall(text))

    parsed = urlparse(page_url)
    if "/doi/" in parsed.path:
        candidates.append(urljoin(page_url, parsed.path.replace("/doi/", "/doi/pdf/")))

    return unique(candidates)


def looks_like_pdf(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    if "application/pdf" in content_type:
        return True
    first = response.content[:5] if response.content else b""
    return first == b"%PDF-"


def try_download_pdf(
    session: requests.Session,
    candidates: list[str],
    output_path: Path,
    timeout: int,
) -> str:
    errors: list[str] = []
    for url in candidates:
        try:
            with session.get(url, allow_redirects=False, timeout=timeout, stream=True) as response:
                explain_if_webvpn_login(response)
                if apply_key_challenge(session, response):
                    response.close()
                    with session.get(url, allow_redirects=False, timeout=timeout, stream=True) as retry_response:
                        explain_if_webvpn_login(retry_response)
                        retry_response.raise_for_status()
                        content = retry_response.content
                        if content[:5] != b"%PDF-" and "application/pdf" not in retry_response.headers.get("Content-Type", "").lower():
                            errors.append(f"{url}: 不是 PDF")
                            continue
                        output_path.write_bytes(content)
                        return url
                if response.is_redirect:
                    redirected = urljoin(url, response.headers.get("Location", ""))
                    response.close()
                    with session.get(
                        redirected,
                        allow_redirects=True,
                        timeout=timeout,
                        stream=True,
                    ) as redirected_response:
                        explain_if_webvpn_login(redirected_response)
                        redirected_response.raise_for_status()
                        content = redirected_response.content
                        if content[:5] != b"%PDF-" and "application/pdf" not in redirected_response.headers.get("Content-Type", "").lower():
                            errors.append(f"{redirected}: 不是 PDF")
                            continue
                        output_path.write_bytes(content)
                        return redirected

                response.raise_for_status()
                content = response.content
                if content[:5] != b"%PDF-" and not looks_like_pdf(response):
                    errors.append(f"{url}: 不是 PDF")
                    continue
                output_path.write_bytes(content)
                return url
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")

    detail = "\n".join(errors[-5:]) if errors else "页面里没有找到 PDF 链接。"
    raise DownloadError(f"PDF 下载失败。\n{detail}")


def download_one(
    session: requests.Session,
    doi: str,
    output_dir: Path,
    timeout: int,
    use_webvpn: bool,
    target: PaperTarget | None = None,
):
    normalized = normalize_doi(doi)
    page_url = resolve_doi(session, normalized, timeout)
    if use_webvpn:
        page_url = to_webvpn_url(page_url)

    page = fetch_page(session, page_url, timeout)
    candidates = find_pdf_candidates(page.url, page.text)
    if use_webvpn:
        candidates = [to_webvpn_url(candidate) for candidate in candidates]

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (target_safe_filename(target) if target else safe_filename(normalized))
    pdf_url = try_download_pdf(session, candidates, output_path, timeout)
    return normalized, page.url, pdf_url, output_path


def download_page_url(
    session: requests.Session,
    target: PaperTarget,
    output_dir: Path,
    timeout: int,
    use_webvpn: bool,
):
    page_url = target.url or target.proxy_access_url
    if not page_url:
        raise DownloadError("缺少 GeoScienceWorld 页面 URL。")
    if use_webvpn:
        page_url = to_webvpn_url(page_url)

    page = fetch_page(session, page_url, timeout)
    candidates = find_pdf_candidates(page.url, page.text)
    if use_webvpn:
        candidates = [to_webvpn_url(candidate) for candidate in candidates]

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / target_safe_filename(target)
    pdf_url = try_download_pdf(session, candidates, output_path, timeout)
    return page.url, pdf_url, output_path


def download_gsw_pdf(
    target: PaperTarget,
    pdf_dir: Path,
    *,
    timeout: int = 30,
    cookie_file: str | None = None,
    use_webvpn: bool = False,
) -> DownloadResult:
    if not target.doi and not (target.url or target.proxy_access_url):
        return DownloadResult(status="skipped_no_doi_or_url", provider="gsw", title=target.title)
    normalized_doi = normalize_doi(target.doi) if target.doi else None
    output_path = pdf_dir / target_safe_filename(PaperTarget(title=target.title, doi=normalized_doi, url=target.url or target.proxy_access_url))
    if output_path.exists() and output_path.stat().st_size > 0:
        return DownloadResult(status="exists", provider="gsw", doi=target.doi, title=target.title, path=str(output_path), bytes=output_path.stat().st_size)
    try:
        session = build_session(cookie_file)
        if target.doi:
            doi, page_url, pdf_url, path = download_one(session, target.doi, pdf_dir, timeout, use_webvpn, target)
        else:
            page_url, pdf_url, path = download_page_url(session, target, pdf_dir, timeout, use_webvpn)
            doi = None
        return DownloadResult(
            status="downloaded",
            provider="gsw",
            doi=doi,
            title=target.title,
            page_url=page_url,
            pdf_url=pdf_url,
            final_url=pdf_url,
            path=str(path),
            bytes=path.stat().st_size if path.exists() else None,
        )
    except Exception as exc:
        return DownloadResult(status="failed", provider="gsw", doi=target.doi, title=target.title, reason=str(exc))


def read_dois(args: argparse.Namespace) -> list[str]:
    dois: list[str] = []
    if args.doi:
        dois.extend(args.doi)
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                dois.append(line)
    if not dois:
        raise DownloadError("请用 --doi 传入 DOI，或用 --file 传入 DOI 列表。")
    return dois


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download GeoScienceWorld PDFs by DOI through campus VPN."
    )
    parser.add_argument("--doi", action="append", help="DOI，可重复传入多次")
    parser.add_argument("--file", help="每行一个 DOI 的文本文件")
    parser.add_argument("--out", default="downloads", help="PDF 输出目录，默认 downloads")
    parser.add_argument("--cookie-file", help="Netscape/Mozilla cookies.txt，用于 WebVPN 登录态")
    parser.add_argument("--webvpn", action="store_true", help="把 GeoScienceWorld 原站 URL 改写为学校 WebVPN URL")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数")
    parser.add_argument("--sleep", type=float, default=1.0, help="批量下载时每篇之间暂停秒数")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        session = build_session(args.cookie_file)
        output_dir = Path(args.out)
        for index, doi in enumerate(read_dois(args), start=1):
            if index > 1:
                time.sleep(args.sleep)
            print(f"[{index}] {doi}")
            result = download_one(session, doi, output_dir, args.timeout, args.webvpn)
            print(f"  page: {result.page_url}")
            print(f"  pdf : {result.pdf_url}")
            print(f"  saved: {result.output_path}")
        return 0
    except DownloadError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"网络错误: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
