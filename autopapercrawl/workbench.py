"""Persistent local library and reusable search/download operations."""
from __future__ import annotations

import csv
import hashlib
import html
import io
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
        self.topics = load_topics()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="papers")
        self.job_lock = threading.Lock()
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
                        conn.execute("UPDATE jobs SET document=? WHERE id=?", (json.dumps(job, ensure_ascii=False), row[0]))
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
                "directions": list(self.topics), "download_dir": str(self.root / self.defaults["download_dir"]), **custom}

    def set_download_dir(self, directory):
        with self.db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("download_dir", json.dumps(str(directory))))

    def store(self, papers):
        ids = []
        with self.db() as conn:
            for paper in papers:
                title_key = normalized_text(paper.title)
                row = conn.execute("SELECT id,document FROM papers WHERE paper_key=?", (paper.key,)).fetchone()
                if not row:
                    row = conn.execute("SELECT id,document FROM papers WHERE title_key=? AND (doi IS NULL OR ? IS NULL) LIMIT 1", (title_key, paper.doi)).fetchone()
                old = json.loads(row[1]) if row else {}
                if old:
                    paper = merge_papers(paper, paper_from_dict(old))
                identifier = row[0] if row else hashlib.sha256(paper.key.encode()).hexdigest()[:24]
                record = {**old, **paper.as_dict(), "id": identifier, "key": paper.key, "directions": paper.metadata.get("directions", old.get("directions", [])), "venues": paper_venues(paper)}
                conn.execute("INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?)", (identifier, paper.key, title_key, paper.doi, json.dumps(record, ensure_ascii=False)))
                ids.append(identifier)
        return list(dict.fromkeys(ids))

    def update_download(self, identifier, result):
        with self.db() as conn:
            row = conn.execute("SELECT document FROM papers WHERE id=?", (identifier,)).fetchone()
            if row:
                record = json.loads(row[0])
                old_path = (record.get("download") or {}).get("path")
                if result_outcome(result) != "success" and old_path and ensure_pdf_bytes(Path(old_path)):
                    result["previous_path"] = old_path
                record["download"] = result
                conn.execute("UPDATE papers SET document=? WHERE id=?", (json.dumps(record, ensure_ascii=False), identifier))

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
                if record["key"] in by_key and not record.get("download"):
                    record["download"] = by_key[record["key"]]
                    conn.execute("UPDATE papers SET document=? WHERE id=?", (json.dumps(record, ensure_ascii=False), identifier))
        return len(ids)

    def records(self, ids=None):
        with self.db() as conn:
            rows = conn.execute("SELECT document FROM papers").fetchall()
        items = [json.loads(row[0]) for row in rows]
        if ids is not None:
            wanted = set(ids)
            items = [item for item in items if item["id"] in wanted]
        return sorted(items, key=lambda item: (item.get("year") or 0, str(item.get("metadata", {}).get("published") or ""), item.get("citations") or 0, item["title"]), reverse=True)

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
        filters = filters or {}
        rows = self.records()
        scope = filters.get("job_id")
        if scope:
            job = self.get_job(scope)
            wanted = set(job.get("paper_ids", [])) if job else set()
            rows = [row for row in rows if row["id"] in wanted]
        start = int(filters.get("start_year") or 0)
        end = int(filters.get("end_year") or 9999)
        directions = filters.get("directions") or []
        if isinstance(directions, str):
            directions = [part for part in directions.split(",") if part]
        venues = filters.get("venues") or []
        if isinstance(venues, str):
            venues = [part.strip() for part in venues.split(";") if part.strip()]
        title = normalized_text(filters.get("title") or "")
        query = normalized_text(filters.get("query") or "")
        public = []
        for row in rows:
            if not start <= (row.get("year") or 0) <= end:
                continue
            if directions and not set(directions).intersection(row.get("directions", [])):
                continue
            if title and title not in normalized_text(row["title"]):
                continue
            if query and query not in normalized_text(row["title"] + " " + (row.get("abstract") or "")):
                continue
            if venues and not any(venue_matches(actual, wanted) for actual in row.get("venues", []) for wanted in venues):
                continue
            item = self.public_record(row)
            if filters.get("status") and filters["status"] != item["outcome"]:
                continue
            public.append(item)
        counts = {key: sum(item["outcome"] == key for item in public) for key in STATUS_LABELS}
        return {"total": len(public), "items": public[offset:offset + limit] if limit else public,
                "all_ids": [item["id"] for item in public], "counts": counts}

    def search_options(self, supplied):
        options = {**self.settings(), **supplied}
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
            options[field] = [part.strip() for part in value.split(",") if part.strip()] if isinstance(value, str) else list(value)
        if set(options["directions"]) - set(self.topics):
            raise ValueError("研究方向无效")
        if not options["sources"] or set(options["sources"]) - {"arxiv", "openalex", "crossref"}:
            raise ValueError("请选择有效的数据来源")
        for field in ("query", "title"):
            options[field] = str(options.get(field) or "").strip()[:1000]
        if not (options["directions"] or options["query"] or options["title"] or options["venues"]):
            raise ValueError("请选择方向，或填写关键词、标题、会议期刊")
        return options

    def new_job(self, kind, options):
        identifier = uuid.uuid4().hex
        job = {"id": identifier, "kind": kind, "status": "queued", "created": now(), "done": 0, "total": 0,
               "message": "等待处理", "options": options, "paper_ids": [], "diagnostics": [], "results": []}
        self.save_job(job)
        self.executor.submit(self.run_job, job)
        return self.get_job(identifier)

    def save_job(self, job):
        with self.db() as conn:
            conn.execute("INSERT OR REPLACE INTO jobs VALUES (?,?)", (job["id"], json.dumps(job, ensure_ascii=False)))

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
            self.progress(job, status="running", message="正在开始")
            if job["kind"] == "search":
                self.run_search(job)
            else:
                self.run_download(job)
            self.progress(job, status="cancelled" if job.get("cancel_requested") else "completed", finished=now())
        except Exception as exc:
            self.progress(job, status="failed", error=str(exc), message=str(exc), finished=now())

    def start_search(self, options):
        return self.new_job("search", self.search_options(options))

    def run_search(self, job):
        options = job["options"]
        if options["query"] or options["title"]:
            queries = [options["query"] or options["title"]]
        else:
            queries = list(dict.fromkeys(query for key in options["directions"] for query in self.topics[key]["queries"][:options["query_limit"] or None])) or [""]
        plan = [(query, venue) for query in queries for venue in options["venues"] or [None]]
        client = HttpClient(delay_seconds=options["delay"], timeout_seconds=options["timeout"], retries=options["retries"])
        candidates = []
        for index, (query, venue) in enumerate(plan):
            if not self.progress(job, total=len(plan), done=index, message=f"检索 {query or '全部论文'}" + (f" · {canonical(venue)}" if venue else "")):
                break
            sources = options["sources"]
            # arXiv records without a journal reference cannot establish a
            # conference/journal match. Publication APIs provide that evidence.
            if venue and canonical(venue).lower() != "arxiv":
                sources = [source for source in sources if source != "arxiv"]
            if not sources:
                job["diagnostics"].append({"status": "skipped", "reason": "该会议/期刊筛选需要 OpenAlex 或 Crossref 出处元数据"})
                continue
            found = search_all(query, max_results=options["per_query"], start_year=options["start_year"], end_year=options["end_year"],
                client=client, email=options.get("email") or None, sources=sources, page_size=options["page_size"], sort_by="newest",
                diagnostics=job["diagnostics"], truncate_results=False, venue=venue, title_filter=options["title"] or None)
            candidates.extend(found)
            accepted = []
            for paper in deduplicate(candidates):
                if not in_year_range(paper.year, options["start_year"], options["end_year"]):
                    continue
                if str(paper.metadata.get("published") or "")[:10] > publication_cutoff(options["end_year"]):
                    continue
                labels = classify_paper(paper, self.topics)
                if options["directions"] and not set(labels).intersection(options["directions"]):
                    continue
                if options["title"] and normalized_text(options["title"]) not in normalized_text(paper.title):
                    continue
                if options["venues"] and not any(venue_matches(actual, wanted) for actual in paper_venues(paper) for wanted in options["venues"]):
                    continue
                accepted.append(paper)
            job["paper_ids"] = self.store(accepted[:options["max_results"]])
            self.progress(job, done=index + 1, candidate_count=len(candidates), message=f"已找到 {len(job['paper_ids'])} 篇符合条件的论文")
        failures = sum(item.get("status") == "request_failed" for item in job["diagnostics"])
        warnings_detail = []
        for item in job["diagnostics"]:
            if item.get("status") == "request_failed":
                error = str(item.get("error") or item.get("reason") or "")
                cause = "访问限流（429），可稍后重试或暂时取消该来源" if "429" in error else "请求失败，请在任务详情中查看原因"
                warnings_detail.append(f"{item.get('source', '数据来源')}：{cause}")
            elif item.get("status") == "skipped":
                warnings_detail.append(item.get("reason") or "该来源暂不支持当前条件")
        self.progress(job, message=f"查询完成，保留 {len(job['paper_ids'])} 篇" + (f"；{failures} 次来源请求失败，请查看详情" if failures else ""), warnings=failures, warnings_detail=list(dict.fromkeys(warnings_detail)))

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

    def run_download(self, job):
        options = job["options"]
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
                    result = download_public_pdf(target, pdf_dir, client, email=options.get("email"), lookup=options["lookup"], retries=client.retries, sleep_seconds=client.delay_seconds).as_dict()
                if result_outcome(result) == "success":
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
            job["results"].append({"id": record["id"], "title": record["title"], "year": record.get("year"), "doi": record.get("doi"), "url": record.get("url"), "venues": record.get("venues", []), "directions": record.get("directions", []), **result})
            self.write_reports(job, directory)
            self.progress(job, done=index + 1, message=f"已处理 {index + 1}/{len(records)} 篇")
        successful = sum(result_outcome(item) == "success" for item in job["results"])
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
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["论文标题", "年份", "研究方向", "会议期刊", "下载状态", "说明", "PDF 路径", "DOI"])
    for record in records:
        values = [record["title"], record.get("year"), "；".join(record.get("direction_labels", [])), "；".join(record.get("venues", [])), record.get("status_label"), record.get("status_detail"), record.get("local_path"), record.get("doi")]
        writer.writerow(["'" + str(value) if str(value or "").startswith(("=", "+", "-", "@", "\t", "\r")) else value for value in values])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


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
