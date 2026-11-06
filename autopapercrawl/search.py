from __future__ import annotations

import html
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from .http import HttpClient, urlencode
from .models import Paper, normalize_doi
from .venues import canonical, venue_matches, is_conference


ABSTRACT_TAG_RE = re.compile(r"<[^>]+>")
DEFAULT_PAGE_SIZE = 10
SOURCE_IDS: dict[str, str] = {}
CROSSREF_ISSNS: dict[str, str | None] = {}


def search_all(
    query: str,
    *,
    max_results: int,
    start_year: int | None,
    end_year: int | None,
    client: HttpClient,
    email: str | None = None,
    sources: list[str] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    source_workers: int = 3,
    sort_by: str = "newest",
    diagnostics: list[dict] | None = None,
    truncate_results: bool = True,
    venue: str | None = None,
    title_filter: str | None = None,
    registry=None,
    from_date=None,
) -> list[Paper]:
    if max_results < 1 or page_size < 1:
        raise ValueError("max_results and page_size must be positive")
    if start_year and end_year and start_year > end_year:
        raise ValueError("start_year must not exceed end_year")
    selected = sources or ["openalex", "crossref", "arxiv"]
    per_source = max(1, max_results)
    papers: list[Paper] = []

    from .adapters import ADAPTERS
    from .adapters.base import UnsupportedCapability
    from .registry import Registry
    registry = registry or Registry()
    filters = {"max_results": per_source, "start_year": start_year, "end_year": end_year, "email": email,
               "page_size": page_size, "sort_by": sort_by, "venue": venue, "title_filter": title_filter,
               "venue_record": registry.venue(venue) if venue else None, "from_date": from_date}
    for source in selected:
        if source not in ADAPTERS:
            raise ValueError(f"Unknown metadata source: {source}")

    def call(source):
        source_client = clone_client(client)
        diagnostic = {"source": source, "query": query, "venue": venue, "count": 0}
        try:
            result = ADAPTERS[source](source_client, registry).search(query, filters)
            diagnostic.update(status="ok" if result else "empty", count=len(result), error=None)
        except Exception as exc:
            result = []
            diagnostic.update(status="unsupported" if isinstance(exc, UnsupportedCapability) else "request_failed", error=str(exc))
            print(f"warning: {source} search failed: {exc}", file=sys.stderr)
        diagnostic.update(requests=getattr(source_client, "request_count", 0), successful_requests=getattr(source_client, "success_count", 0),
                          failed_requests=getattr(source_client, "failure_count", 0))
        if diagnostics is not None:
            diagnostics.append(diagnostic)
        return result
    if source_workers <= 1 or len(selected) <= 1:
        for source in selected:
            papers.extend(call(source))
    else:
        with ThreadPoolExecutor(max_workers=min(source_workers, len(selected))) as executor:
            for result in executor.map(call, selected):
                papers.extend(result)

    if not truncate_results:
        return sort_papers(papers, sort_by)
    return deduplicate(papers, sort_by=sort_by)[:max_results]


def search_openalex(
    query: str,
    max_results: int,
    start_year: int | None,
    end_year: int | None,
    client: HttpClient,
    email: str | None,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "newest",
    venue: str | None = None,
    title_filter: str | None = None,
    from_date: str | None = None,
) -> list[Paper]:
    from .adapters.openalex import _search
    return _search(**locals_for_search(locals()))


def search_crossref(
    query: str,
    max_results: int,
    start_year: int | None,
    end_year: int | None,
    client: HttpClient,
    email: str | None,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "newest",
    venue: str | None = None,
    title_filter: str | None = None,
    from_date: str | None = None,
) -> list[Paper]:
    from .adapters.crossref import _search
    return _search(**locals_for_search(locals()))


def search_arxiv(
    query: str,
    max_results: int,
    start_year: int | None,
    end_year: int | None,
    client: HttpClient,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "newest",
    title_filter: str | None = None,
    from_date: str | None = None,
) -> list[Paper]:
    # Keep explicit field/Boolean queries intact. Ordinary topic words must each
    # receive a field prefix; all:foo bar does not reliably mean all:foo AND all:bar.
    from .adapters.arxiv import _search
    return _search(**locals_for_search(locals()))


def in_year_range(year: int | None, start_year: int | None, end_year: int | None) -> bool:
    if year is None:
        return not (start_year or end_year)
    return (not start_year or year >= start_year) and (not end_year or year <= end_year)


def publication_cutoff(end_year: int) -> str:
    return min(date(end_year, 12, 31), date.today()).isoformat()


