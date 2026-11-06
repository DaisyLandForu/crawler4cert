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
        filters.append(f"from_publication_date:{from_date or str(start_year) + '-01-01'}")
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
    batch_size = min(page_size, max_results, 100)
    seen = set()
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
        added = 0
        for item in results:
            identifier = item.get("id") or (item.get("doi"), item.get("title"), item.get("publication_year"))
            if identifier in seen:
                continue
            seen.add(identifier)
            added += 1
            if item.get("title") and in_year_range(item.get("publication_year"), start_year, end_year):
                papers.append(paper_from_openalex(item))
        page += 1
        if not added or len(results) < batch_size:
            break
    return papers[:max_results]


class OpenAlexAdapter(Adapter):
    source_id = "openalex"
    capabilities = {"metadata": True, "venue_catalog": True, "preprint": False, "pdf": False}

    def search(self, query, filters):
        from .. import search as api
        record = filters.get("venue_record") or {}
        venue = record.get("name") or filters.get("venue")
        if record.get("openalex_source_ids"):
            SOURCE_IDS[canonical(venue)] = "|".join(value.rsplit("/", 1)[-1] for value in record["openalex_source_ids"])
        return api.search_openalex(query, filters.get("max_results", 100), filters.get("start_year"), filters.get("end_year"), self.client, filters.get("email"), filters.get("page_size", 50), filters.get("sort_by", "newest"), title_filter=filters.get("title_filter"), venue=venue, from_date=filters.get("from_date"))

    def normalize(self, raw_record):
        from .. import search as api
        return api.paper_from_openalex(raw_record)
