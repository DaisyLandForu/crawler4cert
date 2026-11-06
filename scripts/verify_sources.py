"""Opt-in live acceptance run; reports real failures, never mocks support."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autopapercrawl.workbench import Workbench

CASES = [
    ("security", "conference", "sp", "satellite"),
    ("security", "journal", "cose", "firmware"),
    ("security", "preprint", None, "prompt injection"),
    ("ai_ml", "conference", "icml", "learning"),
    ("ai_ml", "journal", "jmlr", "learning"),
    ("ai_ml", "preprint", None, "reinforcement learning"),
    ("computer_vision", "conference", "cvpr", "segmentation"),
    ("computer_vision", "journal", "ijcv", "segmentation"),
    ("computer_vision", "preprint", None, "image segmentation"),
]


def wait(service, job):
    while job["status"] in {"queued", "running"}:
        time.sleep(0.2)
        job = service.get_job(job["id"])
    return job


def run(output, *, limit=3, pdf_sample=1):
    output.mkdir(parents=True, exist_ok=True)
    service = Workbench(output, import_existing=False)
    summary = []
    try:
        for domain, kind, venue, query in CASES:
            options = {"name": domain + "_" + kind, "directions": [], "venues": [venue] if venue else [], "query": query,
                       "start_year": 2024, "end_year": 2026, "mode": "quick", "max_results": limit * 3, "per_query": limit,
                       "sources": ["arxiv"] if kind == "preprint" else ["crossref", "openalex", "dblp"],
                       "enrich_metadata": False, "timeout": 12, "retries": 0, "delay": 1}
            print("LIVE", options["name"], flush=True)
            job = wait(service, service.start_search(options))
            records = service.records(job["paper_ids"])
            sample = [record["id"] for record in records if record.get("pdf_url")][:pdf_sample]
            downloads = wait(service, service.start_download(sample, str(output / "downloads"), lookup=False)) if sample else {"results": []}
            results = downloads["results"]
            row = {"domain": domain, "kind": kind, "venue": venue, "query": query, "job_id": job["id"], "status": job["status"],
                   "sources": options["sources"], "recalled": job.get("candidate_count", 0), "duplicates": job.get("duplicate_count", 0),
                   "retained": len(records), "venue_matched": sum(any(match["status"] in {"confirmed", "probable"} for match in record.get("metadata", {}).get("venue_matches", {}).values()) for record in records),
                   "abstract_count": sum(bool(record.get("abstract")) for record in records),
                   "pdf_attempted": len(results), "pdf_success": sum(result.get("outcome") == "success" for result in results),
                   "failures": [diagnostic for diagnostic in job["diagnostics"] if diagnostic["status"] in {"request_failed", "unsupported"}],
                   "pdf_failures": [{"status": result.get("status"), "reason": result.get("reason")} for result in results if result.get("outcome") != "success"],
                   "source_stats": job.get("source_stats", {})}
            row["abstract_rate"] = round(row["abstract_count"] / len(records), 3) if records else None
            row["pdf_success_rate"] = round(row["pdf_success"] / len(results), 3) if results else None
            summary.append(row)
            (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({key: row[key] for key in ("domain", "kind", "recalled", "retained", "venue_matched", "abstract_count", "pdf_attempted", "pdf_success")}), flush=True)
    finally:
        service.close()
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / ("live_acceptance_" + datetime.now().strftime("%Y%m%d_%H%M%S")))
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--pdf-sample", type=int, default=1)
    args = parser.parse_args()
    if args.limit < 1 or args.pdf_sample < 0:
        parser.error("limit must be positive; pdf-sample nonnegative")
    run(args.output.resolve(), limit=args.limit, pdf_sample=args.pdf_sample)
