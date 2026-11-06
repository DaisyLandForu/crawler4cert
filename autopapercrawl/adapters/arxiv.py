from __future__ import annotations
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from .base import Adapter
from ..http import HttpClient, urlencode
from ..models import Paper
from ..venues import canonical, venue_matches, is_conference
from ..search import publication_cutoff, in_year_range, first, text, extract_crossref_year, paper_from_openalex, paper_from_crossref, paper_from_arxiv_entry, SOURCE_IDS, CROSSREF_ISSNS, DEFAULT_PAGE_SIZE


def _search(
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
    if re.search(r"\b(?:all|ti|abs|au|cat|id|submittedDate|lastUpdatedDate):", query):
        expression = query
    else:
        tokens = re.findall(r'"[^"]+"|\S+', query)
        expression = " AND ".join(f"all:{token}" for token in tokens)
    if title_filter:
        title_expression = 'ti:"' + title_filter.replace('"', ' ') + '"'
        expression = f"({expression}) AND {title_expression}" if expression and query != title_filter else title_expression
    if start_year or end_year:
        lower = from_date.replace("-", "") + "0000" if from_date else f"{start_year or 1991}01010000"
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


class ArxivAdapter(Adapter):
    source_id = "arxiv"
    capabilities = {"metadata": True, "venue_catalog": False, "preprint": True, "pdf": False}

    def search(self, query, filters):
        from .. import search as api
        return api.search_arxiv(query, filters.get("max_results", 100), filters.get("start_year"), filters.get("end_year"), self.client, filters.get("page_size", 50), filters.get("sort_by", "newest"), title_filter=filters.get("title_filter"), from_date=filters.get("from_date"))

    def normalize(self, raw_record):
        from .. import search as api
        return api.paper_from_arxiv_entry(ET.fromstring(raw_record) if isinstance(raw_record, str) else raw_record, {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"})