def resolve_seeds(
    *,
    dois: list[str],
    arxiv_ids: list[str],
    client: HttpClient,
    email: str | None = None,
    doi_workers: int = 4,
) -> list[Paper]:
    papers: list[Paper] = []
    clean_dois = [normalize_doi(doi) for doi in dois if doi]
    if doi_workers <= 1 or len(clean_dois) <= 1:
        for doi in clean_dois:
            paper = resolve_doi(doi, clone_client(client), email)
            if paper:
                papers.append(paper)
    else:
        with ThreadPoolExecutor(max_workers=min(doi_workers, len(clean_dois))) as executor:
            futures = {
                executor.submit(resolve_doi, doi, clone_client(client), email): doi
                for doi in clean_dois
            }
            for future in as_completed(futures):
                doi = futures[future]
                try:
                    paper = future.result()
                except Exception as exc:
                    print(f"warning: DOI resolve failed for {doi}: {exc}", file=sys.stderr)
                    continue
                if paper:
                    papers.append(paper)
    if arxiv_ids:
        papers.extend(resolve_arxiv_ids(arxiv_ids, client))
    return deduplicate(papers)


def resolve_doi(doi: str, client: HttpClient, email: str | None) -> Paper | None:
    return resolve_openalex_doi(doi, client, email) or resolve_crossref_doi(doi, client, email)


def clone_client(client: HttpClient) -> HttpClient:
    return HttpClient(
        delay_seconds=client.delay_seconds,
        timeout_seconds=client.timeout_seconds,
        retries=client.retries,
    )


def resolve_openalex_doi(doi: str, client: HttpClient, email: str | None) -> Paper | None:
    encoded = urllib.parse.quote(f"https://doi.org/{doi}", safe="")
    url = f"https://api.openalex.org/works/{encoded}"
    if email:
        url += "?" + urlencode({"mailto": email})
    try:
        return paper_from_openalex(client.get_json(url))
    except Exception as exc:
        print(f"warning: OpenAlex DOI resolve failed for {doi}: {exc}", file=sys.stderr)
        return None


def resolve_crossref_doi(doi: str, client: HttpClient, email: str | None) -> Paper | None:
    encoded = urllib.parse.quote(doi, safe="")
    url = f"https://api.crossref.org/works/{encoded}"
    if email:
        url += "?" + urlencode({"mailto": email})
    try:
        return paper_from_crossref(client.get_json(url).get("message", {}))
    except Exception as exc:
        print(f"warning: Crossref DOI resolve failed for {doi}: {exc}", file=sys.stderr)
        return None


