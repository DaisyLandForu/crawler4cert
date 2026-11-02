#!/usr/bin/env python3
"""Run existing conference spiders without their title-only/citation pipeline.

Runs as a separate process so Scrapy's reactor never conflicts with the main CLI.
The external checkout is imported, never edited.
"""
from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from pathlib import Path


def configured_years(spider_class) -> set[int] | None:
    source = inspect.getsource(spider_class.parse)
    if "GET_dict" not in source:
        return None
    return {int(year) for year in re.findall(r'["\x27](\d{4})["\x27]\s*:', source)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--venues", default="cvpr,iclr,acl,emnlp")
    parser.add_argument("--years", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--max-items", type=int, default=0, help="Smoke-run cap per spider; 0 is unlimited.")
    args = parser.parse_args()
    project = args.checkout.resolve() / "crawl_conf"
    if not (project / "scrapy.cfg").is_file():
        parser.error(f"No Scrapy project under {project}")
    sys.path.insert(0, str(project))
    from scrapy.crawler import CrawlerProcess
    from scrapy.settings import Settings

    settings = Settings()
    settings.setmodule("crawl_conf.settings")
    settings.set("ITEM_PIPELINES", {}, priority="cmdline")
    settings.set("FEED_EXPORT_FIELDS", ["conf", "title", "abstract", "authors", "pdf_url", "doi"], priority="cmdline")
    # Each spider must write its own feed. Sharing an overwrite=True URI would
    # let the last crawler truncate results from earlier crawlers.
    feed_uri = args.output.parent.resolve().as_uri().replace("%", "%%") + "/%(name)s.jsonl"
    settings.set("FEEDS", {feed_uri: {"format": "jsonlines", "encoding": "utf-8", "overwrite": True}}, priority="cmdline")
    settings.set("LOG_LEVEL", "INFO", priority="cmdline")
    settings.set("CONCURRENT_REQUESTS_PER_DOMAIN", 2, priority="cmdline")
    settings.set("DOWNLOAD_DELAY", 2, priority="cmdline")
    settings.set("AUTOTHROTTLE_ENABLED", True, priority="cmdline")
    settings.set("TELNETCONSOLE_ENABLED", False, priority="cmdline")
    settings.set("CLOSESPIDER_ITEMCOUNT", args.max_items, priority="cmdline")
    process = CrawlerProcess(settings)
    years = [int(value) for value in args.years.split(",")]
    report = {"venues": [], "unsupported_years": {}}
    crawlers = []
    for venue in args.venues.split(","):
        crawler = process.create_crawler(venue.strip().lower())
        supported = configured_years(crawler.spidercls)
        selected = [year for year in years if supported is None or year in supported]
        report["unsupported_years"][venue] = [year for year in years if year not in selected]
        if not selected:
            continue
        crawlers.append((venue, crawler))
        process.crawl(crawler, years=",".join(map(str, selected)), queries="*", nocrossref=True,
                      download_pdf=-1, pdf_dir=str(args.output.parent))
    process.start()
    records = []
    for venue, crawler in crawlers:
        stats = crawler.stats.get_stats()
        count = stats.get("item_scraped_count", 0)
        errors = stats.get("log_count/ERROR", 0)
        report["venues"].append({"venue": venue, "count": count,
                                  "status": "partial" if count and errors else ("ok" if count else "empty_or_failed"),
                                  "errors": errors, "finish_reason": stats.get("finish_reason"),
                                  "http": {key: value for key, value in stats.items() if "response_status_count" in key}})
        feed = args.output.parent / f"{venue}.jsonl"
        if feed.exists():
            records.extend(json.loads(line) for line in feed.read_text(encoding="utf-8").splitlines() if line.strip())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    args.stats.parent.mkdir(parents=True, exist_ok=True)
    args.stats.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
