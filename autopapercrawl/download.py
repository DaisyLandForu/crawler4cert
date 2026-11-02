from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from .http import HttpClient
from .models import Paper, normalize_doi


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def download_open_access_pdf(
    paper: Paper,
    output_dir: Path,
    client: HttpClient,
    *,
    dry_run: bool = False,
) -> dict:
    from .open_access import download_public_pdf
    from .targets import paper_to_target
    from .downloaders.common import safe_filename

    target = paper_to_target(paper)
    path = output_dir / safe_filename(target)
    if dry_run:
        return {"status": "dry_run", "path": str(path), "reason": None}
    return download_public_pdf(target, output_dir, client, retries=client.retries, sleep_seconds=client.delay_seconds).as_dict()


def is_allowed_pdf_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith(
        (
            "arxiv.org",
            "biorxiv.org",
            "medrxiv.org",
            "ncbi.nlm.nih.gov",
            "europepmc.org",
            "frontiersin.org",
            "plos.org",
            "mdpi.com",
            "doaj.org",
            "osf.io",
            "zenodo.org",
            "figshare.com",
            "openreview.net",
        )
    )


def has_open_access_metadata(paper: Paper) -> bool:
    return (paper.oa_status or "").lower() in {
        "gold",
        "green",
        "hybrid",
        "bronze",
        "diamond",
        "license_present",
        "open",
    }


def looks_like_pdf(body: bytes) -> bool:
    return body[:5] == b"%PDF-" or b"%PDF-" in body[:1024]


def paper_filename(paper: Paper) -> str:
    if paper.doi:
        base = "doi_" + normalize_doi(paper.doi)
    else:
        digest = hashlib.sha1(paper.title.encode("utf-8")).hexdigest()[:10]
        year = str(paper.year or "unknown")
        base = f"{year}_{paper.title[:80]}_{digest}"
    return SAFE_NAME_RE.sub("_", base).strip("._") + ".pdf"
