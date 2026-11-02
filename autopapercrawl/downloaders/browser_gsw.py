from __future__ import annotations

import asyncio
import os
import random
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse, urlunparse

from .common import DownloadResult, PaperTarget, normalize_doi, safe_filename


DEFAULT_HOME_URL = "https://pubs.geoscienceworld.org"
UPPER_XPATH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER_XPATH = "abcdefghijklmnopqrstuvwxyz"
PDF_LINK_XPATH = (
    "//a["
    "@href "
    "and not(contains(@href, '/Citation/Download')) "
    "and not(contains(@href, '/DownloadFile/DownloadImage')) "
    f"and not(contains(translate(@href, '{UPPER_XPATH}', '{LOWER_XPATH}'), 'fig')) "
    "and ("
    f"contains(translate(@href, '{UPPER_XPATH}', '{LOWER_XPATH}'), '.pdf') "
    f"or contains(translate(@href, '{UPPER_XPATH}', '{LOWER_XPATH}'), 'article-pdf')"
    ")"
    "]"
)

PDF_REQUEST_LOCK: asyncio.Lock | None = None
PDF_PHASE_LOCK: asyncio.Lock | None = None
PDF_LAST_PHASE_AT = 0.0
PDF_COOLDOWN_UNTIL = 0.0


class BrowserConfig:
    def __init__(
        self,
        *,
        timeout: int,
        headless: bool,
        executable_path: str | None,
        profile_dir: str | None,
    ) -> None:
        self.timeout_ms = int(os.getenv("PAPER_GSW_PLAYWRIGHT_TIMEOUT_MS", str(max(5000, timeout * 1000))))
        self.pdf_timeout_ms = int(os.getenv("PAPER_GSW_PLAYWRIGHT_PDF_TIMEOUT_MS", str(max(30000, timeout * 1000))))
        self.min_pdf_bytes = int(os.getenv("PAPER_GSW_PLAYWRIGHT_MIN_PDF_BYTES", "50000"))
        self.cooldown_seconds = float(os.getenv("PAPER_GSW_PLAYWRIGHT_PDF_COOLDOWN_SECONDS", "20"))
        self.cooldown_429_seconds = float(os.getenv("PAPER_GSW_PLAYWRIGHT_429_COOLDOWN_SECONDS", "90"))
        self.retries = max(1, int(os.getenv("PAPER_GSW_PLAYWRIGHT_RETRIES", "2")))
        self.slow_mo_ms = int(os.getenv("PAPER_GSW_PLAYWRIGHT_SLOW_MO_MS", "120"))
        self.headless = headless
        self.executable_path = executable_path
        self.channel = os.getenv("PAPER_GSW_PLAYWRIGHT_CHANNEL", os.getenv("PAPER_PLAYWRIGHT_CHANNEL", "")).strip() or None
        self.home_url = os.getenv("PAPER_GSW_PLAYWRIGHT_HOME_URL", os.getenv("HOME_URL", DEFAULT_HOME_URL)).strip().rstrip("/")
        self.storage_state = resolve_storage_state(profile_dir)


def download_gsw_pdf_with_playwright(
    target: PaperTarget,
    pdf_dir: Path,
    *,
    timeout: int = 120,
    profile_dir: str | None = None,
    headless: bool = False,
    executable_path: str | None = None,
    cookie_file: str | None = None,
) -> DownloadResult:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return DownloadResult(
            status="skipped_missing_dependency",
            provider="gsw",
            doi=target.doi,
            title=target.title,
            reason="Playwright is not installed. Install it with `uv pip install playwright` and run `python -m playwright install chromium`.",
        )

    config = BrowserConfig(
        timeout=timeout,
        headless=headless,
        executable_path=executable_path,
        profile_dir=profile_dir,
    )
    output_path = pdf_dir / safe_filename(
        PaperTarget(title=target.title, doi=normalize_doi(target.doi), url=target.url or target.proxy_access_url)
    )
    if is_usable_pdf(output_path, config.min_pdf_bytes):
        return DownloadResult(
            status="exists",
            provider="gsw",
            doi=target.doi,
            title=target.title,
            path=str(output_path),
            bytes=output_path.stat().st_size,
        )

    try:
        result = asyncio.run(download_gsw_pdf_async(target, output_path, config))
    except Exception as exc:  # noqa: BLE001
        result = DownloadResult(status="failed_browser", provider="gsw", doi=target.doi, title=target.title, reason=str(exc))

    if result.status in {"downloaded", "exists"}:
        return result

    from .geoscienceworld import download_gsw_pdf

    fallback = download_gsw_pdf(target, pdf_dir, timeout=timeout, cookie_file=cookie_file)
    if fallback.status == "failed":
        fallback.reason = f"Playwright GSW site flow failed: {result.reason}; requests fallback failed: {fallback.reason}"
    fallback.provider = "gsw"
    return fallback


