from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .download import download_open_access_pdf
from .http import HttpClient
from .search import deduplicate, enrich_with_unpaywall, resolve_seeds, search_all
from .seeds import extract_seed_identifiers
from .store import init_db, upsert_paper


DEFAULT_OUTPUT_DIR = Path("data/papers")
DEFAULT_DB = Path("data/papers.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopapercrawl",
        description="Search academic metadata and download lawful open-access PDFs.",
    )
    parser.add_argument("query", nargs="?", help="Search topic, keywords, DOI, or copied content.")
    parser.add_argument("--query-file", type=Path, help="Read search content from a text file.")
    parser.add_argument(
        "--seed-file",
        action="append",
        type=Path,
        default=[],
        help="Extract DOI/arXiv seeds from a research-lookup or parallel-cli output file. Repeatable.",
    )
    parser.add_argument("--max-results", type=int, default=25, help="Maximum deduplicated papers to keep.")
    parser.add_argument("--start-year", type=int, help="Earliest publication year.")
    parser.add_argument("--end-year", type=int, help="Latest publication year.")
    parser.add_argument("--sort-by", choices=("newest", "citations", "relevance"), default="newest")
    parser.add_argument("--sources", default="openalex,crossref,arxiv", help="Comma-separated sources.")
    parser.add_argument("--email", help="Contact email for polite API usage where supported.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="PDF output directory.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path.")
    parser.add_argument("--no-db", action="store_true", help="Do not create or write a SQLite database.")
    parser.add_argument("--json-output", type=Path, help="Write metadata and download report to JSON.")
    parser.add_argument("--metadata-only", action="store_true", help="Search and store metadata without downloading PDFs.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve targets without writing PDF files.")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum seconds between HTTP requests.")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=5, help="HTTP retry attempts per request.")
    parser.add_argument("--page-size", type=int, default=10, help="API results per request; smaller is more stable.")
    parser.add_argument("--source-workers", type=int, default=3, help="Concurrent structured metadata sources to query.")
    parser.add_argument("--doi-workers", type=int, default=4, help="Concurrent DOI metadata resolve workers.")
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"serve", "search", "library", "download", "run", "retry", "backup", "--help", "-h"}:
        from .workbench_cli import main as workbench_main
        workbench_main(argv or ["serve"])
        return
    args = build_parser().parse_args(argv)
    query = load_query(args)
    sources = [source.strip().lower() for source in args.sources.split(",") if source.strip()]

    client = HttpClient(delay_seconds=args.delay, timeout_seconds=args.timeout, retries=args.retries)
    papers = []
    if query:
        papers.extend(
            search_all(
                query,
                max_results=args.max_results,
                start_year=args.start_year,
                end_year=args.end_year,
                client=client,
                email=args.email,
                sources=sources,
                page_size=max(1, args.page_size),
                source_workers=max(1, args.source_workers),
                sort_by=args.sort_by,
            )
        )

    if args.seed_file:
        dois, arxiv_ids = extract_seed_identifiers(args.seed_file)
        papers.extend(resolve_seeds(dois=dois, arxiv_ids=arxiv_ids, client=client, email=args.email, doi_workers=max(1, args.doi_workers)))
        papers = deduplicate(papers)[: args.max_results]

    papers = enrich_with_unpaywall(papers, client, args.email)

    conn = None if args.no_db else init_db(args.db)
    report = []
    for paper in papers:
        if args.metadata_only:
            result = {"status": "metadata_only", "path": None, "reason": None}
        else:
            result = download_open_access_pdf(paper, args.output_dir, client, dry_run=args.dry_run)

        if conn is not None:
            upsert_paper(
                conn,
                paper,
                download_status=result["status"],
                file_path=result["path"],
                reason=result["reason"],
            )
        report.append({**paper.as_dict(), "download": result})

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print_summary(report, None if args.no_db else args.db, args.output_dir)


def load_query(args: argparse.Namespace) -> str:
    if args.query_file:
        return args.query_file.read_text(encoding="utf-8").strip()
    if args.query:
        return args.query.strip()
    if args.seed_file:
        return ""
    raise SystemExit("Provide a query argument, --query-file, or --seed-file.")


def print_summary(report: list[dict], db_path: Path | None, output_dir: Path) -> None:
    counts: dict[str, int] = {}
    for item in report:
        status = item["download"]["status"]
        counts[status] = counts.get(status, 0) + 1

    print(f"papers: {len(report)}")
    for status, count in sorted(counts.items()):
        print(f"{status}: {count}")
    print(f"database: {db_path if db_path else 'disabled'}")
    print(f"pdf_dir: {output_dir}")
