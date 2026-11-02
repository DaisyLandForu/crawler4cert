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
) -> list[Paper]:
    if max_results < 1 or page_size < 1:
        raise ValueError("max_results and page_size must be positive")
    if start_year and end_year and start_year > end_year:
        raise ValueError("start_year must not exceed end_year")
    selected = sources or ["openalex", "crossref", "arxiv"]
    per_source = max(1, max_results)
    papers: list[Paper] = []

    extra = {"venue": venue, "title_filter": title_filter} if venue or title_filter else {}
    source_calls = {
        "openalex": lambda source_client: search_openalex(query, per_source, start_year, end_year, source_client, email, page_size, sort_by, **extra),
        "crossref": lambda source_client: search_crossref(query, per_source, start_year, end_year, source_client, email, page_size, sort_by, **extra),
        "arxiv": lambda source_client: search_arxiv(query, per_source, start_year, end_year, source_client, page_size, sort_by, title_filter=title_filter),
    }
    selected_calls = []
    for source in selected:
        call = source_calls.get(source)
        if call is None:
            raise ValueError(f"Unknown metadata source: {source}")
        selected_calls.append((source, call))

    def record(source, result=None, error=None):
        if diagnostics is not None:
            diagnostics.append({"source": source, "query": query, "status": "request_failed" if error else ("ok" if result else "empty"), "count": len(result or []), "error": str(error) if error else None})

    if source_workers <= 1 or len(selected_calls) <= 1:
        for source, call in selected_calls:
            try:
                result = call(clone_client(client))
                papers.extend(result)
                record(source, result)
            except Exception as exc:
                print(f"warning: {source} search failed: {exc}", file=sys.stderr)
                record(source, error=exc)
    else:
        with ThreadPoolExecutor(max_workers=min(source_workers, len(selected_calls))) as executor:
            futures = {
                executor.submit(call, clone_client(client)): source
                for source, call in selected_calls
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    result = future.result()
                    papers.extend(result)
                    record(source, result)
                except Exception as exc:
                    print(f"warning: {source} search failed: {exc}", file=sys.stderr)
                    record(source, error=exc)

    combined = deduplicate(papers, sort_by=sort_by)
    return combined[:max_results] if truncate_results else combined


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
) -> list[Paper]:
    filters = []
    if start_year:
        filters.append(f"from_publication_date:{start_year}-01-01")
    if end_year:
        filters.append(f"to_publication_date:{publication_cutoff(end_year)}")
    if venue:
        name = canonical(venue)
        if name not in SOURCE_IDS:
            results = client.get_json("https://api.openalex.org/sources?" + urlencode({"search": name, "per-page": 25})).get("results", [])
            matched = next((source for source in results if venue_matches(source.get("display_name") or "", venue)), None)
            if not matched:
                raise ValueError(f"OpenAlex 未找到该会议/期刊的来源标识：{name}")
            SOURCE_IDS[name] = matched["id"].rsplit("/", 1)[-1]
        filters.append("primary_location.source.id:" + SOURCE_IDS[name])
    if title_filter:
        # Punctuation has special meaning in OpenAlex filter values.
        filters.append("title.search:" + re.sub(r"[^\w\s-]", " ", title_filter).strip())

    papers: list[Paper] = []
    page = 1
    batch_size = min(page_size, max_results, 200)
    while len(papers) < max_results:
        params = {
            "search": query,
            "per-page": batch_size,
            "page": page,
            "filter": ",".join(filters) if filters else None,
            "mailto": email,
            "sort": {"newest": "publication_date:desc", "citations": "cited_by_count:desc"}.get(sort_by),
        }
        print(
            f"[openalex] page={page} per_page={params['per-page']} collected={len(papers)}/{max_results}",
            file=sys.stderr,
        )
        data = client.get_json("https://api.openalex.org/works?" + urlencode(params))
        results = data.get("results", [])
        if not results:
            break
        for item in results:
            if item.get("title") and in_year_range(item.get("publication_year"), start_year, end_year):
                papers.append(paper_from_openalex(item))
        page += 1
        if len(results) < batch_size:
            break
    return papers[:max_results]


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
) -> list[Paper]:
    filters = []
    if start_year:
        filters.append(f"from-pub-date:{start_year}-01-01")
    if end_year:
        filters.append(f"until-pub-date:{publication_cutoff(end_year)}")
    issn = None
    if venue:
        name = canonical(venue)
        if name not in CROSSREF_ISSNS and not is_conference(venue):
            try:
                journals = client.get_json("https://api.crossref.org/journals?" + urlencode({"query": name, "rows": 20})).get("message", {}).get("items", [])
                matched = next((journal for journal in journals if venue_matches(str(journal.get("title") or ""), venue)), None)
                CROSSREF_ISSNS[name] = first(matched.get("ISSN")) if matched else None
            except Exception as exc:
                print(f"warning: Crossref journal lookup failed: {exc}", file=sys.stderr)
        issn = CROSSREF_ISSNS.get(name)
        if issn:
            filters.append("issn:" + issn)

    papers: list[Paper] = []
    offset = 0
    while len(papers) < max_results:
        params = {
            "query": query,
            "query.container-title": canonical(venue) if venue else None,
            "query.title": title_filter,
            "rows": min(page_size, max_results - len(papers), 100),
            "offset": offset,
            "filter": ",".join(filters) if filters else None,
            "mailto": email,
            # Without an ISSN, container-title is a relevance query, not an
            # exact filter. First recall matching proceedings; the merged pool
            # is sorted by date after checking its actual container title.
            "sort": "relevance" if venue and not issn else {"newest": "published", "citations": "is-referenced-by-count"}.get(sort_by),
            "order": "desc" if sort_by != "relevance" else None,
        }
        print(
            f"[crossref] offset={offset} rows={params['rows']} collected={len(papers)}/{max_results}",
            file=sys.stderr,
        )
        data = client.get_json("https://api.crossref.org/works?" + urlencode(params))
        items = data.get("message", {}).get("items", [])
        if not items:
            break
        for item in items:
            title = first(item.get("title")) or ""
            if not title:
                continue
            year = extract_crossref_year(item)
            if not in_year_range(year, start_year, end_year):
                continue
            papers.append(paper_from_crossref(item))
        offset += len(items)
    return papers[:max_results]


