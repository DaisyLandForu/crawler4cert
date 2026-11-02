from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

from .downloaders.common import PaperTarget, merge_target, normalize_doi, normalize_title
from .http import HttpClient
from .models import Paper
from .search import deduplicate, enrich_with_unpaywall, resolve_seeds, search_all


ARXIV_ID_RE = re.compile(
    r"(?:arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
    re.I,
)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)


@dataclass
class MetadataSearchConfig:
    max_results: int = 3
    sources: list[str] | None = None
    email: str | None = None
    year: int | None = None
    page_size: int = 10
    source_workers: int = 3
    doi_workers: int = 4
    delay_seconds: float = 2.0
    timeout_seconds: float = 120.0
    retries: int = 3
    exact_title: str | None = None
    authors: list[str] | None = None


def build_metadata_client(config: MetadataSearchConfig) -> HttpClient:
    return HttpClient(
        delay_seconds=config.delay_seconds,
        timeout_seconds=config.timeout_seconds,
        retries=config.retries,
    )


def resolve_identifier_targets(
    *,
    dois: list[str] | None = None,
    arxiv_ids: list[str] | None = None,
    config: MetadataSearchConfig,
) -> list[PaperTarget]:
    dois = dois or []
    arxiv_ids = arxiv_ids or []
    client = build_metadata_client(config)
    papers = resolve_seeds(
        dois=dois,
        arxiv_ids=[clean_arxiv_id(item) for item in arxiv_ids],
        client=client,
        email=config.email,
        doi_workers=max(1, config.doi_workers),
    )
    papers = enrich_with_unpaywall(papers, client, config.email)
    targets = [paper_to_target(paper) for paper in papers]

    resolved_dois = {normalize_doi(target.doi) for target in targets if target.doi}
    for doi in dois:
        normalized = normalize_doi(doi)
        if normalized and normalized not in resolved_dois:
            targets.append(PaperTarget(doi=normalized))
    return dedupe_targets(targets)


def resolve_url_targets(urls: list[str]) -> list[PaperTarget]:
    targets: list[PaperTarget] = []
    for url in urls:
        clean = url.strip()
        if not clean:
            continue
        doi = extract_doi_from_text(clean)
        arxiv_id = extract_arxiv_id_from_url(clean)
        pdf_url = clean if looks_like_direct_pdf(clean) or arxiv_id else None
        metadata = {"input_type": "url"}
        if arxiv_id:
            metadata["arxiv_id"] = arxiv_id
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        targets.append(
            PaperTarget(
                doi=doi,
                pdf_url=pdf_url,
                url=clean,
                source="url",
                publisher=infer_publisher_from_url(clean),
                metadata=metadata,
            )
        )
    return dedupe_targets(targets)


def search_metadata_targets(queries: list[str], *, config: MetadataSearchConfig) -> list[PaperTarget]:
    client = build_metadata_client(config)
    sources = config.sources or ["openalex", "crossref", "arxiv"]
    papers: list[Paper] = []
    for query in queries:
        papers.extend(
            search_all(
                query,
                max_results=max(1, config.max_results),
                start_year=config.year,
                end_year=config.year,
                client=client,
                email=config.email,
                sources=sources,
                page_size=max(1, config.page_size),
                source_workers=max(1, config.source_workers),
            )
        )
    papers = rank_metadata_matches(deduplicate(papers), config)[: max(1, config.max_results)]
    papers = enrich_with_unpaywall(papers, client, config.email)
    return dedupe_targets([paper_to_target(paper) for paper in papers])


def rank_metadata_matches(papers: list[Paper], config: MetadataSearchConfig) -> list[Paper]:
    if not (config.exact_title or config.year or config.authors):
        return papers
    return sorted(papers, key=lambda paper: metadata_match_score(paper, config), reverse=True)


def metadata_match_score(paper: Paper, config: MetadataSearchConfig) -> float:
    score = float(paper.citations or 0) / 10000.0
    if config.exact_title:
        score += 10.0 * title_similarity(config.exact_title, paper.title)
    if config.year and paper.year:
        score += 2.0 if paper.year == config.year else max(0.0, 1.0 - abs(paper.year - config.year) * 0.25)
    if config.authors and paper.authors:
        wanted = [normalize_title(author) for author in config.authors]
        got = [normalize_title(author) for author in paper.authors]
        matches = sum(1 for author in wanted if any(author in candidate or candidate in author for candidate in got))
        score += 2.0 * matches
    return score


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def paper_to_target(paper: Paper) -> PaperTarget:
    metadata = dict(paper.metadata or {})
    if paper.authors:
        metadata["authors"] = paper.authors
    if paper.year:
        metadata["year"] = paper.year
    if paper.abstract:
        metadata["abstract"] = paper.abstract
    return PaperTarget(
        title=paper.title,
        doi=normalize_doi(paper.doi),
        pdf_url=paper.pdf_url,
        url=paper.url,
        source=paper.source,
        publisher=metadata.get("publisher"),
        metadata=metadata,
    )


