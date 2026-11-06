"""One reusable CLI for the workbench's search/library/download operations."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import json
from .workbench import Workbench, export_csv, export_html
from .exports import export_json, export_bibtex


def wait(service, job):
    last = None
    while job["status"] in {"queued", "running"}:
        if job["message"] != last:
            print(job["message"], flush=True)
            last = job["message"]
        time.sleep(0.3)
        job = service.get_job(job["id"])
    print(job["message"])
    if job["status"] == "failed":
        raise RuntimeError(job.get("error") or "任务失败")
    return job


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m autopapercrawl", description="本地论文查询与下载工作台；无参数时打开页面。")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="启动前端页面")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--no-browser", action="store_true")
    runner = commands.add_parser("run", help="执行保存的 JSON 检索条件，适用于本地定时任务")
    runner.add_argument("--config", type=Path, required=True)
    runner.add_argument("--plan-only", action="store_true")
    retry = commands.add_parser("retry", help="重试一个历史任务")
    retry.add_argument("job_id")
    commands.add_parser("backup", help="备份当前 SQLite 数据库")
    for name in ("search", "library", "download"):
        sub = commands.add_parser(name, help={"search": "联网查询", "library": "筛选本地论文库", "download": "选择下载"}[name])
        sub.add_argument("--start-year", type=int)
        sub.add_argument("--end-year", type=int)
        sub.add_argument("--direction", action="append", dest="directions", help="方向 ID，可重复；all 表示全部预设方向")
        sub.add_argument("--venue", action="append", dest="venues", help="会议/期刊名或简称，可重复")
        sub.add_argument("--title", help="标题包含的文字")
        sub.add_argument("--query", help="研究关键词")
        sub.add_argument("--status", choices=("success", "pending", "no_fulltext", "request_failed", "invalid_pdf"))
        sub.add_argument("--export", type=Path, help="导出当前结果为 .csv 或 .html")
        if name == "search":
            sub.add_argument("--mode", choices=("quick", "standard", "comprehensive"))
            sub.add_argument("--incremental", action="store_true")
            sub.add_argument("--since", help="增量起点 YYYY-MM-DD")
            sub.add_argument("--overlap-days", type=int)
            sub.add_argument("--plan-only", action="store_true")
            sub.add_argument("--name", help="任务名称")
            sub.add_argument("--per-query", type=int)
            sub.add_argument("--limit", type=int, dest="max_results")
            sub.add_argument("--sources", help="来源，逗号分隔")
            sub.add_argument("--download", action="store_true", help="下载本次查询的全部结果")
            sub.add_argument("--output-dir", help="PDF 保存目录")
        if name == "download":
            sub.add_argument("--id", action="append", dest="ids", help="本地论文 ID，可重复")
            sub.add_argument("--all", action="store_true", help="下载所有符合本地筛选条件的论文")
            sub.add_argument("--output-dir", help="PDF 保存目录")
    args = parser.parse_args(argv)
    if args.command == "serve":
        from .web import serve
        serve(args.port, open_browser=not args.no_browser)
        return
    service = Workbench()
    try:
        if args.command == "backup":
            from .database import backup_database
            print(backup_database(service.database))
            return
        if args.command == "run":
            config = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                parser.error("配置文件必须为 JSON 对象")
            if args.plan_only:
                print(json.dumps(service.preview_plan(config), ensure_ascii=False, indent=2))
                return
            job = wait(service, service.start_search(config))
            print("任务 ID：" + job["id"])
            print("报告：" + job["report_dir"])
            if job.get("warnings"):
                raise SystemExit(2)
            return
        if args.command == "retry":
            job = wait(service, service.retry_job(args.job_id))
            print("任务 ID：" + job["id"])
            if job.get("warnings"):
                raise SystemExit(2)
            return
        supplied = {key: value for key, value in vars(args).items() if value is not None and key in {"start_year", "end_year", "directions", "venues", "title", "query", "status", "max_results", "sources", "mode", "incremental", "since", "overlap_days", "name", "per_query"}}
        if "all" in supplied.get("directions", []):
            supplied["directions"] = list(service.topics)
        if args.command == "search":
            # An explicit title/keyword/venue search is unrestricted by the four
            # presets unless --direction is also supplied.
            if not args.directions and (args.title or args.query or args.venues):
                supplied["directions"] = []
            if args.plan_only:
                print(json.dumps(service.preview_plan(supplied), ensure_ascii=False, indent=2))
                return
            job = wait(service, service.start_search(supplied))
            filters = {"job_id": job["id"]}
            ids = job["paper_ids"]
            if args.download and ids:
                wait(service, service.start_download(ids, args.output_dir))
        else:
            filters = supplied
            ids = args.ids if args.command == "download" and args.ids else service.list_papers(filters, limit=0)["all_ids"]
            if args.command == "download":
                if not args.ids and not args.all:
                    parser.error("下载需要 --id，或明确使用 --all 下载全部筛选结果")
                wait(service, service.start_download(ids, args.output_dir))
        rows = [service.public_record(record) for record in service.records(ids)] if args.command == "download" else service.list_papers(filters, limit=0)["items"]
        print(f"共 {len(rows)} 篇，已下载 {sum(row['outcome'] == 'success' for row in rows)} 篇")
        print("论文 ID                    年份   下载状态         标题")
        for row in rows:
            print(f"{row['id']}  {row.get('year') or '未知'}  {row['status_label']:<10} {row['title'][:110]}")
        if args.export:
            args.export.parent.mkdir(parents=True, exist_ok=True)
            suffix = args.export.suffix.lower()
            args.export.write_bytes(export_html(rows).encode("utf-8") if suffix == ".html" else export_json(rows) if suffix == ".json" else export_bibtex(rows) if suffix == ".bib" else export_csv(rows))
            print(f"已导出：{args.export.resolve()}")
    finally:
        service.close()