def search_arxiv(
    query: str,
    max_results: int,
    start_year: int | None,
    end_year: int | None,
    client: HttpClient,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "newest",
    title_filter: str | None = None,
) -> list[Paper]:
    # Keep explicit field/Boolean queries intact. Ordinary topic words must each
    # receive a field prefix; all:foo bar does not reliably mean all:foo AND all:bar.
    if re.search(r"\b(?:all|ti|abs|au|cat|id|submittedDate|lastUpdatedDate):", query):
        expression = query
    else:
        tokens = re.findall(r'"[^"]+"|\S+', query)
        expression = " AND ".join(f"all:{token}" for token in tokens)
    if title_filter:
        title_expression = 'ti:"' + title_filter.replace('"', ' ') + '"'
        expression = f"({expression}) AND {title_expression}" if expression and query != title_filter else title_expression
    if start_year or end_year:
        lower = f"{start_year or 1991}01010000"
        upper = publication_cutoff(end_year or date.today().year).replace("-", "") + "2359"
        dates = f"submittedDate:[{lower} TO {upper}]"
        expression = f"({expression}) AND {dates}" if expression else dates
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    client.delay_seconds = max(client.delay_seconds, 3.0)
    papers: list[Paper] = []
    seen: set[str] = set()
    offset = 0
    while len(papers) < max_results:
        batch_size = min(max_results - len(papers), page_size, 100)
        params = {"search_query": expression, "start": offset, "max_results": batch_size,
                  "sortBy": "submittedDate" if sort_by == "newest" else "relevance", "sortOrder": "descending"}
        print(f"[arxiv] start={offset} collected={len(papers)}/{max_results}", file=sys.stderr)
        body = client.get_bytes("https://export.arxiv.org/api/query?" + urlencode(params), headers={"Accept": "application/atom+xml"})
        root = ET.fromstring(body)
        entries = root.findall("atom:entry", ns)
        if not entries:
            break
        added_ids = 0
        for entry in entries:
            identifier = text(entry, "atom:id", ns)
            if "api/errors" in identifier:
                raise RuntimeError(text(entry, "atom:summary", ns))
            if identifier in seen:
                continue
            seen.add(identifier)
            added_ids += 1
            paper = paper_from_arxiv_entry(entry, ns)
            if in_year_range(paper.year, start_year, end_year):
                papers.append(paper)
        offset += len(entries)
        total = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
        if not added_ids or (total and offset >= int(total)) or (not total and len(entries) < batch_size):
            break
    return papers


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
    seen: dict[str, Paper] = {}
    titles: dict[str, str] = {}
    doi_keys: dict[str, str] = {}
    for paper in papers:
        title = re.sub(r"[^\w]+", " ", paper.title.lower()).strip()
        key = paper.key
        doi_key = normalize_doi(paper.doi) if paper.doi else None
        existing_key = doi_keys.get(doi_key) or (key if key in seen else titles.get(title))
        existing = seen.get(existing_key) if existing_key else None
        # Different DOIs with the same title can be distinct publications.
        if existing and existing.doi and paper.doi and normalize_doi(existing.doi) != normalize_doi(paper.doi):
            existing = None
        if existing is None:
            seen[key] = paper
            if title:
                titles[title] = key
            if doi_key:
                doi_keys[doi_key] = key
            continue
        if score_completeness(paper) > score_completeness(existing):
            seen[existing_key] = merge_papers(paper, existing)
        else:
            seen[existing_key] = merge_papers(existing, paper)
        if title:
            titles[title] = existing_key
        if seen[existing_key].doi:
            doi_keys[normalize_doi(seen[existing_key].doi)] = existing_key
    return sort_papers(list(seen.values()), sort_by)


