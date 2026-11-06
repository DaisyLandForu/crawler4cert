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
    email: str | None,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "newest",
    venue: str | None = None,
    title_filter: str | None = None,
    from_date: str | None = None,
) -> list[Paper]:
    filters = []
    if start_year:
        filters.append(f"from-pub-date:{from_date or str(start_year) + '-01-01'}")
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
    seen = set()
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
        added = 0
        for item in items:
            identifier = item.get("DOI") or (first(item.get("title")), extract_crossref_year(item))
            if identifier in seen:
                continue
            seen.add(identifier)
            added += 1
            title = first(item.get("title")) or ""
            if not title:
                continue
            year = extract_crossref_year(item)
            if not in_year_range(year, start_year, end_year):
                continue
            papers.append(paper_from_crossref(item))
        offset += len(items)
        if not added or offset >= 10000:
            break
    return papers[:max_results]


class CrossrefAdapter(Adapter):
    source_id = "crossref"
    capabilities = {"metadata": True, "venue_catalog": True, "preprint": False, "pdf": False}

    def search(self, query, filters):
        from .. import search as api
        record = filters.get("venue_record") or {}
        venue = record.get("name") or filters.get("venue")
        if record.get("issn"):
            CROSSREF_ISSNS[canonical(venue)] = record["issn"][0]
        return api.search_crossref(query, filters.get("max_results", 100), filters.get("start_year"), filters.get("end_year"), self.client, filters.get("email"), filters.get("page_size", 50), filters.get("sort_by", "newest"), title_filter=filters.get("title_filter"), venue=venue, from_date=filters.get("from_date"))

    def normalize(self, raw_record):
        from .. import search as api
        return api.paper_from_crossref(raw_record)