async def download_gsw_pdf_async(target: PaperTarget, output_path: Path, config: BrowserConfig) -> DownloadResult:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    doi = normalize_doi(target.doi)
    if not doi and not (target.url or target.proxy_access_url):
        return DownloadResult(status="skipped_no_doi_or_url", provider="gsw", title=target.title)

    init_locks()
    async with async_playwright() as playwright:
        launch_kwargs = {
            "headless": config.headless,
            "slow_mo": 0 if config.headless else config.slow_mo_ms,
        }
        if config.executable_path:
            launch_kwargs["executable_path"] = config.executable_path
        elif config.channel:
            launch_kwargs["channel"] = config.channel
        browser = await playwright.chromium.launch(**launch_kwargs)
        try:
            context_kwargs = {
                "accept_downloads": True,
                "viewport": {"width": 1440, "height": 1000},
            }
            if config.storage_state and Path(config.storage_state).exists():
                context_kwargs["storage_state"] = config.storage_state
            context = await browser.new_context(**context_kwargs)
            context.set_default_timeout(config.timeout_ms)
            try:
                try:
                    await warm_site_session(context, config)
                except Exception:
                    pass

                for attempt in range(1, config.retries + 1):
                    article_page = None
                    try:
                        article_page = await open_article_page(context, target, config)
                        await accept_cookies(article_page)
                        if doi and await download_pdf_phase(context, article_page, doi, output_path, config):
                            return downloaded_result(target, article_page.url, output_path)
                        if not doi and await download_from_article_links(article_page, output_path, config):
                            return downloaded_result(target, article_page.url, output_path)
                        await save_debug(article_page, doi or "url", f"attempt_{attempt}", output_path.parent)
                    except Exception as exc:  # noqa: BLE001
                        if article_page:
                            await save_debug(article_page, doi or "url", f"attempt_{attempt}_error", output_path.parent)
                        if attempt >= config.retries:
                            return DownloadResult(status="failed_browser", provider="gsw", doi=target.doi, title=target.title, reason=str(exc))
                    finally:
                        if article_page:
                            await close_page(article_page)
                    await human_wait(0.6, 1.2)

                if doi and await try_doi_resolver(context, doi, output_path, config):
                    return downloaded_result(target, None, output_path)

                return DownloadResult(status="failed_browser", provider="gsw", doi=target.doi, title=target.title, reason="GSW Playwright flow did not find a savable PDF.")
            finally:
                try:
                    if config.storage_state:
                        await context.storage_state(path=config.storage_state)
                except PlaywrightError:
                    pass
                await context.close()
        finally:
            await browser.close()


