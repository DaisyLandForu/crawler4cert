from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .common import DownloadResult, PaperTarget, normalize_doi
from .browser_gsw import download_gsw_pdf_with_playwright
from .cookies import get_cookie_file
from .geoscienceworld import download_gsw_pdf
from .pdf_url import download_pdf_url
from .seg import download_seg_pdf
from .unimplemented import publisher_not_implemented


METADATA_PROVIDERS = ["arxiv", "openalex", "crossref"]
PUBLISHER_PROVIDERS = ["gsw", "seg", "sciencedirect", "cnki", "onepetro"]
ALL_PROVIDERS = METADATA_PROVIDERS + PUBLISHER_PROVIDERS


@dataclass
class DownloadOptions:
    timeout: float = 120.0
    retries: int = 3
    sleep_seconds: float = 2.0
    engine: str = "requests"
    playwright_enabled: bool = False
    playwright_profile_dir: str | None = None
    playwright_headless: bool = False
    playwright_executable_path: str | None = None
    publisher_cookie_dir: str | None = None
    gsw_cookie_file: str | None = None
    seg_cookie_file: str | None = None
    sciencedirect_cookie_file: str | None = None
    cnki_cookie_file: str | None = None
    onepetro_cookie_file: str | None = None
    gsw_webvpn: bool = False
    try_all_publishers: bool = False
    resolve_open_access: bool = True
    email: str | None = None


def download_target(
    target: PaperTarget,
    providers: list[str],
    pdf_dir: Path,
    options: DownloadOptions,
) -> DownloadResult:
    # Resolve public alternatives once, even when the original target has no
    # pdf_url. Do not repeat the same URL under three metadata provider names.
    public_result = None
    if any(provider in METADATA_PROVIDERS for provider in providers):
        from ..http import HttpClient
        from ..open_access import download_public_pdf

        public_result = download_public_pdf(
            target, pdf_dir, HttpClient(delay_seconds=options.sleep_seconds, timeout_seconds=options.timeout, retries=options.retries),
            email=options.email, lookup=options.resolve_open_access, retries=options.retries, sleep_seconds=options.sleep_seconds,
        )
        if public_result.status in {"downloaded", "exists"}:
            return public_result
    if options.try_all_publishers:
        ordered_providers = dedupe_preserve_order(providers)
    else:
        ordered_providers = two_phase_provider_order(target, providers)
    last_result: DownloadResult | None = public_result
    for provider in ordered_providers:
        provider = provider.strip().lower()
        if not provider:
            continue
        if provider in {"arxiv", "openalex", "crossref"}:
            if public_result is not None:
                continue
            result = download_pdf_url(
                target,
                pdf_dir,
                timeout=options.timeout,
                retries=options.retries,
                sleep_seconds=options.sleep_seconds,
                provider_name=provider,
            )
        elif provider == "gsw":
            if not (options.try_all_publishers or should_try_provider(target, provider)):
                continue
            cookie_file = provider_cookie_file(provider, options)
            if options.engine in {"requests", "auto"}:
                result = download_gsw_pdf(
                    target,
                    pdf_dir,
                    timeout=int(options.timeout),
                    cookie_file=cookie_file,
                    use_webvpn=options.gsw_webvpn,
                )
                if result.status in {"downloaded", "exists"} or options.engine != "auto":
                    pass
                elif options.playwright_enabled:
                    result = download_gsw_pdf_with_playwright(
                        target,
                        pdf_dir,
                        timeout=int(options.timeout),
                        profile_dir=provider_profile_dir(options, provider),
                        headless=options.playwright_headless,
                        executable_path=options.playwright_executable_path,
                        cookie_file=cookie_file,
                    )
            else:
                result = run_playwright_or_skip(
                    target,
                    pdf_dir,
                    options,
                    provider,
                    cookie_file,
                )
        elif provider == "seg":
            if not (options.try_all_publishers or should_try_provider(target, provider)):
                continue
            if options.engine == "playwright":
                result = publisher_not_implemented("seg_playwright", target)
            else:
                result = download_seg_pdf(
                    target,
                    pdf_dir,
                    timeout=int(options.timeout),
                    cookie_file=provider_cookie_file(provider, options),
                )
        elif provider in {"sciencedirect", "cnki", "onepetro"}:
            if not (options.try_all_publishers or should_try_provider(target, provider)):
                continue
            result = publisher_not_implemented(provider, target)
        elif provider == "playwright":
            result = DownloadResult(
                status="skipped_engine_not_implemented",
                provider=provider,
                doi=target.doi,
                title=target.title,
                reason="Playwright engine is configured but not implemented yet.",
            )
        else:
            last_result = DownloadResult(
                status="skipped_unknown_provider",
                provider=provider,
                doi=target.doi,
                title=target.title,
                reason=f"unknown provider: {provider}",
            )
            continue

        if result.status in {"downloaded", "exists"}:
            if public_result is not None:
                result.attempts = [*public_result.attempts, *result.attempts]
            return result
        if public_result is not None:
            public_result.attempts.append({"stage": provider, "status": result.status, "reason": result.reason})
        # A placeholder publisher must not hide the real public download error.
        if not result.status.startswith("skipped"):
            last_result = result

    return last_result or DownloadResult(
        status="skipped_no_provider",
        provider="none",
        doi=target.doi,
        title=target.title,
    )