def load_curated_targets(curated_dir: Path, project_dir: Path) -> list[PaperTarget]:
    selected = load_papers_json(curated_dir / "selected_papers.json")
    raw_index = index_targets(load_project_raw_targets(project_dir))
    enriched = []
    for target in selected:
        raw = raw_index.get(target.key)
        enriched.append(merge_target(target, raw) if raw else target)
    return dedupe_targets(enriched)


def load_project_raw_targets(project_dir: Path) -> list[PaperTarget]:
    targets: list[PaperTarget] = []
    for run_dir in sorted((project_dir / "raw").glob("*")):
        if run_dir.is_dir():
            targets.extend(load_run_targets(run_dir))
    return dedupe_targets(targets)


def load_run_targets(run_dir: Path) -> list[PaperTarget]:
    targets = load_papers_json(run_dir / "papers.json")
    targets.extend(load_ranked_json(run_dir / "publisher_ranked.json"))
    targets.extend(load_ranked_json(run_dir / "native_ranked.json"))
    return dedupe_targets(targets)


def load_papers_json(path: Path) -> list[PaperTarget]:
    items = load_json_list(path)
    targets = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = dict(item.get("metadata") or {})
        for field in ("year", "abstract", "authors", "directions", "pdf_urls", "arxiv_id"):
            if item.get(field) is not None:
                metadata[field] = item[field]
        targets.append(
            PaperTarget(
                title=item.get("title"),
                doi=normalize_doi(item.get("doi")),
                pdf_url=item.get("pdf_url"),
                url=item.get("url"),
                source=item.get("source"),
                publisher=item.get("publisher") or metadata.get("publisher"),
                proxy_access_url=item.get("proxy_access_url"),
                metadata=metadata,
            )
        )
    return targets


def load_ranked_json(path: Path) -> list[PaperTarget]:
    items = load_json_list(path)
    targets = []
    for item in items:
        if not isinstance(item, dict):
            continue
        targets.append(
            PaperTarget(
                title=item.get("title"),
                doi=normalize_doi(item.get("doi")),
                url=item.get("url"),
                source=item.get("source"),
                publisher=item.get("publisher"),
                proxy_access_url=item.get("proxy_access_url"),
                metadata={"ranked_source": str(path)},
            )
        )
    return targets


def dedupe_targets(targets: list[PaperTarget]) -> list[PaperTarget]:
    seen: dict[str, PaperTarget] = {}
    for target in targets:
        if not (target.doi or target.url or target.title):
            continue
        existing = seen.get(target.key)
        seen[target.key] = merge_target(existing, target) if existing else target
    return list(seen.values())


def index_targets(targets: list[PaperTarget]) -> dict[str, PaperTarget]:
    return {target.key: target for target in targets}


def load_json_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def target_as_dict(target: PaperTarget) -> dict:
    return {
        "title": target.title,
        "doi": target.doi,
        "pdf_url": target.pdf_url,
        "url": target.url,
        "source": target.source,
        "publisher": target.publisher,
        "proxy_access_url": target.proxy_access_url,
        "metadata": target.metadata,
    }


def extract_doi_from_text(value: str) -> str | None:
    match = DOI_RE.search(value)
    if not match:
        return None
    return normalize_doi(match.group(0).rstrip(".,);]"))


def extract_arxiv_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if "arxiv.org" not in parsed.netloc.lower():
        return None
    path = parsed.path.strip("/")
    for prefix in ("abs/", "pdf/"):
        if path.startswith(prefix):
            return clean_arxiv_id(path[len(prefix):].removesuffix(".pdf"))
    return None


def clean_arxiv_id(value: str) -> str:
    clean = value.strip()
    clean = re.sub(r"^arxiv:", "", clean, flags=re.I)
    clean = clean.removesuffix(".pdf")
    match = ARXIV_ID_RE.search(clean)
    return match.group(1) if match else clean


def looks_like_direct_pdf(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.path.lower().endswith(".pdf") or "/pdf/" in parsed.path.lower()


def infer_publisher_from_url(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if "geoscienceworld" in host or "pubs-geoscienceworld" in host:
        return "gsw"
    if "sciencedirect" in host or "www-sciencedirect-com" in host:
        return "sciencedirect"
    if "cnki" in host or "www-cnki-net" in host:
        return "cnki"
    if "onepetro" in host or "onepetro-org" in host:
        return "onepetro"
    if "science.org" in host or "sciencemag.org" in host:
        return "science"
    if "seg.org" in host or "library-seg" in host:
        return "seg"
    if "arxiv.org" in host:
        return "arxiv"
    return None
