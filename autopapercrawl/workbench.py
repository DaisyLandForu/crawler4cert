"""Persistent local library and reusable search/download operations."""
from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .downloaders.common import ensure_pdf_bytes, safe_filename
from .http import HttpClient
from .models import Paper
from .registry import Registry
from .database import migrate, index_record, sql_filter, backup_database
from .identity import same_identity, identifiers, relate_versions
from .adapters import ADAPTERS
from .planning import build_plan
from .relevance import query_match
from .exports import export_csv as full_csv, export_json, export_bibtex
from .pdf_validation import inspect_pdf
from .open_access import download_public_pdf
from .search import deduplicate, in_year_range, merge_papers, publication_cutoff, search_all
from .targets import paper_to_target
from .topics import classify_paper, load_topics, normalized_text
from .venues import canonical, paper_venues, venue_matches
from scripts.llm_utils import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
STATUS_LABELS = {"success": "已下载", "pending": "未下载", "not_attempted": "未下载", "no_fulltext": "未找到公开全文", "request_failed": "请求失败", "invalid_pdf": "文件无效"}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def paper_from_dict(record):
    return Paper(**{key: record[key] for key in Paper.__dataclass_fields__ if key in record})


def result_outcome(result):
    return result.get("outcome") or ("success" if result.get("status") in {"downloaded", "exists"} else "pending")


def readable_reason(result):
    status = result.get("status", "")
    reason = str(result.get("reason") or "")
    if status == "exists":
        return "本地已有有效 PDF，已复用"
    if result_outcome(result) == "success":
        if result.get("archive_warnings"):
            return "PDF 已保存；部分方向归档失败，可在详情查看原因"
        return "已保存 PDF" if result.get("provider") != "local_copy" else "已将已有 PDF 复制到所选目录"
    if result.get("provider") == "local":
        return "本地文件处理失败，请检查保存路径、剩余空间和写入权限"
    if "cloudflare" in status or "403" in reason:
        return "站点访问校验或权限限制；可查看论文原网页或通过机构访问获取"
    if "404" in reason:
        return "PDF 地址已失效（404），可重试查找其他公开版本"
    if "timeout" in reason.lower() or "timed out" in reason.lower():
        return "请求超时，可检查网络后重试"
    if result_outcome(result) == "no_fulltext":
        return "本次未找到公开 PDF 地址；不表示论文没有全文"
    if result_outcome(result) == "invalid_pdf":
        return "返回内容不是完整 PDF，未作为成功文件保存"
    if result_outcome(result) == "request_failed":
        return "网络或地址查询失败，可查看尝试详情后重试"
    return "尚未尝试下载"