def should_try_provider(target: PaperTarget, provider: str) -> bool:
    if provider == "crossref":
        return bool(target.pdf_url)
    if provider == "openalex":
        return bool(target.pdf_url)
    if provider == "arxiv":
        fields = " ".join(str(value or "").lower() for value in [target.source, target.url, target.pdf_url])
        return bool((target.metadata or {}).get("arxiv_id") or "arxiv.org" in fields)
    if provider == "gsw":
        return looks_like_gsw(target)
    if provider == "seg":
        return looks_like_seg(target)
    if provider == "sciencedirect":
        return looks_like_sciencedirect(target)
    if provider == "cnki":
        return looks_like_cnki(target)
    if provider == "onepetro":
        return looks_like_onepetro(target)
    return False


def looks_like_gsw(target: PaperTarget) -> bool:
    fields = target_fields(target)
    if any(
        marker in fields
        for marker in [
            "gsw",
            "geoscienceworld",
            "pubs.geoscienceworld.org",
            "geological society of america",
            "seismological society of america",
            "bulletin of the seismological society of america",
            "seismological research letters",
            "geosphere",
            "gsa bulletin",
            "lithosphere",
        ]
    ):
        return True
    doi = normalize_doi(target.doi) or ""
    return doi_has_configured_prefix(doi, "PAPER_GSW_DOI_PREFIXES", ["10.1130", "10.1785", "10.17491"])


def target_fields(target: PaperTarget) -> str:
    return " ".join(
        str(value or "").lower()
        for value in [
            target.publisher,
            target.url,
            target.proxy_access_url,
            target.source,
            target.pdf_url,
        ]
    )


def looks_like_seg(target: PaperTarget) -> bool:
    fields = target_fields(target)
    if "library.seg.org" in fields or "society of exploration geophysicists" in fields or "seg" in fields:
        return True
    doi = normalize_doi(target.doi) or ""
    return doi_has_configured_prefix(doi, "PAPER_SEG_DOI_PREFIXES", ["10.1190"])


def looks_like_sciencedirect(target: PaperTarget) -> bool:
    fields = target_fields(target)
    if "sciencedirect" in fields or "elsevier" in fields:
        return True
    doi = normalize_doi(target.doi) or ""
    return doi_has_configured_prefix(doi, "PAPER_SCIENCEDIRECT_DOI_PREFIXES", ["10.1016"])


def looks_like_cnki(target: PaperTarget) -> bool:
    fields = target_fields(target)
    return "cnki" in fields


def looks_like_onepetro(target: PaperTarget) -> bool:
    fields = target_fields(target)
    return "onepetro" in fields


def default_provider_order(target: PaperTarget, configured: list[str]) -> list[str]:
    ordered = evidence_provider_order(target)
    for provider in configured:
        if provider not in ordered:
            ordered.append(provider)
    return two_phase_provider_order(target, ordered)


