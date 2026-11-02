from __future__ import annotations

from .common import DownloadResult, PaperTarget


def publisher_not_implemented(provider: str, target: PaperTarget) -> DownloadResult:
    return DownloadResult(
        status="skipped_provider_not_implemented",
        provider=provider,
        doi=target.doi,
        title=target.title,
        reason=(
            f"{provider} is registered for discovery/harvest routing, but its "
            "formal PDF downloader is not implemented yet. Provide a sample DOI "
            "or landing page URL to add a site-specific downloader."
        ),
    )
