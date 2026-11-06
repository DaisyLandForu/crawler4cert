"""Local-only HTTP interface. No external service or frontend build needed."""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .workbench import ROOT, Workbench, export_csv, export_html
from .venues import VENUES
from .adapters import ADAPTERS, capability_matrix
from .exports import export_json, export_bibtex

ASSETS = Path(__file__).resolve().parent / "web_assets"


def choose_directory(initial):
    code = """import sys,json,tkinter as tk
from tkinter import filedialog
root=tk.Tk();root.withdraw();root.attributes('-topmost',True)
value=filedialog.askdirectory(initialdir=sys.argv[1],title='选择 PDF 保存文件夹',mustexist=False)
root.destroy();print(json.dumps({'path':value},ensure_ascii=True))
"""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    result = subprocess.run([sys.executable, "-c", code, str(initial)], capture_output=True, text=True, timeout=180, **options)
    if result.returncode:
        raise ValueError("系统文件夹选择器不可用，请在输入框填写保存路径")
    return json.loads(result.stdout)["path"]


def handler_class(service, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, content_type="application/json; charset=utf-8", status=200, filename=None):
            if not isinstance(body, bytes):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            if filename:
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
            self.end_headers()
            self.wfile.write(body)

        def json(self, value, status=200):
            self.send(json.dumps(value, ensure_ascii=False), status=status)

        def trusted(self, write=False):
            port = self.server.server_address[1]
            if self.headers.get("Host", "").lower() not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self.json({"error": "仅允许本机访问"}, 403)
                return False
            if write:
                origin = self.headers.get("Origin")
                if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                    self.json({"error": "请求来源不匹配"}, 403)
                    return False
                if not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), token):
                    self.json({"error": "请刷新工作台页面后重试"}, 403)
                    return False
            return True

        def do_GET(self):
            if not self.trusted():
                return
            try:
                url = urlparse(self.path)
                params = {key: values[-1] for key, values in parse_qs(url.query).items()}
                if url.path in {"/", "/app.js", "/style.css"}:
                    name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[url.path]
                    mime = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[name]
                    self.send((ASSETS / name).read_bytes(), mime)
                elif url.path == "/api/config":
                    self.json({"instance": {"project_root": str(service.root), "interface": "configurable-registry"},
                               "defaults": service.settings(), "topics": {key: item["label"] for key, item in service.topics.items()},
                               "venues": [{"id": key, "label": item.get("abbreviation") or item["name"], **item} for key, item in service.registry.list("venues", enabled_only=True).items()],
                               "sources": {key: adapter(None).get_capabilities() for key, adapter in ADAPTERS.items()}, "token": token})
                elif url.path == "/api/registry":
                    self.json({"domains": service.registry.list("domains"), "venues": service.registry.list("venues")})
                elif url.path == "/api/capabilities":
                    self.json({"venues": capability_matrix(service.registry), "states": service.source_states()})
                elif url.path == "/api/papers":
                    offset = max(0, int(params.pop("offset", 0)))
                    limit = max(1, min(200, int(params.pop("limit", 50))))
                    self.json(service.list_papers(params, offset=offset, limit=limit))
                elif url.path == "/api/jobs":
                    self.json(service.jobs())
                elif url.path.startswith("/api/jobs/"):
                    job = service.get_job(url.path.rsplit("/", 1)[-1])
                    self.json(job if job else {"error": "任务不存在"}, 200 if job else 404)
                elif url.path.startswith("/api/pdf/"):
                    rows = service.records([url.path.rsplit("/", 1)[-1]])
                    record = service.public_record(rows[0]) if rows else None
                    if not record or not record["file_available"]:
                        self.json({"error": "PDF 不存在，请重新下载"}, 404)
                    else:
                        self.send(Path(record["local_path"]).read_bytes(), "application/pdf")
                elif url.path == "/api/export":
                    fmt = params.pop("format", "csv")
                    rows = service.list_papers(params, limit=0)["items"]
                    if params.get("ids"):
                        selected = set(params["ids"].split(","))
                        rows = [row for row in rows if row["id"] in selected]
                    if fmt == "html":
                        self.send(export_html(rows), "text/html; charset=utf-8", filename="论文下载报告.html")
                    elif fmt == "json":
                        self.send(export_json(rows), filename="papers.json")
                    elif fmt == "bib":
                        self.send(export_bibtex(rows), "application/x-bibtex; charset=utf-8", filename="papers.bib")
                    else:
                        self.send(export_csv(rows), "text/csv; charset=utf-8", filename="论文目录与下载结果.csv")
                else:
                    self.json({"error": "页面不存在"}, 404)
            except (ValueError, OSError, KeyError) as exc:
                self.json({"error": str(exc)}, 400)

        def do_POST(self):
            if not self.trusted(write=True):
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 2_000_000:
                    raise ValueError("请求大小无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求格式无效")
                path = urlparse(self.path).path
                if path == "/api/search":
                    self.json(service.start_search(data), 202)
                elif path == "/api/plan":
                    self.json(service.preview_plan(data))
                elif path == "/api/registry/save":
                    record = service.registry.save(data["kind"], data["record"])
                    service.topics = service.registry.topics()
                    self.json(record)
                elif path == "/api/registry/delete":
                    service.registry.delete(data["kind"], data["id"])
                    service.topics = service.registry.topics()
                    self.json({"ok": True})
                elif path == "/api/library/delete":
                    self.json(service.delete_records(data.get("ids") or []))
                elif path == "/api/library/reclassify":
                    self.json({"updated": service.reclassify(data.get("ids") or [])})
                elif path == "/api/retry":
                    self.json(service.retry_job(data["id"]), 202)
                elif path == "/api/health":
                    source = data["source"]
                    if source not in ADAPTERS:
                        raise ValueError("来源未实现")
                    from .http import HttpClient
                    result = ADAPTERS[source](HttpClient(timeout_seconds=12, retries=0), service.registry).health_check()
                    service.registry.verification(source, result=result)
                    self.json(result)
                elif path == "/api/download":
                    self.json(service.start_download(data.get("ids") or [], data.get("directory"), email=data.get("email"), lookup=data.get("lookup", True)), 202)
                elif path == "/api/import":
                    self.json({"imported": service.import_existing()})
                elif path == "/api/choose-directory":
                    self.json({"path": choose_directory(data.get("initial") or service.settings()["download_dir"])})
                elif path.startswith("/api/cancel/"):
                    service.cancel_job(path.rsplit("/", 1)[-1])
                    self.json({"ok": True})
                else:
                    self.json({"error": "接口不存在"}, 404)
            except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
                self.json({"error": str(exc)}, 400)

    return Handler


def serve(port=8765, *, root=ROOT, open_browser=True):
    token = secrets.token_urlsafe(32)
    # Bind before recovering persisted jobs so a second launch cannot interfere
    # with the instance already using the default port.
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    service = Workbench(root, recover_jobs=True)
    server.RequestHandlerClass = handler_class(service, token)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"crawler4cert 论文工作台：{url}\n项目目录：{service.root}\n保持此进程运行，按 Ctrl+C 关闭。", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for job in service.jobs():
            if job["status"] in {"running", "queued"}:
                service.cancel_job(job["id"])
        service.close()