class Workbench:
    def __init__(self, root=ROOT, *, import_existing=True, recover_jobs=False):
        self.root = Path(root).resolve()
        self.data_dir = self.root / "data" / "workbench"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.data_dir / "library.sqlite3"
        load_dotenv(self.root / ".env")
        config = self.root / "config" / "workbench.json"
        self.defaults = json.loads(config.read_text(encoding="utf-8")) if config.exists() else json.loads((ROOT / "config" / "workbench.json").read_text(encoding="utf-8"))
        self.defaults["email"] = os.getenv("PAPER_EMAIL")
        self.registry = Registry(self.root)
        self.topics = self.registry.topics()
        self.migration_backup = migrate(self.database)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="papers")
        self.job_lock = threading.Lock()
        self.library_lock = threading.RLock()
        with self.db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, paper_key TEXT UNIQUE, title_key TEXT, doi TEXT, document TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS papers_title ON papers(title_key)")
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            empty = conn.execute("SELECT count(*) FROM papers").fetchone()[0] == 0
            if recover_jobs:
                for row in conn.execute("SELECT id, document FROM jobs").fetchall():
                    job = json.loads(row[1])
                    if job["status"] in {"running", "queued"}:
                        job.update(status="interrupted", message="上次运行已中断，可重新查询或重试下载")
                        conn.execute("UPDATE jobs SET document=?,status=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job["status"], row[0]))
        if import_existing and empty:
            self.import_existing()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.database, timeout=30)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def settings(self):
        with self.db() as conn:
            custom = {key: json.loads(value) for key, value in conn.execute("SELECT key,value FROM settings")}
        return {**self.defaults, "start_year": date.today().year - 2, "end_year": date.today().year,
                "directions": [key for key in self.defaults.get("default_domains", list(load_topics())) if key in self.topics], "download_dir": str(self.root / self.defaults["download_dir"]), **custom}

    def set_download_dir(self, directory):
        with self.db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("download_dir", json.dumps(str(directory))))

    def store(self, papers):
        with self.library_lock:
            ids = []
            with self.db() as conn:
                for paper in papers:
                    title_key = normalized_text(paper.title)
                    row = None
                    for value in identifiers(paper):
                        row = conn.execute("SELECT p.id,p.document FROM paper_identifiers i JOIN papers p ON p.id=i.paper_id WHERE i.identifier=?", (value,)).fetchone()
                        if row and same_identity(paper, paper_from_dict(json.loads(row[1]))):
                            break
                        row = None
                    if not row:
                        candidates = conn.execute("SELECT id,document FROM papers WHERE title_key=?", (title_key,)).fetchall()
                        row = next((value for value in candidates if same_identity(paper, paper_from_dict(json.loads(value[1])))), None)
                    old = json.loads(row[1]) if row else {}
                    if old:
                        paper = merge_papers(paper, paper_from_dict(old))
                    identifier = row[0] if row else hashlib.sha256(paper.key.encode()).hexdigest()[:24]
                    record = {**old, **paper.as_dict(), "id": identifier, "paper_id": identifier, "key": paper.key, "directions": paper.metadata.get("directions", old.get("directions", [])), "venues": paper_venues(paper)}
                    conn.execute("INSERT OR REPLACE INTO papers (id,paper_key,title_key,doi,document) VALUES (?,?,?,?,?)", (identifier, paper.key, title_key, paper.doi, json.dumps(record, ensure_ascii=False)))
                    index_record(conn, identifier, record)
                    # Link independently published versions across separate runs.
                    related = conn.execute("SELECT id,document FROM papers WHERE title_key=? AND id<>?", (title_key, identifier)).fetchall()
                    for other_id, other_doc in related:
                        other = json.loads(other_doc)
                        other_paper = paper_from_dict(other)
                        relate_versions([paper, other_paper])
                        if other_paper.metadata.get("related_versions"):
                            other["metadata"] = other_paper.metadata
                            conn.execute("UPDATE papers SET document=? WHERE id=?", (json.dumps(other, ensure_ascii=False), other_id))
                    if paper.metadata.get("related_versions"):
                        record["metadata"] = paper.metadata
                        conn.execute("UPDATE papers SET document=? WHERE id=?", (json.dumps(record, ensure_ascii=False), identifier))
                    ids.append(identifier)
            return list(dict.fromkeys(ids))

    def update_download(self, identifier, result):
        with self.library_lock:
            with self.db() as conn:
                row = conn.execute("SELECT document FROM papers WHERE id=?", (identifier,)).fetchone()
                if row:
                    record = json.loads(row[0])
                    old_path = (record.get("download") or {}).get("path")
                    if result_outcome(result) != "success" and old_path and ensure_pdf_bytes(Path(old_path)):
                        result["previous_path"] = old_path
                    record["download"] = result
                    record["local_pdf"] = result.get("path") or result.get("previous_path")
                    record["download_status"] = result_outcome(result)
                    conn.execute("UPDATE papers SET document=? WHERE id=?", (json.dumps(record, ensure_ascii=False), identifier))
                    index_record(conn, identifier, record)

    def import_existing(self):
        catalog = self.root / "outputs" / "security_research" / "raw" / "main" / "papers.json"
        if not catalog.exists():
            return 0
        records = json.loads(catalog.read_text(encoding="utf-8-sig"))
        papers = []
        for record in records:
            paper = paper_from_dict(record)
            if record.get("directions"):
                paper.metadata["directions"] = record["directions"]
            papers.append(paper)
        ids = self.store(papers)
        report = self.root / "downloads" / "security_research" / "download_report.json"
        results = json.loads(report.read_text(encoding="utf-8-sig")) if report.exists() else []
        by_key = {result.get("key"): result for result in results if result.get("key")}
        with self.db() as conn:
            for identifier in ids:
                row = conn.execute("SELECT document FROM papers WHERE id=?", (identifier,)).fetchone()
                record = json.loads(row[0])
                # Import old reports only if they do not replace a newer UI run.
                legacy_key = "title:" + " ".join(record["title"].lower().strip().split())
                match = by_key.get(record["key"]) or by_key.get(legacy_key)
                if match and not record.get("download"):
                    record["download"] = match
                    conn.execute("UPDATE papers SET document=? WHERE id=?", (json.dumps(record, ensure_ascii=False), identifier))
                    index_record(conn, identifier, record)
        return len(ids)

    def records(self, ids=None):
        with self.db() as conn:
            if ids is None:
                rows = conn.execute("SELECT document FROM papers ORDER BY year DESC,citations DESC,title_key").fetchall()
            else:
                ids = list(ids)
                rows = []
                for index in range(0, len(ids), 800):
                    batch = ids[index:index + 800]
                    rows.extend(conn.execute("SELECT document FROM papers WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch).fetchall())
        return [json.loads(row[0]) for row in rows]

    def public_record(self, record):
        result = record.get("download") or {}
        outcome = result_outcome(result)
        path = result.get("path") or result.get("previous_path")
        available = bool(path and Path(path).is_file())
        if outcome == "success" and not available:
            outcome = "request_failed"
            description = "报告显示成功，但本地文件已移动或丢失，可重新下载"
        else:
            description = readable_reason(result)
        return {**record, "outcome": outcome, "status_label": STATUS_LABELS.get(outcome, "未下载"),
                "status_detail": description, "file_available": available, "local_path": path,
                "direction_labels": [self.topics[key]["label"] for key in record.get("directions", []) if key in self.topics]}

    def list_papers(self, filters=None, *, offset=0, limit=50):
        filters = dict(filters or {})
        scope = filters.get("job_id")
        job = self.get_job(scope) if scope else None
        if scope and job is None:
            return {"total": 0, "items": [], "all_ids": [], "counts": {key: 0 for key in STATUS_LABELS}}
        requested_status = filters.pop("status", None)
        with self.db() as conn:
            where, params = sql_filter(conn, filters, self.registry, job)
            conn.create_function("local_file_exists", 1, lambda path: int(bool(path and Path(path).is_file())))
            effective = "CASE WHEN outcome='success' AND NOT local_file_exists(coalesce(json_extract(document,'$.download.path'),json_extract(document,'$.download.previous_path'))) THEN 'request_failed' ELSE outcome END"
            if requested_status:
                where += (" AND " if where else " WHERE ") + effective + "=?"
                params.append(requested_status)
            sort = {"citations": "citations DESC,year DESC,id", "title": "title_key ASC,id"}.get(filters.get("sort"), "year DESC,coalesce(json_extract(document,'$.metadata.published'),'') DESC,citations DESC,id")
            projected = conn.execute("SELECT id," + effective + " FROM papers" + where + " ORDER BY " + sort, params).fetchall()
            pagination = " LIMIT ? OFFSET ?" if limit else ""
            rows = conn.execute("SELECT document FROM papers" + where + " ORDER BY " + sort + pagination, [*params, limit, max(0, offset)] if limit else params).fetchall()
        public = [self.public_record(json.loads(row[0])) for row in rows]
        counts = {key: sum(outcome == key for _, outcome in projected) for key in STATUS_LABELS}
        return {"total": len(projected), "items": public, "all_ids": [identifier for identifier, _ in projected], "counts": counts}

    def search_options(self, supplied):
        options = {**self.settings(), **supplied}
        if "sources" not in supplied and supplied.get("directions"):
            requested = supplied["directions"]
            requested = requested.split(",") if isinstance(requested, str) else requested
            defaults = list(dict.fromkeys(source for key in requested if key in self.topics for source in self.topics[key].get("default_sources", [])))
            if defaults:
                options["sources"] = defaults
        for field in ("start_year", "end_year", "max_results", "per_query", "query_limit", "page_size", "retries"):
            options[field] = int(options[field])
        if not 1991 <= options["start_year"] <= options["end_year"] <= date.today().year:
            raise ValueError("请检查年份范围（1991 年至今年）")
        if not 1 <= options["max_results"] <= 5000 or not 1 <= options["per_query"] <= 500 or not 1 <= options["page_size"] <= 100:
            raise ValueError("结果上限需为 1–5000，每次召回 1–500，每页 1–100")
        if not 0 <= options["query_limit"] <= 100 or not 0 <= options["retries"] <= 3:
            raise ValueError("检索词数量或重试次数超出范围")
        options["timeout"] = float(options["timeout"])
        options["delay"] = float(options["delay"])
        if not 1 <= options["timeout"] <= 180 or not 0 <= options["delay"] <= 60:
            raise ValueError("超时应为 1–180 秒，请求间隔应为 0–60 秒")
        for field in ("directions", "sources", "venues"):
            value = options.get(field) or []
            options[field] = list(dict.fromkeys([part.strip() for part in value.split(",") if part.strip()] if isinstance(value, str) else list(value)))
        if set(options["directions"]) - set(self.topics):
            raise ValueError("研究方向无效")
        if not options["sources"] or set(options["sources"]) - set(ADAPTERS):
            raise ValueError("请选择有效的数据来源")
        for field in ("query", "title"):
            options[field] = str(options.get(field) or "").strip()[:1000]
        if not (options["directions"] or options["query"] or options["title"] or options["venues"]):
            raise ValueError("请选择方向，或填写关键词、标题、会议期刊")
        options["enrich_metadata"] = bool(options.get("enrich_metadata", True))
        options["enrichment_limit"] = max(0, min(100, int(options.get("enrichment_limit", 10))))
        options["mode"] = str(options.get("mode") or self.defaults.get("mode", "standard"))
        options["overlap_days"] = int(options.get("overlap_days", 7))
        if not 0 <= options["overlap_days"] <= 90:
            raise ValueError("重叠窗口应为 0–90 天")
        return options

    def new_job(self, kind, options):
        identifier = uuid.uuid4().hex
        job = {"id": identifier, "kind": kind, "status": "queued", "created": now(), "done": 0, "total": 0,
               "name": options.get("name") or ("论文检索" if kind == "search" else "PDF 下载"), "message": "等待处理", "options": options, "paper_ids": [], "diagnostics": [], "results": []}
        if kind == "search":
            job["relevance_filter_version"] = "title_abstract_terms_v1"
            job["plan"] = self.preview_plan(options)
            entries = job["plan"]["entries"]
            job["search_conditions"] = {
                "directions": [{"id": key, "label": self.topics[key]["label"]} for key in options["directions"]],
                "venues": [{"id": value, "label": (self.registry.venue(value) or {}).get("name", value)} for value in options["venues"]],
                "title": options["title"], "query": options["query"],
                "start_year": options["start_year"], "end_year": options["end_year"],
                "queries": list(dict.fromkeys(entry["query"] for entry in entries)),
                "sources": list(dict.fromkeys(source for entry in entries for source in entry["sources"])),
                "mode": options["mode"], "max_results": options["max_results"],
                "download_pdfs": bool(options.get("download_pdfs")),
                "download_dir": str(options.get("download_dir") or self.settings()["download_dir"]),
            }
        self.save_job(job)
        self.executor.submit(self.run_job, job)
        return self.get_job(identifier)

    def save_job(self, job):
        with self.db() as conn:
            conn.execute("INSERT OR REPLACE INTO jobs (id,document,status,created) VALUES (?,?,?,?)", (job["id"], json.dumps(job, ensure_ascii=False), job["status"], job["created"]))

    def get_job(self, identifier):
        with self.db() as conn:
            row = conn.execute("SELECT document FROM jobs WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def jobs(self):
        with self.db() as conn:
            rows = conn.execute("SELECT document FROM jobs ORDER BY rowid DESC LIMIT 20").fetchall()
        return [json.loads(row[0]) for row in rows]

    def cancel_job(self, identifier):
        with self.job_lock:
            job = self.get_job(identifier)
            if not job:
                raise ValueError("任务不存在")
            job["cancel_requested"] = True
            self.save_job(job)

    def progress(self, job, **changes):
        with self.job_lock:
            stored = self.get_job(job["id"])
            job.update(changes)
            if stored and stored.get("cancel_requested"):
                job["cancel_requested"] = True
            self.save_job(job)
            return not job.get("cancel_requested")

    def run_job(self, job):
        try:
            if not self.progress(job, status="running", started=now(), message="正在开始"):
                self.progress(job, status="cancelled", finished=now())
                return
            if job["kind"] == "search":
                self.run_search(job)
                if job["options"].get("download_pdfs") and job["paper_ids"] and not job.get("cancel_requested"):
                    self.run_search_download(job)
            else:
                self.run_download(job)
            job.update(status="cancelled" if job.get("cancel_requested") else "completed", finished=now())
            if job["kind"] == "search":
                self.write_search_report(job)
            self.progress(job)
        except Exception as exc:
            job.update(status="failed", error=str(exc), message=str(exc), finished=now())
            if job["kind"] == "search":
                try:
                    self.write_search_report(job)
                except OSError as report_error:
                    job["report_error"] = str(report_error)
            self.progress(job)

    def start_search(self, options):
        return self.new_job("search", self.search_options(options))

    def run_search(self, job):
        options = job["options"]
        plan = job["plan"]
        entries = plan["entries"]
        client = HttpClient(delay_seconds=options["delay"], timeout_seconds=options["timeout"], retries=options["retries"])
        candidates = []
        enriched = set()
        enrichment_success = 0
        for index, entry in enumerate(entries):
            query, venue = entry["query"], entry["venue"]
            if not self.progress(job, total=len(entries), done=index, message=f"检索 {query or '全部论文'}" + (f" · {venue}" if venue else "")):
                break
            sources = entry["sources"]
            found = search_all(query, max_results=options["per_query"], start_year=options["start_year"], end_year=options["end_year"],
                client=client, email=options.get("email") or None, sources=sources, page_size=options["page_size"], sort_by="newest",
                diagnostics=job["diagnostics"], truncate_results=False, venue=venue, title_filter=options["title"] or None, registry=self.registry, from_date=plan["since"])
            raw_dir = self.root / "outputs" / "tasks" / job["id"]
            raw_dir.mkdir(parents=True, exist_ok=True)
            with (raw_dir / "raw_records.jsonl").open("a", encoding="utf-8") as handle:
                for paper in found:
                    handle.write(json.dumps(paper.as_dict(), ensure_ascii=False) + "\n")
            for found_index, paper in enumerate(found):
                paper.metadata.setdefault("queries", []).append(query)
                if options.get("enrich_metadata", True) and paper.doi and not paper.abstract and paper.doi not in enriched and len(enriched) < int(options.get("enrichment_limit", 20)):
                    from .search import resolve_doi
                    enriched.add(paper.doi)
                    enriched_paper = resolve_doi(paper.doi, client, options.get("email"))
                    if enriched_paper and enriched_paper.doi == paper.doi:
                        paper = merge_papers(paper, enriched_paper)
                        found[found_index] = paper
                        enrichment_success += 1
            candidates.extend(found)
            raw_dir = self.root / "outputs" / "tasks" / job["id"]
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "candidates.json").write_bytes(export_json([paper.as_dict() for paper in candidates]))
            accepted, pending_venue, unrelated, relevance_audit = [], 0, 0, []
            for paper in deduplicate(candidates):
                if not in_year_range(paper.year, options["start_year"], options["end_year"]):
                    continue
                published = str(paper.metadata.get("published") or "")[:10]
                if published and published > publication_cutoff(options["end_year"]):
                    continue
                classify_paper(paper, self.topics)
                checks = [query_match(paper, query) for query in paper.metadata.get("queries", plan["queries"])]
                paper.metadata["query_relevance"] = checks
                relevance_audit.append({"key": paper.key, "title": paper.title, "checks": checks})
                if not any(check["matched"] for check in checks):
                    unrelated += 1
                    continue
                if options["title"] and normalized_text(options["title"]) not in normalized_text(paper.title):
                    continue
                if options["venues"]:
                    from .venues import match_paper_venue
                    matches = [match_paper_venue(paper, wanted, self.registry) for wanted in options["venues"]]
                    if not any(match["status"] in {"confirmed", "probable"} for match in matches):
                        if paper.arxiv_id or paper.metadata.get("arxiv_id"):
                            paper.metadata["venue_review_required"] = True
                            pending_venue += 1
                        else:
                            continue
                accepted.append(paper)
            (raw_dir / "relevance_checks.json").write_bytes(export_json(relevance_audit))
            # Raw candidates remain archived; only text-verified results enter
            # the selected/downloadable result set.
            retained = accepted[:options["max_results"]]
            for paper in accepted:
                levels = [entry["confidence"] for key, entry in paper.metadata["classification"].items() if not options["directions"] or key in options["directions"]]
                paper.metadata["review_confidence"] = "high" if "high" in levels else "medium" if "medium" in levels else "low"
            job["paper_ids"] = self.store(retained)
            job["candidate_count"] = len(candidates)
            job["deduplicated_count"] = len(deduplicate(candidates))
            job["duplicate_count"] = len(candidates) - job["deduplicated_count"]
            job["excluded_by_limit"] = max(0, len(accepted) - len(retained))
            job["venue_review_count"] = pending_venue
            job["excluded_by_relevance"] = unrelated
            job["classification_counts"] = {level: sum(any(value["confidence"] == level for value in paper.metadata["classification"].values()) for paper in retained) for level in ("high", "medium", "low")}
            self.progress(job, done=index + 1, message=f"已召回 {len(candidates)} 条，入库 {len(job['paper_ids'])} 篇")
        job["enrichment_stats"] = {"doi_attempted": len(enriched), "doi_resolved": enrichment_success, "http_requests": client.request_count, "successful_requests": client.success_count, "failed_requests": client.failure_count}
        failures = sum(item.get("status") in {"request_failed", "unsupported"} for item in job["diagnostics"])
        source_stats = {}
        for item in job["diagnostics"]:
            stats = source_stats.setdefault(item.get("source", "unknown"), {"requests": 0, "returned": 0, "successes": 0, "failures": 0, "duplicates": 0, "retained": 0})
            stats["requests"] += item.get("requests", 0)
            stats["returned"] += item.get("count", 0)
            stats["successes"] += int(item.get("status") in {"ok", "empty"})
            stats["failures"] += int(item.get("status") in {"request_failed", "unsupported"})
        for source, stats in source_stats.items():
            source_papers = [paper for paper in candidates if paper.source.lower() == source.lower()]
            stats["duplicates"] = len(source_papers) - len(deduplicate(source_papers))
            stats["retained"] = sum(source.lower() in {value.lower() for value in record.get("sources", [])} for record in self.records(job["paper_ids"]))
        self.record_source_states(job)
        self.progress(job, source_stats=source_stats, warnings=failures,
                      warnings_detail=[f"{item.get('source')}: {item.get('error') or item.get('reason')}" for item in job["diagnostics"] if item.get("status") in {"request_failed", "unsupported"}],
                      message=f"查询完成，保留 {len(job['paper_ids'])} 篇" + (f"；{failures} 次来源请求失败或不支持，请查看详情" if failures else ""))

    def preview_plan(self, supplied):
        options = self.search_options(supplied)
        plan = build_plan(options, self.topics)
        with self.db() as conn:
            checkpoint = conn.execute("SELECT last_success FROM checkpoints WHERE plan_key=?", (plan["plan_key"],)).fetchone()
        return build_plan(options, self.topics, checkpoint=checkpoint[0] if checkpoint else None)

    def source_states(self):
        with self.db() as conn:
            return [dict(zip(("source", "venue", "last_success", "job_id"), row)) for row in conn.execute("SELECT source,venue,last_success,job_id FROM source_states")]

    def record_source_states(self, job):
        grouped = {}
        for diagnostic in job["diagnostics"]:
            source = diagnostic.get("source")
            if source:
                grouped.setdefault((source, diagnostic.get("venue") or ""), []).append(diagnostic)
        for (source, venue), results in grouped.items():
            successful = all(item.get("status") in {"ok", "empty"} for item in results)
            matched = [record for record in self.records(job["paper_ids"]) if (not venue or record.get("metadata", {}).get("venue_matches", {}).get(venue, {}).get("status") in {"confirmed", "probable"}) and source.lower() in {value.lower() for value in record.get("sources", [])}]
            status = "verified" if matched and successful else "queryable" if successful else "unavailable"
            if successful and not job.get("cancel_requested"):
                with self.db() as conn:
                    conn.execute("INSERT OR REPLACE INTO source_states VALUES (?,?,?,?,?)", (source, venue, now(), job["id"], json.dumps(results, ensure_ascii=False)))
            self.registry.verification(source, venue, {"status": status, "checked_at": now(), "count": len(matched), "reason": next((item.get("error") for item in results if item.get("error")), None), "job_id": job["id"]})
        # A cursor is scoped to the whole normalized plan. Never advance on a
        # failed, cancelled or truncated run: doing so could silently lose papers.
        complete = len(job["diagnostics"]) == job["plan"]["source_calls"] and all(item.get("status") in {"ok", "empty"} and item.get("count", 0) < job["options"]["per_query"] for item in job["diagnostics"])
        if complete and not job.get("cancel_requested") and not job.get("excluded_by_limit"):
            with self.db() as conn:
                conn.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)", (job["plan"]["plan_key"], job["started"], job["id"]))

    def write_search_report(self, job):
        directory = self.root / "outputs" / "tasks" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        records = [self.public_record(record) for record in self.records(job["paper_ids"])]
        (directory / "papers.json").write_bytes(export_json(records))
        (directory / "papers.csv").write_bytes(export_csv(records))
        (directory / "papers.bib").write_bytes(export_bibtex(records))
        (directory / "report.html").write_text(export_html(records), encoding="utf-8")
        job["report_dir"] = str(directory)
        (directory / "task.json").write_bytes(export_json(job))

    def run_search_download(self, job):
        original = job["options"]
        directory = Path(original.get("download_dir") or self.settings()["download_dir"])
        if not directory.is_absolute():
            directory = self.root / directory
        download_options = {"ids": list(job["paper_ids"]), "directory": str(directory), "email": original.get("email"), "lookup": True}
        self.progress(job, phase="download")
        self.run_download(job, download_options=download_options)

    def retry_job(self, identifier):
        job = self.get_job(identifier)
        if not job or job["status"] in {"queued", "running"}:
            raise ValueError("任务不存在或尚未结束")
        options = dict(job["options"])
        if job["kind"] == "search":
            entries = [{"query": diagnostic.get("query", ""), "venue": diagnostic.get("venue"), "sources": [diagnostic["source"]]} for diagnostic in job.get("diagnostics", []) if diagnostic.get("status") == "request_failed"]
            if entries:
                options["retry_plan"] = entries
            options["retry_of"] = identifier
            return self.start_search(options)
        failed = [row["id"] for row in job.get("results", []) if result_outcome(row) != "success"]
        failed += [value for value in options["ids"] if value not in {row["id"] for row in job.get("results", [])}]
        if not failed:
            raise ValueError("该任务没有失败或未完成的 PDF")
        return self.start_download(failed, options["directory"], email=options.get("email"), lookup=options.get("lookup", True))

    def reclassify(self, ids):
        with self.library_lock:
            self.topics = self.registry.topics()
            records = self.records(ids)
            papers = [paper_from_dict(record) for record in records]
            for paper in papers:
                classify_paper(paper, self.topics)
            self.store(papers)
            return len(papers)

    def delete_records(self, ids):
        with self.library_lock:
            records = self.records(ids)
            if not records:
                raise ValueError("请选择有效记录")
            backup = backup_database(self.database)
            with self.db() as conn:
                for record in records:
                    conn.execute("INSERT INTO deleted_papers VALUES (?,?,?)", (record["id"], json.dumps(record, ensure_ascii=False), now()))
                    conn.execute("DELETE FROM papers WHERE id=?", (record["id"],))
                    conn.execute("DELETE FROM paper_identifiers WHERE paper_id=?", (record["id"],))
                    try:
                        conn.execute("DELETE FROM papers_fts WHERE id=?", (record["id"],))
                    except sqlite3.OperationalError:
                        pass
            return {"deleted": len(records), "backup": backup, "pdf_files_kept": True}

    def start_download(self, ids, directory=None, *, email=None, lookup=True):
        ids = list(dict.fromkeys(ids))
        records = self.records(ids)
        if not ids or len(records) != len(ids):
            raise ValueError("请选择论文；选择中包含不存在的记录时请刷新页面")
        path = Path(directory or self.settings()["download_dir"]).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError("下载路径不是文件夹")
        self.set_download_dir(path)
        return self.new_job("download", {"ids": ids, "directory": str(path), "email": email or self.defaults.get("email"), "lookup": bool(lookup)})

    def run_download(self, job, *, download_options=None):
        options = download_options or job["options"]
        directory = Path(options["directory"])
        pdf_dir = directory / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        records = self.records(options["ids"])
        job["paper_ids"] = [record["id"] for record in records]
        client = HttpClient(delay_seconds=self.defaults["delay"], timeout_seconds=self.defaults["timeout"], retries=self.defaults["retries"])
        self.write_reports(job, directory)
        for index, record in enumerate(records):
            if not self.progress(job, total=len(records), done=index, message=f"正在处理：{record['title']}"):
                break
            paper = paper_from_dict(record)
            target = paper_to_target(paper)
            destination = pdf_dir / safe_filename(target)
            old_path = (record.get("download") or {}).get("path") or (record.get("download") or {}).get("previous_path")
            try:
                if old_path and Path(old_path).resolve() != destination.resolve() and ensure_pdf_bytes(Path(old_path)) and not ensure_pdf_bytes(destination):
                    temporary = destination.with_suffix(".pdf.part")
                    shutil.copy2(old_path, temporary)
                    temporary.replace(destination)
                    result = {"status": "downloaded", "outcome": "success", "provider": "local_copy", "path": str(destination), "bytes": destination.stat().st_size}
                else:
                    result = download_public_pdf(target, pdf_dir, client, email=options.get("email"), lookup=options["lookup"], retries=client.retries, sleep_seconds=client.delay_seconds, max_bytes=int(self.defaults.get("max_pdf_mb", 100)) * 1024 * 1024).as_dict()
                if result_outcome(result) == "success":
                    result.update(inspect_pdf(result["path"]))
                    for direction in record.get("directions", []):
                        if direction not in self.topics:
                            continue
                        link = directory / direction / "pdfs" / Path(result["path"]).name
                        try:
                            link.parent.mkdir(parents=True, exist_ok=True)
                            if not link.exists():
                                try:
                                    os.link(result["path"], link)
                                except OSError:
                                    shutil.copy2(result["path"], link)
                        except OSError as exc:
                            result.setdefault("archive_warnings", []).append({"direction": direction, "reason": str(exc)})
            except (OSError, ValueError) as exc:
                result = {"status": "failed", "outcome": "request_failed", "reason": str(exc), "provider": "local"}
            self.update_download(record["id"], result)
            job["results"].append({**record, **result})
            self.write_reports(job, directory)
            self.progress(job, done=index + 1, message=f"已处理 {index + 1}/{len(records)} 篇")
        successful = sum(result_outcome(item) == "success" for item in job["results"])
        job["pdf_success"] = successful
        job["pdf_failed"] = len(job["results"]) - successful
        self.progress(job, message=f"处理完成：成功 {successful}，未成功 {len(job['results']) - successful}")

    def write_reports(self, job, directory):
        report_dir = directory / "reports" / job["id"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "download_report.json").write_text(json.dumps(job["results"], ensure_ascii=False, indent=2), encoding="utf-8")
        rows = []
        for result in job["results"]:
            rows.append({**result, "status_label": STATUS_LABELS.get(result_outcome(result), "未下载"), "status_detail": readable_reason(result),
                         "direction_labels": [self.topics[key]["label"] for key in result["directions"] if key in self.topics], "local_path": result.get("path") or result.get("previous_path")})
        (report_dir / "results.csv").write_bytes(export_csv(rows))
        (report_dir / "report.html").write_text(export_html(rows), encoding="utf-8")
        job["report_dir"] = str(report_dir)


def export_csv(records):
    return full_csv(records)


def export_html(records):
    counts = {label: sum(record.get("status_label") == label for record in records) for label in set(record.get("status_label", "未下载") for record in records)}
    lines = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>论文下载报告</title><style>body{font:15px system-ui;margin:36px;color:#172b38}table{border-collapse:collapse;width:100%}th,td{border:1px solid #dbe3e7;padding:12px;text-align:left;vertical-align:top}th{background:#f1f6f7}small{color:#596c78}h1{font-size:26px}td:first-child{min-width:250px}</style><h1>论文下载报告</h1>',
             '<p>' + html.escape(f"生成时间：{now()} · 共 {len(records)} 篇 · " + " / ".join(f"{label} {count}" for label, count in counts.items())) + '</p>',
             '<p>未找到公开全文不代表论文没有全文；请求失败可在工作台重试。</p><table><thead><tr><th>论文</th><th>方向 / 出处</th><th>下载结果</th><th>本地文件</th></tr></thead><tbody>']
    for record in records:
        path = record.get("local_path")
        link = '<a href="' + html.escape(Path(path).resolve().as_uri(), quote=True) + '">打开 PDF</a><br><small>' + html.escape(path) + '</small>' if path else '—'
        lines.append('<tr><td>' + html.escape(record["title"]) + '<br><small>' + html.escape(str(record.get("year") or "年份未知")) + '</small></td><td>' + html.escape("；".join(record.get("direction_labels", []))) + '<br>' + html.escape("；".join(record.get("venues", [])) or "出处未记录") + '</td><td>' + html.escape(record.get("status_label", "未下载")) + '<br><small>' + html.escape(record.get("status_detail") or "") + '</small></td><td>' + link + '</td></tr>')
    return "\n".join([*lines, '</tbody></table></html>'])