def evidence_provider_order(target: PaperTarget) -> list[str]:
    ordered: list[str] = []
    if should_try_provider(target, "arxiv"):
        ordered.append("arxiv")
    if target_pdf_url_available(target):
        ordered.extend(["openalex", "crossref"])
    if should_try_provider(target, "gsw"):
        ordered.append("gsw")
    for publisher in ("seg", "sciencedirect", "cnki", "onepetro"):
        if should_try_provider(target, publisher):
            ordered.append(publisher)
    return dedupe_preserve_order(ordered)


def two_phase_provider_order(target: PaperTarget, providers: list[str]) -> list[str]:
    configured = dedupe_preserve_order([provider.strip().lower() for provider in providers if provider.strip()])
    direct = [provider for provider in configured if provider in METADATA_PROVIDERS and should_try_provider(target, provider)]
    publisher_evidence = [provider for provider in evidence_provider_order(target) if provider in PUBLISHER_PROVIDERS]
    configured_publishers = [provider for provider in configured if provider in PUBLISHER_PROVIDERS]
    unknown_or_manual = [provider for provider in configured if provider not in METADATA_PROVIDERS and provider not in PUBLISHER_PROVIDERS]
    return dedupe_preserve_order(direct + publisher_evidence + configured_publishers + unknown_or_manual)


def providers_for_source_groups(groups: str, explicit_providers: str | None = None) -> list[str]:
    if explicit_providers:
        return split_csv(explicit_providers)

    selected: list[str] = []
    for group in split_csv(groups or "all"):
        if group in {"all", "*"}:
            selected.extend(ALL_PROVIDERS)
        elif group in {"metadata", "open", "oa"}:
            selected.extend(METADATA_PROVIDERS)
        elif group in {"publisher", "institution", "institutional"}:
            selected.extend(PUBLISHER_PROVIDERS)
        else:
            selected.append(group)
    return dedupe_preserve_order(selected)


def split_csv(value: str | None) -> list[str]:
    return [item.strip().lower() for item in (value or "").split(",") if item.strip()]


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def target_pdf_url_available(target: PaperTarget) -> bool:
    if target.pdf_url:
        return True
    metadata = target.metadata if isinstance(target.metadata, dict) else {}
    if metadata.get("arxiv_id"):
        return True
    return bool(target.url and "arxiv.org/abs/" in target.url.lower())


def doi_has_configured_prefix(doi: str, env_key: str, defaults: list[str]) -> bool:
    if not doi:
        return False
    prefixes = split_csv(os.getenv(env_key, ",".join(defaults)))
    normalized_prefixes = [prefix.rstrip("/").lower() for prefix in prefixes]
    return any(doi.startswith(prefix + "/") or doi == prefix for prefix in normalized_prefixes)


def provider_cookie_file(provider: str, options: DownloadOptions) -> str | None:
    explicit = {
        "gsw": options.gsw_cookie_file,
        "seg": options.seg_cookie_file,
        "sciencedirect": options.sciencedirect_cookie_file,
        "cnki": options.cnki_cookie_file,
        "onepetro": options.onepetro_cookie_file,
    }.get(provider)
    return get_cookie_file(provider, explicit=explicit, cookie_dir=options.publisher_cookie_dir)


def provider_profile_dir(options: DownloadOptions, provider: str) -> str | None:
    if not options.playwright_profile_dir:
        return None
    return str(Path(options.playwright_profile_dir) / provider)


def run_playwright_or_skip(
    target: PaperTarget,
    pdf_dir: Path,
    options: DownloadOptions,
    provider: str,
    cookie_file: str | None,
) -> DownloadResult:
    if not options.playwright_enabled:
        return DownloadResult(
            status="skipped_playwright_disabled",
            provider=provider,
            doi=target.doi,
            title=target.title,
            reason="Set --enable-playwright or PAPER_PLAYWRIGHT_ENABLED=TRUE.",
        )
    if provider == "gsw":
        return download_gsw_pdf_with_playwright(
            target,
            pdf_dir,
            timeout=int(options.timeout),
            profile_dir=provider_profile_dir(options, provider),
            headless=options.playwright_headless,
            executable_path=options.playwright_executable_path,
            cookie_file=cookie_file,
        )
    return publisher_not_implemented(f"{provider}_playwright", target)