def sort_papers(papers: list[Paper], sort_by: str = "newest") -> list[Paper]:
    if sort_by == "relevance":
        return papers
    if sort_by == "citations":
        return sorted(papers, key=lambda p: (p.citations or 0, p.year or 0, p.title), reverse=True)
    return sorted(papers, key=lambda p: (p.year or 0, str(p.metadata.get("published") or ""), p.citations or 0, p.title), reverse=True)


def merge_papers(primary: Paper, secondary: Paper) -> Paper:
    pdf_urls = list(dict.fromkeys(url for p in (primary, secondary) for url in [p.pdf_url, *p.metadata.get("pdf_urls", [])] if url))
    sources = sorted({str(source) for p in (primary, secondary) for source in [p.source, *p.metadata.get("sources", [])]})
    queries = list(dict.fromkeys(q for p in (primary, secondary) for q in p.metadata.get("queries", [])))
    venues = list(dict.fromkeys(value for p in (primary, secondary) for value in [p.metadata.get("venue"), *p.metadata.get("venues", [])] if value))
    primary.authors = primary.authors or secondary.authors
    primary.year = primary.year or secondary.year
    primary.doi = primary.doi or secondary.doi
    primary.url = primary.url or secondary.url
    primary.abstract = primary.abstract or secondary.abstract
    primary.citations = max(primary.citations or 0, secondary.citations or 0) or None
    primary.pdf_url = primary.pdf_url or secondary.pdf_url
    primary.oa_status = primary.oa_status or secondary.oa_status
    primary.metadata = {**secondary.metadata, **primary.metadata, "sources": sources, "pdf_urls": pdf_urls, "queries": queries, "venues": venues}
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
        metadata={"openalex_id": item.get("id"), "published": item.get("publication_date"), "pdf_urls": pdf_urls,
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
        metadata={"publisher": item.get("publisher"), "license": license_url, "published": crossref_publication_date(item),
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
        metadata={"arxiv_id": text(entry, "atom:id", ns).rsplit("/", 1)[-1], "published": published, "updated": text(entry, "atom:updated", ns), "publication_type": "preprint", "venue": text(entry, "arxiv:journal_ref", ns) or "arXiv"},
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
