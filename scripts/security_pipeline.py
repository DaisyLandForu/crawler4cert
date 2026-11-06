#!/usr/bin/env python3
"""Search, classify and download recent papers for four cybersecurity topics."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autopapercrawl.conferences import crawl_conferences, load_conference_file
from autopapercrawl.downloaders.common import ensure_pdf_bytes
from autopapercrawl.http import HttpClient
from autopapercrawl.models import Paper
from autopapercrawl.open_access import download_public_pdf
from autopapercrawl.search import deduplicate, in_year_range, publication_cutoff, search_all
from autopapercrawl.targets import paper_to_target
from autopapercrawl.topics import DEFAULT_CONFIG, classify_paper, load_topics
from scripts.llm_utils import load_dotenv


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--directions", default="all", help="Comma-separated topic IDs, or all.")
    parser.add_argument("--start-year", type=int, default=date.today().year - 2)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--sources", default=os.getenv("PAPER_SOURCES", "openalex,crossref,arxiv"))
    parser.add_argument("--max-results-per-query", type=int, default=40, help="Candidates per source per query, before relevance filtering.")
    parser.add_argument("--max-per-direction", type=int, default=200, help="0 keeps all relevant papers.")
    parser.add_argument("--query-limit", type=int, default=0, help="For a smoke run, use only the first N queries per direction. 0 uses all.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "security_research")
    parser.add_argument("--download-dir", type=Path, default=ROOT / "downloads" / "security_research")
    parser.add_argument("--metadata-only", action="store_true", help="Skip downloads. PDFs are downloaded by default.")
    parser.add_argument("--max-downloads", type=int, default=0, help="0 attempts all accepted papers; other papers are marked pending.")
    parser.add_argument("--no-oa-lookup", action="store_true", help="Only try known PDF links; skip DOI/title full-text lookups.")
    parser.add_argument("--email", default=os.getenv("PAPER_EMAIL"), help="Optional contact email; enables Unpaywall full-text lookup.")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--source-workers", type=int, default=3)
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--existing-policy", choices=("append", "overwrite", "fail"), default="append")
    parser.add_argument("--input-json", type=Path, action="append", default=[], help="Use local papers.json files instead of running metadata searches.")
    parser.add_argument("--papercrawler-csv", type=Path, action="append", default=[], help="Import existing paperCrawler CSV/JSON results.")
    parser.add_argument("--with-papercrawler", action="store_true", help="Also run existing conference spiders. This can be slow.")
    parser.add_argument("--papercrawler-path", type=Path, default=ROOT.parent / "paperCrawler")
    parser.add_argument("--conference-venues", default="cvpr,iclr,acl,emnlp")
    parser.add_argument("--conference-timeout", type=int, default=1800)
    parser.add_argument("--conference-max-items", type=int, default=0, help="Smoke-run limit per conference spider; 0 is unlimited.")
    parser.add_argument("--dry-plan", action="store_true", help="Print the plan without network access or writing files.")
    args = parser.parse_args(argv)
    if not (1991 <= args.start_year <= args.end_year <= date.today().year):
        parser.error("Use 1991 <= start-year <= end-year <= current year")
    if min(args.max_results_per_query, args.page_size, args.source_workers) < 1:
        parser.error("Result count, page size and workers must be positive")
    if min(args.max_per_direction, args.query_limit, args.max_downloads, args.retries, args.delay, args.conference_max_items) < 0 or args.timeout <= 0 or args.conference_timeout <= 0:
        parser.error("Limits, retries and delay must be nonnegative; timeouts must be positive")
    sources = [part.strip().lower() for part in args.sources.split(",") if part.strip()]
    if not sources or set(sources) - {"arxiv", "openalex", "crossref"}:
        parser.error("sources must be a nonempty subset of openalex,crossref,arxiv")
    args.sources = list(dict.fromkeys(sources))
    return args


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def paper_from_record(item: dict) -> Paper:
    fields = {key: item[key] for key in Paper.__dataclass_fields__ if key in item}
    fields.update(title=item.get("title") or "", source=item.get("source") or "local", year=int(item["year"]) if item.get("year") else None)
    fields["metadata"] = {**(item.get("metadata") or {})}
    for field in ("directions", "classification", "relevance_score"):
        if field in item:
            fields["metadata"][field] = item[field]
    return Paper(**fields)


def export_record(paper: Paper, downloads: dict | None = None) -> dict:
    record = {**paper.as_dict(), "directions": paper.metadata.get("directions", []),
              "classification": paper.metadata.get("classification", {}), "relevance_score": paper.metadata.get("relevance_score", 0)}
    if downloads is not None and paper.key in downloads:
        record["download"] = downloads[paper.key]
    return record


def save_catalog(directory: Path, papers: list[Paper], downloads: dict | None = None):
    from autopapercrawl.exports import export_csv, export_bibtex
    records = [export_record(paper, downloads) for paper in papers]
    save_json(directory / "papers.json", records)
    save_json(directory / "papers_table.json", records)
    for record in records:
        record["venues"] = [record.get("venue")] if record.get("venue") else record.get("metadata", {}).get("venues", [])
        result = record.get("download") or {}
        record["download_status"] = result.get("status")
        record["local_path"] = result.get("path")
        record["status_label"] = result.get("status")
    (directory / "papers.csv").write_bytes(export_csv(records))
    (directory / "papers.bib").write_bytes(export_bibtex(records))


def link_pdf(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and ensure_pdf_bytes(destination):
        return
    # Prefer a hard link on Windows so multi-label papers occupy disk once.
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(ROOT / ".env")
    args = parse_args(argv)
    topics = load_topics(args.config)
    selected = list(topics) if args.directions == "all" else list(dict.fromkeys(part.strip() for part in args.directions.split(",") if part.strip()))
    if not selected or set(selected) - set(topics):
        raise SystemExit(f"Choose directions from: {','.join(topics)}")
    queries = [(key, query) for key in selected for query in topics[key]["queries"][:args.query_limit or None]]
    plan = {"years": [args.start_year, args.end_year], "directions": {key: topics[key]["label"] for key in selected},
            "queries": [{"direction": key, "query": query} for key, query in queries],
            "sources": args.sources, "sort_by": "newest", "download_pdfs": not args.metadata_only,
            "output_dir": str(args.output_dir), "download_dir": str(args.download_dir),
            "conference_supplement": args.with_papercrawler, "input_json": [str(path) for path in args.input_json]}
    if args.dry_plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    raw = args.output_dir / "raw" / "main"
    previous_path = raw / "papers.json"
    if args.existing_policy == "fail" and args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"Output already exists: {args.output_dir}")
    previous = read_json(previous_path, []) if args.existing_policy == "append" else []
    papers = [paper_from_record(item) for item in previous]
    save_json(args.output_dir / "plan.json", plan)
    diagnostics = []
    client = HttpClient(delay_seconds=args.delay, timeout_seconds=args.timeout, retries=args.retries)
    if args.input_json:
        for path in args.input_json:
            loaded = read_json(path, None)
            if not isinstance(loaded, list):
                raise ValueError(f"Expected a list of papers in {path}")
            papers.extend(paper_from_record(item) for item in loaded)
    else:
        for index, (key, query) in enumerate(queries, 1):
            print(f"[{index}/{len(queries)}] {topics[key]['label']}: {query}", flush=True)
            found = search_all(query, max_results=args.max_results_per_query, start_year=args.start_year, end_year=args.end_year,
                               client=client, email=args.email, sources=args.sources, page_size=args.page_size,
                               source_workers=args.source_workers, sort_by="newest", diagnostics=diagnostics, truncate_results=False)
            for paper in found:
                paper.metadata["queries"] = list(dict.fromkeys([*paper.metadata.get("queries", []), query]))
            papers.extend(found)
            save_json(raw / "sources" / f"{index:02d}_{key}.json", [paper.as_dict() for paper in found])
            save_json(args.output_dir / "search_report.json", diagnostics)

    for path in args.papercrawler_csv:
        papers.extend(load_conference_file(path))
    if args.with_papercrawler:
        try:
            conference_papers, report = crawl_conferences(args.papercrawler_path, args.conference_venues.split(","),
                list(range(args.start_year, args.end_year + 1)), raw / "conference", timeout=args.conference_timeout, max_items=args.conference_max_items)
            papers.extend(conference_papers)
            save_json(args.output_dir / "conference_report.json", report)
            if report.get("returncode", 0) or report.get("status") == "timeout":
                diagnostics.append({"source": "paperCrawler", "status": "request_failed", "count": len(conference_papers), "error": report.get("reason") or "Conference bridge failed; see conference/crawl.log"})
            for venue_report in report.get("venues", []):
                diagnostics.append({"source": "paperCrawler:" + venue_report["venue"], "status": "request_failed" if venue_report["status"] in {"empty_or_failed", "partial"} else "ok", "count": venue_report["count"], "details": venue_report})
        except (ValueError, OSError) as exc:
            save_json(args.output_dir / "conference_report.json", {"status": "failed", "reason": str(exc)})
            diagnostics.append({"source": "paperCrawler", "status": "request_failed", "error": str(exc), "count": 0})

    candidates = deduplicate(papers)
    save_json(raw / "candidates.json", [paper.as_dict() for paper in candidates])
    accepted, rejected = [], []
    for paper in candidates:
        published = str(paper.metadata.get("published") or "")[:10]
        if not in_year_range(paper.year, args.start_year, args.end_year) or (published and published > publication_cutoff(args.end_year)):
            rejected.append({**paper.as_dict(), "reason": "outside_year_range_or_unknown_year"})
            continue
        labels = classify_paper(paper, topics)
        if set(labels) & set(selected):
            accepted.append(paper)
        else:
            rejected.append({**paper.as_dict(), "reason": "no_topic_match_in_title_or_abstract"})
    by_direction = {key: [paper for paper in accepted if key in paper.metadata["directions"]][:args.max_per_direction or None] for key in selected}
    keep_keys = {paper.key for group in by_direction.values() for paper in group}
    over_limit = [paper for paper in accepted if paper.key not in keep_keys]
    accepted = [paper for paper in accepted if paper.key in keep_keys]
    save_json(raw / "excluded_by_limit.json", [export_record(paper) for paper in over_limit])
    save_json(raw / "rejected_papers.json", rejected)
    save_json(args.output_dir / "search_report.json", diagnostics)
    # Persist catalogs before starting downloads, so an interrupted run resumes.
    save_catalog(raw, accepted)
    for key, group in by_direction.items():
        directory = args.output_dir / key / "raw" / "main"
        save_catalog(directory, group)
        (directory / "query.txt").write_text("\n".join(topics[key]["queries"]) + "\n", encoding="utf-8")

    downloads = {}
    if not args.metadata_only:
        report_path = args.download_dir / "download_report.json"
        downloads = {item["key"]: item for item in read_json(report_path, []) if item.get("key")} if args.existing_policy == "append" else {}
        for index, paper in enumerate(accepted[:args.max_downloads or None], 1):
            print(f"[PDF {index}] {paper.title}", flush=True)
            result = download_public_pdf(paper_to_target(paper), args.download_dir / "pdfs", client,
                                         email=args.email, lookup=not args.no_oa_lookup,
                                         retries=args.retries, sleep_seconds=args.delay)
            entry = {"key": paper.key, "directions": paper.metadata["directions"], **result.as_dict()}
            downloads[paper.key] = entry
            print(f"  {result.status}", flush=True)
            if result.status in {"downloaded", "exists"} and result.path:
                for key in selected:
                    if paper in by_direction[key]:
                        link_pdf(Path(result.path), args.download_dir / key / "pdfs" / Path(result.path).name)
            save_json(report_path, list(downloads.values()))
        current_downloads = {paper.key: downloads.get(paper.key, {"key": paper.key, "title": paper.title, "directions": paper.metadata["directions"], "status": "pending", "outcome": "not_attempted"}) for paper in accepted}
        downloads = current_downloads
        save_json(report_path, list(downloads.values()))
        save_json(args.download_dir / "failed_downloads.json", [entry for entry in downloads.values() if entry.get("outcome") not in {"success", "not_attempted"}])
        save_catalog(raw, accepted, downloads)
        for key, group in by_direction.items():
            save_catalog(args.output_dir / key / "raw" / "main", group, downloads)
            save_json(args.download_dir / key / "download_report.json", [downloads[paper.key] for paper in group])

    summary = {"years": [args.start_year, args.end_year], "candidates": len(candidates), "accepted_unique": len(accepted),
               "rejected": len(rejected), "excluded_by_limit": len(over_limit), "directions": {key: {"label": topics[key]["label"], "papers": len(group)} for key, group in by_direction.items()},
               "search_failures": sum(item["status"] == "request_failed" for item in diagnostics),
               "downloads": dict(Counter(entry["outcome"] for entry in downloads.values())),
               "metadata_only": args.metadata_only}
    save_json(args.output_dir / "summary.json", summary)
    report = ["# 网络安全论文采集报告", "", f"年份：{args.start_year}—{args.end_year}",
              f"候选 {len(candidates)} 篇；相关且在年份范围内、保留 {len(accepted)} 篇（跨方向去重）。", "",
              "分类依据：标题和摘要关键词规则；一篇论文可以属于多个方向。缺少摘要可能造成漏检。", "",
              *[f"- {topics[key]['label']}：{len(group)} 篇" for key, group in by_direction.items()], "",
              f"检索请求失败：{summary['search_failures']} 次，详见 search_report.json。", "",
              "PDF 状态：success=已下载或本地已有，no_fulltext=本次未找到公开全文路径，request_failed=请求失败，invalid_pdf=返回内容不是 PDF，not_attempted=下载上限内未处理。", "",
              "未找到公开全文不等于论文没有全文。规则分类和数据源覆盖不保证穷尽该方向论文。", ""]
    (args.output_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Catalogs: {args.output_dir.resolve()}")
    if not args.metadata_only:
        print(f"PDFs: {args.download_dir.resolve()}")
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