def resolve_storage_state(profile_dir: str | None) -> str | None:
    configured = os.getenv("PAPER_GSW_PLAYWRIGHT_STORAGE_STATE", os.getenv("STORAGE_STATE", "")).strip()
    if configured:
        return configured
    candidates = []
    if profile_dir:
        candidates.append(Path(profile_dir) / "auth_state.json")
    candidates.append(Path("config/browser-state/gsw.auth_state.json"))
    candidates.append(Path("auth_state.json"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    if profile_dir:
        path = Path(profile_dir)
        path.mkdir(parents=True, exist_ok=True)
        return str(path / "auth_state.json")
    return "config/browser-state/gsw.auth_state.json"


def init_locks() -> None:
    global PDF_REQUEST_LOCK, PDF_PHASE_LOCK
    if PDF_REQUEST_LOCK is None:
        PDF_REQUEST_LOCK = asyncio.Lock()
    if PDF_PHASE_LOCK is None:
        PDF_PHASE_LOCK = asyncio.Lock()


async def human_wait(a: float = 0.15, b: float = 0.55) -> None:
    await asyncio.sleep(random.uniform(a, b))


async def wait_pdf_cooldown(config: BrowserConfig) -> None:
    global PDF_LAST_PHASE_AT
    now = asyncio.get_running_loop().time()
    wait_for = max(PDF_LAST_PHASE_AT + config.cooldown_seconds - now, PDF_COOLDOWN_UNTIL - now, 0)
    if wait_for > 0:
        await asyncio.sleep(wait_for + random.uniform(0.2, 1.0))
    PDF_LAST_PHASE_AT = asyncio.get_running_loop().time()


def note_pdf_429(config: BrowserConfig) -> None:
    global PDF_COOLDOWN_UNTIL
    PDF_COOLDOWN_UNTIL = max(
        PDF_COOLDOWN_UNTIL,
        asyncio.get_running_loop().time() + config.cooldown_429_seconds,
    )


async def goto_with_retry(page, url: str, config: BrowserConfig, attempts: int = 2) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=config.timeout_ms)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await human_wait(0.8, 1.4)
    if last_error:
        raise last_error


async def accept_cookies(page) -> None:
    selectors = [
        page.get_by_role("button", name=re.compile(r"Accept All Cookies", re.I)),
        page.get_by_role("button", name=re.compile(r"Accept", re.I)),
        page.locator("button:has-text('Accept')"),
    ]
    for selector in selectors:
        try:
            await selector.first.click(timeout=1500)
            await human_wait(0.05, 0.2)
        except Exception:
            pass


async def warm_site_session(context, config: BrowserConfig) -> None:
    page = await context.new_page()
    try:
        await goto_with_retry(page, config.home_url, config, attempts=2)
        await accept_cookies(page)
    finally:
        await close_page(page)


async def open_article_page(context, target: PaperTarget, config: BrowserConfig):
    doi = normalize_doi(target.doi)
    if target.url or target.proxy_access_url:
        page = await context.new_page()
        await goto_with_retry(page, proxied_page_url(target.url or target.proxy_access_url or "", config), config, attempts=2)
        return page
    if doi:
        article_url = await resolve_doi_with_ajax(context, doi, config)
        if article_url:
            page = await context.new_page()
            await goto_with_retry(page, article_url, config, attempts=2)
            return page
        return await open_article_by_advanced_search(context, doi, config)
    raise RuntimeError("missing DOI or URL")


async def open_article_by_advanced_search(context, doi: str, config: BrowserConfig):
    page = await context.new_page()
    await goto_with_retry(page, config.home_url, config)
    await accept_cookies(page)
    await goto_with_retry(page, config.home_url.rstrip("/") + "/advanced-search", config)
    await accept_cookies(page)
    doi_box = page.get_by_role("textbox", name=re.compile(r"DOI", re.I))
    await doi_box.wait_for(timeout=config.timeout_ms)
    await doi_box.click()
    await doi_box.fill("")
    await human_wait(0.2, 0.4)
    await doi_box.fill(doi)
    await human_wait(0.3, 0.6)
    try:
        await page.locator("#btnDoiSearch").click(timeout=8000)
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        article_url = await doi_search_ajax(page, doi, config)
        if article_url:
            await goto_with_retry(page, article_url, config, attempts=2)
    return page


async def doi_search_ajax(page, doi: str, config: BrowserConfig) -> str | None:
    await human_wait(0.5, 0.8)
    try:
        data = await page.evaluate(
            """
            async (doi) => {
              const response = await fetch('/Solr/DoiSearch', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                body: new URLSearchParams({doi})
              });
              return await response.text();
            }
            """,
            doi,
        )
    except Exception:
        return None
    return parse_doi_search_result(data, config)


async def resolve_doi_with_ajax(context, doi: str, config: BrowserConfig) -> str | None:
    try:
        response = await context.request.post(
            urljoin(config.home_url + "/", "Solr/DoiSearch"),
            form={"doi": doi},
            timeout=config.timeout_ms,
        )
        if not response.ok:
            return None
        data = await response.text()
    except Exception:
        return None
    return parse_doi_search_result(data, config)


def parse_doi_search_result(data: str | None, config: BrowserConfig) -> str | None:
    if not data or data.strip().lower() == "false":
        return None
    match = re.search(r"href\s*=\s*'([^']+)'", data)
    value = match.group(1) if match else data.strip()
    if not value or value.lower() == "false" or "<html" in value.lower() or not re.search(r"/article/", value, re.I):
        return None
    return urljoin(config.home_url + "/", value)


async def download_pdf_phase(context, article_page, doi: str, save_path: Path, config: BrowserConfig) -> bool:
    assert PDF_PHASE_LOCK is not None
    async with PDF_PHASE_LOCK:
        await wait_pdf_cooldown(config)
        if await download_from_article_links(article_page, save_path, config):
            return True
        pdf_page = None
        try:
            pdf_page = await open_pdf_page(context, article_page, save_path, config)
            if is_usable_pdf(save_path, config.min_pdf_bytes):
                return True
            if await download_pdf_from_page(pdf_page, save_path, config):
                return True
            return False
        finally:
            if pdf_page:
                await close_page(pdf_page)


async def download_from_article_links(page, save_path: Path, config: BrowserConfig) -> bool:
    try:
        await page.locator(f"xpath={PDF_LINK_XPATH}").first.wait_for(timeout=12000)
    except Exception:
        pass
    for link in await candidate_pdf_links(page):
        absolute = urljoin(page.url, link)
        if await fetch_pdf_url(page.context, absolute, save_path, config, referer=page.url):
            return True
    return False


async def candidate_pdf_links(page) -> list[str]:
    js = """
    (xpath) => {
      const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      const anchors = [];
      for (let i = 0; i < result.snapshotLength; i += 1) anchors.push(result.snapshotItem(i));
      return anchors.map(a => ({href: a.href, text: (a.innerText || a.getAttribute('aria-label') || a.title || '').trim()}));
    }
    """
    try:
        anchors = await page.evaluate(js, PDF_LINK_XPATH)
    except Exception:
        anchors = []
    links: list[str] = []
    seen: set[str] = set()
    skip_pattern = re.compile(r"fig|DownloadImage|powerpoint|png|jpg|bmp|gif", re.I)
    keep_pattern = re.compile(r"\.pdf|article-pdf|/pdf/", re.I)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if href and keep_pattern.search(href) and not skip_pattern.search(href) and href not in seen:
            links.append(href)
            seen.add(href)
    return links


async def open_pdf_page(context, page, save_path: Path, config: BrowserConfig):
    locators = [
        page.locator(f"xpath={PDF_LINK_XPATH}").first,
        page.get_by_role("link", name=re.compile(r"open.*pdf|pdf", re.I)).first,
        page.locator("a[aria-label*='PDF' i]").first,
        page.locator("a[href*='.pdf' i]").first,
        page.locator("a[href*='article-pdf' i]").first,
        page.locator("a:has-text('PDF')").first,
    ]
    for locator in locators:
        try:
            await locator.wait_for(timeout=5000)
            try:
                async with context.expect_event("response", predicate=lambda response: is_pdfish_url(response.url), timeout=10000) as response_info:
                    try:
                        async with page.expect_popup(timeout=6000) as popup_info:
                            await locator.click()
                        pdf_page = await popup_info.value
                    except Exception:
                        await locator.click()
                        pdf_page = context.pages[-1] if context.pages else await context.new_page()
                if await save_response_if_pdf(await response_info.value, save_path, config):
                    return pdf_page
            except Exception:
                try:
                    async with page.expect_popup(timeout=6000) as popup_info:
                        await locator.click()
                    pdf_page = await popup_info.value
                except Exception:
                    await locator.click()
                    pdf_page = context.pages[-1] if context.pages else await context.new_page()
            await pdf_page.wait_for_load_state("domcontentloaded", timeout=config.timeout_ms)
            await human_wait()
            return pdf_page
        except Exception:
            continue
    raise RuntimeError("no clickable PDF link found")


async def download_pdf_from_page(page, save_path: Path, config: BrowserConfig) -> bool:
    if await save_current_pdf_response(page, save_path, config):
        return True
    return await trigger_download(page, save_path, config)


async def trigger_download(page, save_path: Path, config: BrowserConfig) -> bool:
    buttons = [
        page.get_by_role("button", name=re.compile(r"download", re.I)).first,
        page.get_by_role("link", name=re.compile(r"download", re.I)).first,
        page.locator("button:has-text('Download')").first,
        page.locator("a:has-text('Download')").first,
    ]
    for button in buttons:
        try:
            await button.wait_for(timeout=3000)
            async with page.expect_download(timeout=config.timeout_ms) as download_info:
                await button.click()
            download = await download_info.value
            await download.save_as(save_path)
            return is_usable_pdf(save_path, config.min_pdf_bytes)
        except Exception:
            continue
    return False


async def try_doi_resolver(context, doi: str, save_path: Path, config: BrowserConfig) -> bool:
    page = await context.new_page()
    try:
        await goto_with_retry(page, f"https://doi.org/{quote(doi, safe='/')}", config, attempts=1)
        if "chooser.crossref.org" in page.url:
            for resource_url in extract_crossref_resource_urls(await page.content())[:3]:
                await goto_with_retry(page, proxied_page_url(resource_url, config), config, attempts=2)
                break
        proxied = proxied_page_url(page.url, config)
        if proxied != page.url:
            await goto_with_retry(page, proxied, config, attempts=2)
        if not is_gsw_page_url(page.url, config):
            return False
        await accept_cookies(page)
        return await download_from_article_links(page, save_path, config)
    finally:
        await close_page(page)


async def fetch_pdf_url(context, url: str, save_path: Path, config: BrowserConfig, referer: str | None = None, depth: int = 0) -> bool:
    if depth > 2:
        return False
    for candidate_url in proxied_url_variants(url, config):
        headers = {"Referer": referer} if referer else None
        for attempt in range(1, 4):
            try:
                assert PDF_REQUEST_LOCK is not None
                async with PDF_REQUEST_LOCK:
                    response = await context.request.get(candidate_url, headers=headers, timeout=config.pdf_timeout_ms)
                content_type = (response.headers.get("content-type") or "").lower()
                body = await response.body()
            except Exception:
                break
            if response.status in {403, 429} and is_pdfish_url(candidate_url):
                note_pdf_429(config)
                await asyncio.sleep(10 * attempt)
                continue
            if response.ok and ("pdf" in content_type or is_pdf_bytes(body)):
                if save_pdf_bytes(body, save_path, config.min_pdf_bytes):
                    return True
            if response.ok and "html" in content_type:
                html = body.decode("utf-8", errors="ignore")
                for nested_url in extract_pdf_urls_from_html(html)[:5]:
                    absolute = urljoin(candidate_url, nested_url)
                    if absolute != candidate_url and await fetch_pdf_url(context, absolute, save_path, config, referer=candidate_url, depth=depth + 1):
                        return True
            break
    return False


async def save_response_if_pdf(response, save_path: Path, config: BrowserConfig) -> bool:
    try:
        content_type = (response.headers.get("content-type") or "").lower()
        body = await response.body()
    except Exception:
        return False
    if response.status in {403, 429}:
        note_pdf_429(config)
        return False
    if response.ok and ("pdf" in content_type or is_pdf_bytes(body)):
        return save_pdf_bytes(body, save_path, config.min_pdf_bytes)
    return False


async def save_current_pdf_response(page, save_path: Path, config: BrowserConfig) -> bool:
    if not is_pdfish_url(page.url):
        return False
    try:
        assert PDF_REQUEST_LOCK is not None
        async with PDF_REQUEST_LOCK:
            response = await page.context.request.get(page.url, headers={"Referer": page.url}, timeout=config.pdf_timeout_ms)
        body = await response.body()
    except Exception:
        return False
    if response.status in {403, 429}:
        note_pdf_429(config)
        return False
    if response.ok and save_pdf_bytes(body, save_path, config.min_pdf_bytes):
        return True
    content_type = (response.headers.get("content-type") or "").lower()
    if response.ok and "html" in content_type:
        html = body.decode("utf-8", errors="ignore")
        for nested_url in extract_pdf_urls_from_html(html)[:5]:
            if await fetch_pdf_url(page.context, urljoin(page.url, nested_url), save_path, config, referer=page.url, depth=1):
                return True
    return False


def proxied_url_variants(url: str, config: BrowserConfig) -> list[str]:
    variants = [url]
    home = urlparse(config.home_url)
    target = urlparse(url)
    if home.netloc and target.hostname == "pubs.geoscienceworld.org":
        proxied = urlunparse((home.scheme, home.netloc, target.path, target.params, target.query, target.fragment))
        if proxied not in variants:
            variants.insert(0, proxied)
    return variants


def proxied_page_url(url: str, config: BrowserConfig) -> str:
    home = urlparse(config.home_url)
    target = urlparse(url)
    if home.netloc and target.hostname == "pubs.geoscienceworld.org":
        return urlunparse((home.scheme, home.netloc, target.path, target.params, target.query, target.fragment))
    return url


def is_gsw_page_url(url: str, config: BrowserConfig) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    home_host = (urlparse(config.home_url).hostname or "").lower()
    return "geoscienceworld.org" in host or bool(home_host and host == home_host)


def is_pdfish_url(url: str) -> bool:
    return bool(re.search(r"\.pdf\b|article-pdf|/pdf/|/pdf$", url, re.I))


def is_pdf_bytes(content: bytes) -> bool:
    return content[:5] == b"%PDF-" or b"%PDF-" in content[:1024]


def is_usable_pdf(path: Path, min_pdf_bytes: int) -> bool:
    if not path.exists() or path.stat().st_size < min_pdf_bytes:
        return False
    try:
        with path.open("rb") as handle:
            return is_pdf_bytes(handle.read(1024))
    except OSError:
        return False


def save_pdf_bytes(content: bytes, save_path: Path, min_pdf_bytes: int) -> bool:
    if not is_pdf_bytes(content) or len(content) < min_pdf_bytes:
        return False
    if is_usable_pdf(save_path, min_pdf_bytes) and save_path.stat().st_size > len(content):
        return True
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(content)
    return True


def extract_pdf_urls_from_html(html: str) -> list[str]:
    urls: list[str] = []
    patterns = [
        r"""(?:href|src)=["']([^"']+(?:\.pdf|article-pdf)[^"']*)["']""",
        r"""(?:window\.location|location\.href)\s*=\s*["']([^"']+(?:\.pdf|article-pdf)[^"']*)["']""",
        r"""https?://[^"' <>()]+(?:\.pdf|article-pdf)[^"' <>()]*""",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, html, flags=re.I):
            value = match if isinstance(match, str) else match[0]
            value = value.replace("&amp;", "&").strip()
            if value and value not in urls:
                urls.append(value)
    return urls


def extract_crossref_resource_urls(html: str) -> list[str]:
    urls: list[str] = []
    patterns = [
        r"""['"]primary-resource['"]\s*:\s*['"]([^'"]+)['"]""",
        r"""['"]url['"]\s*:\s*['"]([^'"]*geoscienceworld\.org[^'"]+)['"]""",
        r"""href=["']([^"']*geoscienceworld\.org[^"']+)["']""",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, html, flags=re.I):
            url = match.replace("\\/", "/").replace("&amp;", "&").strip()
            if url and url not in urls:
                urls.append(url)
    return urls


async def save_debug(page, doi: str, label: str, pdf_dir: Path) -> None:
    debug_dir = pdf_dir / "_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r'[\\/:*?"<>|]', "_", doi) or "gsw"
    try:
        await page.screenshot(path=debug_dir / f"{stem}_{label}.png", full_page=True, timeout=8000)
    except Exception:
        pass
    try:
        html = await page.content()
        (debug_dir / f"{stem}_{label}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass


async def close_page(page) -> None:
    try:
        await page.close()
    except Exception:
        pass


def downloaded_result(target: PaperTarget, page_url: str | None, output_path: Path) -> DownloadResult:
    return DownloadResult(
        status="downloaded",
        provider="gsw",
        doi=normalize_doi(target.doi),
        title=target.title,
        page_url=page_url,
        final_url=page_url,
        path=str(output_path),
        bytes=output_path.stat().st_size if output_path.exists() else None,
    )