def resolve_arxiv_ids(arxiv_ids: list[str], client: HttpClient) -> list[Paper]:
    url = "https://export.arxiv.org/api/query?" + urlencode({"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)})
    try:
        body = client.get_bytes(url, headers={"Accept": "application/atom+xml"}).decode("utf-8")
    except Exception as exc:
        print(f"warning: arXiv ID resolve failed: {exc}", file=sys.stderr)
        return []
    root = ET.fromstring(body)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    return [paper_from_arxiv_entry(entry, ns) for entry in root.findall("atom:entry", ns)]


def enrich_with_unpaywall(papers: list[Paper], client: HttpClient, email: str | None) -> list[Paper]:
    if not email:
        return papers
    for paper in papers:
        if not paper.doi or paper.pdf_url:
            continue
        encoded = urllib.parse.quote(paper.doi, safe="")
        try:
            data = client.get_json(f"https://api.unpaywall.org/v2/{encoded}?" + urlencode({"email": email}))
        except Exception as exc:
            print(f"warning: Unpaywall lookup failed for {paper.doi}: {exc}", file=sys.stderr)
            continue
        best = data.get("best_oa_location") or {}
        paper.pdf_url = best.get("url_for_pdf") or paper.pdf_url
        paper.url = best.get("url") or paper.url
        if data.get("is_oa") and not paper.oa_status:
            paper.oa_status = data.get("oa_status") or "open"
        paper.metadata = {**paper.metadata, "unpaywall": {"is_oa": data.get("is_oa"), "oa_status": data.get("oa_status")}}
    return papers


def deduplicate(papers: list[Paper], *, sort_by: str = "newest") -> list[Paper]:
    from .identity import same_identity, relate_versions
    seen = []
    for paper in papers:
        match = next((i for i, old in enumerate(seen) if same_identity(old, paper)), None)
        if match is None:
            seen.append(paper)
        elif score_completeness(paper) > score_completeness(seen[match]):
            seen[match] = merge_papers(paper, seen[match])
        else:
            seen[match] = merge_papers(seen[match], paper)
    return sort_papers(relate_versions(seen), sort_by)


def sort_papers(papers: list[Paper], sort_by: str = "newest") -> list[Paper]:
    if sort_by == "relevance":
        return papers
    if sort_by == "citations":
        return sorted(papers, key=lambda p: (p.citations or 0, p.year or 0, p.title), reverse=True)
    return sorted(papers, key=lambda p: (p.year or 0, str(p.metadata.get("published") or ""), p.citations or 0, p.title), reverse=True)


def merge_papers(primary: Paper, secondary: Paper) -> Paper:
    from .identity import version_snapshots
    versions = version_snapshots(primary, secondary)
    pdf_urls = list(dict.fromkeys(url for p in (primary, secondary) for url in [p.pdf_url, *p.metadata.get("pdf_urls", [])] if url))
    sources = sorted({str(source) for p in (primary, secondary) for source in [p.source, *p.metadata.get("sources", [])]})
    queries = list(dict.fromkeys(q for p in (primary, secondary) for q in p.metadata.get("queries", [])))
    venues = list(dict.fromkeys(value for p in (primary, secondary) for value in [p.metadata.get("venue"), *p.metadata.get("venues", [])] if value))
    lists = {name: list(dict.fromkeys(value for p in (primary, secondary) for value in p.metadata.get(name, []))) for name in ("issn", "isbn", "openalex_source_ids")}
    primary.authors = primary.authors or secondary.authors
    primary.year = primary.year or secondary.year
    primary.doi = primary.doi or secondary.doi
    primary.url = primary.url or secondary.url
    primary.abstract = primary.abstract or secondary.abstract
    counts = [paper.citations for paper in (primary, secondary) if paper.citations is not None]
    primary.citations = max(counts) if counts else None
    primary.pdf_url = primary.pdf_url or secondary.pdf_url
    primary.oa_status = primary.oa_status or secondary.oa_status
    for name in ("arxiv_id", "dblp_id", "code_url", "affiliations", "keywords", "categories", "concepts", "venue", "venue_type"):
        if not getattr(primary, name):
            setattr(primary, name, getattr(secondary, name))
    for name in ("affiliations", "keywords", "categories", "concepts"):
        import json
        merged = {json.dumps(value, sort_keys=True, ensure_ascii=False): value for paper in (secondary, primary) for value in getattr(paper, name)}
        setattr(primary, name, list(merged.values()))
    formal = next((paper for paper in (primary, secondary) if paper.publication_status in {"journal", "journal-article", "conference", "proceedings-article"}), None)
    if formal:
        primary.publication_status = formal.publication_status
        primary.venue_type = formal.venue_type
        primary.venue = formal.venue or formal.metadata.get("venue") or primary.venue
    raw_records = [*secondary.metadata.get("raw_records", []), *primary.metadata.get("raw_records", [])]
    import json
    raw_records = list({json.dumps(record, sort_keys=True, ensure_ascii=False): record for record in raw_records}.values())
    primary.versions = versions
    primary.sources = sources
    primary.metadata = {**secondary.metadata, **primary.metadata, "versions": versions, "raw_records": raw_records, **lists, "sources": sources, "pdf_urls": pdf_urls, "queries": queries, "venues": venues}
    if formal:
        primary.metadata["venue"] = primary.venue
    return primary


def score_completeness(paper: Paper) -> int:
    return sum(1 for field in [paper.title, paper.authors, paper.year, paper.doi, paper.url, paper.abstract, paper.pdf_url] if field)


def paper_from_openalex(item: dict) -> Paper:
    authors = [
        authorship.get("author", {}).get("display_name", "")
        for authorship in item.get("authorships", [])
        if authorship.get("author", {}).get("display_name")
    ]
    doi = item.get("doi")
    oa = item.get("open_access") or {}
    best = item.get("best_oa_location") or {}
    locations = [best, *(item.get("locations") or [])]
    pdf_urls = list(dict.fromkeys(loc["pdf_url"] for loc in locations if loc and loc.get("pdf_url") and (loc.get("is_oa") or loc is best)))
    return Paper(
        title=item.get("title") or "",
        source="OpenAlex",
        authors=authors,
        year=item.get("publication_year"),
        doi=normalize_doi(doi) if doi else None,
        url=best.get("landing_page_url") or (item.get("primary_location") or {}).get("landing_page_url") or item.get("id"),
        abstract=inverted_index_to_text(item.get("abstract_inverted_index")),
        citations=item.get("cited_by_count"),
        pdf_url=first(pdf_urls),
        oa_status=oa.get("oa_status") if oa.get("is_oa") else "closed",
        affiliations=list(dict.fromkeys(inst.get("display_name") for author in item.get("authorships", []) for inst in author.get("institutions", []) if inst.get("display_name"))),
        concepts=item.get("concepts") or [], keywords=[entry.get("display_name", "") for entry in item.get("keywords", [])],
        metadata={"raw_records": [{"source": "openalex", "record": item}], "openalex_source_ids": list(dict.fromkeys(loc["source"]["id"].rsplit("/", 1)[-1] for loc in [item.get("primary_location") or {}, *locations] if loc.get("source") and loc["source"].get("id"))), "issn": ((item.get("primary_location") or {}).get("source") or {}).get("issn") or [], "openalex_id": item.get("id"), "published": item.get("publication_date"), "pdf_urls": pdf_urls,
                  "venue": ((item.get("primary_location") or {}).get("source") or {}).get("display_name"), "publication_type": item.get("type")},
    )


def paper_from_crossref(item: dict) -> Paper:
    authors = []
    for author in item.get("author", []):
        name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
        if name:
            authors.append(name)
    doi = item.get("DOI")
    license_url = first((lic.get("URL") for lic in item.get("license", []) if lic.get("URL")))
    return Paper(
        title=first(item.get("title")) or "",
        source="Crossref",
        authors=authors,
        year=extract_crossref_year(item),
        doi=normalize_doi(doi) if doi else None,
        url=item.get("URL"),
        abstract=clean_abstract(item.get("abstract")),
        citations=item.get("is-referenced-by-count"),
        pdf_url=first_link_url(item, content_type="application/pdf"),
        oa_status="license_present" if license_url else None,
        affiliations=list(dict.fromkeys(aff.get("name") for author in item.get("author", []) for aff in author.get("affiliation", []) if aff.get("name"))),
        keywords=item.get("subject") or [],
        metadata={"raw_records": [{"source": "crossref", "record": item}], "issn": item.get("ISSN") or [], "isbn": item.get("ISBN") or [], "publisher": item.get("publisher"), "license": license_url, "published": crossref_publication_date(item),
                  "venue": first(item.get("container-title")), "publication_type": item.get("type"),
                  "pdf_urls": [link["URL"] for link in item.get("link", []) if link.get("content-type") == "application/pdf" and link.get("URL")]},
    )


def paper_from_arxiv_entry(entry: ET.Element, ns: dict[str, str]) -> Paper:
    published = text(entry, "atom:published", ns)
    year = int(published[:4]) if published[:4].isdigit() else None
    doi = text(entry, "arxiv:doi", ns) or None
    pdf_url = None
    for link in entry.findall("atom:link", ns):
        if link.attrib.get("title") == "pdf":
            pdf_url = link.attrib.get("href")
            break
    return Paper(
        title=" ".join(text(entry, "atom:title", ns).split()),
        source="arXiv",
        authors=[text(author, "atom:name", ns) for author in entry.findall("atom:author", ns)],
        year=year,
        doi=normalize_doi(doi) if doi else None,
        url=text(entry, "atom:id", ns),
        abstract=" ".join(text(entry, "atom:summary", ns).split()),
        pdf_url=pdf_url,
        oa_status="green",
        categories=[e.get("term") for e in entry.findall("atom:category", ns) if e.get("term")],
        metadata={"raw_records": [{"source": "arxiv", "record": ET.tostring(entry, encoding="unicode")}], "journal_ref": text(entry, "arxiv:journal_ref", ns), "arxiv_id": text(entry, "atom:id", ns).rsplit("/", 1)[-1], "published": published, "updated": text(entry, "atom:updated", ns), "publication_type": "preprint", "venue": text(entry, "arxiv:journal_ref", ns) or "arXiv"},
    )


def inverted_index_to_text(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positions: list[tuple[int, str]] = []
    for word, indices in index.items():
        positions.extend((position, word) for position in indices)
    return " ".join(word for _, word in sorted(positions))


def clean_abstract(value: str | None) -> str | None:
    return html.unescape(ABSTRACT_TAG_RE.sub("", value)).strip() if value else None


def extract_crossref_year(item: dict) -> int | None:
    value = crossref_publication_date(item)
    return int(value[:4]) if value else None


def crossref_publication_date(item: dict) -> str | None:
    dates = []
    for key in ("published-online", "published-print", "published", "issued"):
        parts = item.get(key, {}).get("date-parts")
        if parts and parts[0]:
            try:
                year, month, day = [*parts[0], 1, 1][:3]
                dates.append(date(int(year), int(month), int(day)).isoformat())
            except (TypeError, ValueError):
                continue
    return min(dates) if dates else None


def first_link_url(item: dict, *, content_type: str) -> str | None:
    return first(link.get("URL") for link in item.get("link", []) if link.get("content-type") == content_type and link.get("URL"))


def first(values):
    for value in values or []:
        if value:
            return value
    return None


def text(element: ET.Element, path: str, ns: dict[str, str]) -> str:
    found = element.find(path, ns)
    return found.text.strip() if found is not None and found.text else ""


def default_end_year() -> int:
    return date.today().year


def locals_for_search(values):
    return {key: value for key, value in values.items() if key != "_search"}
