"""Discover articles on an explicitly configured, public official catalog."""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from .base import Adapter, UnsupportedCapability
from ..models import Paper


class CatalogParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.metadata = [], {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "meta" and attrs.get("name", "").lower().startswith("citation_"):
            self.metadata.setdefault(attrs["name"].lower(), []).append(attrs.get("content", ""))


def article_metadata(body, url, venue=None):
    parser = CatalogParser()
    parser.feed(body)
    get = lambda key: (parser.metadata.get(key) or [None])[0]
    title = get("citation_title")
    if not title:
        return None
    published = get("citation_publication_date") or get("citation_date") or ""
    return Paper(title, "Publisher", authors=parser.metadata.get("citation_author", []), year=int(published[:4]) if published[:4].isdigit() else None,
                 doi=get("citation_doi"), url=url, pdf_url=urljoin(url, get("citation_pdf_url")) if get("citation_pdf_url") else None,
                 metadata={"venue": get("citation_journal_title") or get("citation_conference_title") or (venue or {}).get("name"),
                           "published": published.replace("/", "-"), "issn": parser.metadata.get("citation_issn", []),
                           "raw_records": [{"source": "publisher", "url": url, "record": parser.metadata}]})


class PublisherAdapter(Adapter):
    source_id = "publisher"
    capabilities = {"metadata": True, "venue_catalog": True, "preprint": False, "pdf": False}

    def normalize(self, raw_record):
        return article_metadata(raw_record["html"], raw_record["url"])

    def search(self, query, filters):
        venue = filters.get("venue_record") or (self.registry.venue(filters["venue"]) if self.registry and filters.get("venue") else None)
        config = (venue or {}).get("publisher", {})
        url = config.get("catalog_url")
        if not url:
            raise UnsupportedCapability("出版社发现需要配置公开 catalog_url 和 article_path；未配置不代表已实现")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("出版社目录需要 HTTPS URL")
        robots = RobotFileParser()
        try:
            robots.parse(self.client.get_bytes(f"https://{parsed.netloc}/robots.txt").decode("utf-8").splitlines())
        except Exception as exc:
            raise UnsupportedCapability(f"无法核对站点 robots.txt，未进行目录采集：{exc}") from exc
        user_agent = "crawler4cert"
        def fetch(page):
            if not robots.can_fetch(user_agent, page):
                raise UnsupportedCapability("站点 robots.txt 不允许采集此 URL")
            return self.client.get_bytes(page).decode("utf-8", errors="replace")
        body = fetch(url)
        direct = article_metadata(body, url, venue)
        parser = CatalogParser()
        parser.feed(body)
        path = config.get("article_path")
        links = [urljoin(url, link) for link in parser.links if path and path in link]
        links = [link for link in dict.fromkeys(links) if urlparse(link).netloc == parsed.netloc]
        # Search only the configured catalog, with an explicit crawl bound.
        papers = [direct] if direct else []
        for link in links[:filters.get("max_results", 100)]:
            paper = article_metadata(fetch(link), link, venue)
            if paper:
                papers.append(paper)
        words = query.lower().split()
        return [paper for paper in papers if all(word in paper.title.lower() for word in words)
                and (not filters.get("start_year") or (paper.year or 0) >= filters["start_year"])
                and (not filters.get("end_year") or (paper.year or 9999) <= filters["end_year"])][:filters.get("max_results", 100)]
