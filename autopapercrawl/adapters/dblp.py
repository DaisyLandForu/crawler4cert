"""Independent DBLP JSON API adapter; no paperCrawler source copied."""
from __future__ import annotations

import re
import html
from urllib.parse import urlparse
from .base import Adapter, UnsupportedCapability
from ..http import urlencode
from ..models import Paper, normalize_doi


def parse_dblp_key(value):
    value = str(value).strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.hostname not in {"dblp.org", "dblp.uni-trier.de", "dblp.dagstuhl.de"}:
            raise ValueError("只接受官方 DBLP 目录 URL")
        value = re.sub(r"^(?:db|streams)/", "", parsed.path.strip("/"))
    parts = value.removesuffix(".xml").split("/")
    if len(parts) < 2 or parts[0] not in {"conf", "journals"} or not re.fullmatch(r"[a-zA-Z0-9_-]+", parts[1]):
        raise ValueError("DBLP key 需为 conf/sp、journals/jmlr 或其官方目录 URL")
    return "/".join(parts[:2])


def normalize_hit(info, stream=None):
    authors = (info.get("authors") or {}).get("author", [])
    if not isinstance(authors, list):
        authors = [authors]
    authors = [(a.get("text") or a.get("#text") or "") if isinstance(a, dict) else str(a) for a in authors]
    urls = info.get("ee") or []
    if not isinstance(urls, list):
        urls = [urls]
    urls = [(v.get("text") or v.get("#text") or "") if isinstance(v, dict) else str(v) for v in urls]
    doi = info.get("doi") or next((url.split("doi.org/", 1)[1] for url in urls if "doi.org/" in url), None)
    key = info.get("key") or (info.get("url") or "").split("/rec/")[-1]
    kind = "journal" if info.get("type") == "Journal Articles" else "conference"
    return Paper(title=html.unescape(re.sub(r"<[^>]+>", "", info.get("title") or "")), source="DBLP", authors=authors,
                 year=int(info["year"]) if str(info.get("year", "")).isdigit() else None,
                 doi=normalize_doi(doi) if doi else None, url=info.get("url"),
                 metadata={"dblp_id": key, "dblp_stream": stream, "venue": info.get("venue"), "publication_type": kind,
                           "electronic_editions": urls, "sources": ["DBLP"], "raw_records": [{"source": "dblp", "record": info}]})


class DBLPAdapter(Adapter):
    source_id = "dblp"
    capabilities = {"metadata": True, "venue_catalog": True, "preprint": False, "pdf": False}

    def normalize(self, raw_record):
        return normalize_hit(raw_record.get("info", raw_record))

    def search(self, query, filters):
        venue = filters.get("venue_record")
        if not venue and filters.get("venue") and self.registry:
            venue = self.registry.venue(filters["venue"])
        stream = filters.get("dblp_key") or (venue or {}).get("dblp_key") or (venue or {}).get("dblp_url")
        if filters.get("venue") and not stream:
            raise UnsupportedCapability("该会议/期刊未配置 DBLP 目录；不能以空结果表示支持")
        stream = parse_dblp_key(stream) if stream else None
        maximum = filters.get("max_results", 100)
        size = min(filters.get("page_size", 100), 1000, maximum)
        output, seen = [], set()
        lower, upper = filters.get("start_year"), filters.get("end_year")
        if filters.get("from_date"):
            lower = max(lower or 1991, int(filters["from_date"][:4]))
        years = list(range(upper, lower - 1, -1)) if lower and upper else [None]
        prefix = f"stream:streams/{stream}:" if stream else ""
        if stream:
            probe = self.client.get_json("https://dblp.org/search/publ/api?" + urlencode({"q": prefix, "h": 1, "format": "json"}))
            if not int(probe.get("result", {}).get("hits", {}).get("@total", 0)):
                raise UnsupportedCapability(f"DBLP 未收录目录或无法识别 key：{stream}")
        for year in years:
            offset = 0
            while len(output) < maximum:
                expression = " ".join(part for part in [prefix, f"year:{year}:" if year else "", query, filters.get("title_filter") or ""] if part)
                data = self.client.get_json("https://dblp.org/search/publ/api?" + urlencode({"q": expression, "h": min(size, maximum - len(output)), "f": offset, "format": "json"}))
                hits = data.get("result", {}).get("hits", {})
                entries = hits.get("hit", [])
                if isinstance(entries, dict):
                    entries = [entries]
                if not entries:
                    break
                added = 0
                for hit in entries:
                    paper = normalize_hit(hit["info"], stream)
                    if paper.metadata["dblp_id"] in seen:
                        continue
                    seen.add(paper.metadata["dblp_id"])
                    added += 1
                    if paper.title and (not lower or (paper.year or 0) >= lower) and (not upper or (paper.year or 9999) <= upper):
                        output.append(paper)
                offset += len(entries)
                if not added or offset >= int(hits.get("@total", offset)):
                    break
                if offset >= 10000:
                    raise UnsupportedCapability("DBLP 返回超过分页窗口，请缩小年份或关键词范围")
            if len(output) >= maximum:
                break
        return output[:maximum]
