import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import subprocess

from autopapercrawl.downloaders.common import DownloadResult, PaperTarget, ensure_pdf_bytes, safe_filename
from autopapercrawl.http import HttpClient
from autopapercrawl.open_access import PDFLinks, download_public_pdf, resolve_arxiv_title_urls
from autopapercrawl.models import Paper
from scripts.curate_project import curate_with_llm
from scripts.security_pipeline import main
from autopapercrawl.conferences import crawl_conferences


PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


class Handler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        self.hits.append(self.path)
        body = PDF if self.path == "/paper.pdf" else b"<html>Not a PDF</html>"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf" if self.path == "/paper.pdf" else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_fallback_after_bad_pdf_and_resume_without_requests(self):
        target = PaperTarget(title="Test paper", pdf_url=self.base + "/bad", metadata={"pdf_urls": [self.base + "/paper.pdf"], "citations": -1})
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = download_public_pdf(target, directory, HttpClient(timeout_seconds=2), lookup=False, retries=0)
            self.assertEqual(result.status, "downloaded")
            self.assertEqual([entry["status"] for entry in result.attempts], ["failed_not_pdf", "downloaded"])
            self.assertTrue(ensure_pdf_bytes(Path(result.path)))
            with patch("autopapercrawl.open_access.resolve_public_urls", side_effect=AssertionError("must reuse local PDF")):
                result = download_public_pdf(PaperTarget(title="Test paper"), directory, HttpClient())
            self.assertEqual(result.status, "exists")

    def test_html_is_not_saved_as_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = download_public_pdf(PaperTarget(title="Bad", pdf_url=self.base + "/html"), Path(tmp), HttpClient(timeout_seconds=2), lookup=False, retries=0)
            self.assertEqual(result.outcome, "invalid_pdf")
            self.assertFalse(list(Path(tmp).iterdir()))

    def test_missing_fulltext_and_failed_lookup_are_distinct(self):
        target = PaperTarget(title="No URL")
        with tempfile.TemporaryDirectory() as tmp:
            result = download_public_pdf(target, Path(tmp), HttpClient(), lookup=False)
            self.assertEqual(result.outcome, "no_fulltext")
            def fail_lookup(target, client, email, attempts):
                attempts.append({"stage": "test", "status": "request_failed", "reason": "timeout"})
                return []
            with patch("autopapercrawl.open_access.resolve_public_urls", side_effect=fail_lookup), patch("autopapercrawl.open_access.resolve_arxiv_title_urls", return_value=[]):
                result = download_public_pdf(target, Path(tmp), HttpClient())
            self.assertEqual(result.outcome, "request_failed")

    def test_arxiv_title_fallback_after_metadata_pdf_failure(self):
        target = PaperTarget(title="Fallback paper", pdf_url=self.base + "/bad")
        with tempfile.TemporaryDirectory() as tmp, patch("autopapercrawl.open_access.resolve_public_urls", return_value=[]), patch("autopapercrawl.open_access.resolve_arxiv_title_urls", return_value=[self.base + "/paper.pdf"]):
            result = download_public_pdf(target, Path(tmp), HttpClient(timeout_seconds=2), retries=0)
        self.assertEqual(result.status, "downloaded")

    def test_unrelated_arxiv_title_is_never_downloaded(self):
        target = PaperTarget(title="Exact requested cybersecurity paper")
        with patch("autopapercrawl.open_access.search_arxiv", return_value=[Paper("Completely different research", "arXiv", pdf_url="https://arxiv.org/pdf/wrong")]):
            self.assertEqual(resolve_arxiv_title_urls(target, HttpClient(), []), [])

    def test_public_page_pdf_metadata_and_slides(self):
        parser = PDFLinks()
        parser.feed('<meta name="citation_pdf_url" content="/paper.pdf"><a href="/slides.pdf">Download slides PDF</a><a href="/actual.pdf">Full paper PDF</a>')
        self.assertEqual(parser.citation_urls, ["/paper.pdf"])
        self.assertEqual(parser.urls, ["/actual.pdf"])

    def test_truncated_pdf_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "truncated.pdf"
            path.write_bytes(b"%PDF-1.7\ntruncated")
            self.assertFalse(ensure_pdf_bytes(path))

    def test_url_only_targets_have_distinct_filenames(self):
        self.assertNotEqual(safe_filename(PaperTarget(url="https://example.org/a.pdf")), safe_filename(PaperTarget(url="https://example.org/b.pdf")))

    def test_unicode_and_truncated_titles_have_distinct_filenames(self):
        self.assertNotEqual(safe_filename(PaperTarget(title="论文甲")), safe_filename(PaperTarget(title="论文乙")))
        prefix = "Long paper title " * 30
        self.assertNotEqual(safe_filename(PaperTarget(title=prefix + "one")), safe_filename(PaperTarget(title=prefix + "two")))

    def test_publisher_placeholder_does_not_hide_public_request_failure(self):
        from autopapercrawl.downloaders.router import DownloadOptions, download_target
        failure = DownloadResult(status="failed", provider="open_access", reason="timeout")
        with tempfile.TemporaryDirectory() as tmp, patch("autopapercrawl.open_access.download_public_pdf", return_value=failure):
            result = download_target(PaperTarget(title="Elsevier paper", doi="10.1016/example"), ["openalex", "sciencedirect"], Path(tmp), DownloadOptions())
        self.assertEqual(result.outcome, "request_failed")

    def test_offline_pipeline_classifies_downloads_and_resumes(self):
        records = [
            {"title": "Satellite network security: detecting malicious encrypted traffic", "year": 2026, "pdf_url": self.base + "/paper.pdf"},
            {"title": "Detecting backdoor vulnerabilities in neural models", "year": 2025, "pdf_url": self.base + "/paper.pdf"},
            {"title": "Old satellite network security", "year": 2020},
            {"title": "Satellite image classification", "year": 2026},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.json"
            source.write_text(json.dumps(records), encoding="utf-8")
            args = ["--input-json", str(source), "--output-dir", str(root / "out"), "--download-dir", str(root / "downloads"),
                    "--start-year", "2024", "--end-year", "2026", "--no-oa-lookup", "--retries", "0", "--timeout", "2", "--delay", "0"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            summary = json.loads((root / "out" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["accepted_unique"], 2)
            self.assertEqual(summary["rejected"], 2)
            self.assertEqual(summary["downloads"], {"success": 2})
            for direction in ("satellite_security", "encrypted_traffic", "vulnerability_detection", "ai_security"):
                self.assertEqual(len(list((root / "downloads" / direction / "pdfs").glob("*.pdf"))), 1)
                self.assertTrue((root / "out" / direction / "raw" / "main" / "papers.csv").exists())
            hits = len(Handler.hits)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            self.assertEqual(len(Handler.hits), hits)
            report = json.loads((root / "downloads" / "download_report.json").read_text(encoding="utf-8"))
            self.assertTrue(all(item["status"] == "exists" for item in report))

    def test_llm_curation_can_select_preprint_without_doi(self):
        args = SimpleNamespace(top_n=1, language="zh", api_key_env="unused", base_url="unused", model="unused")
        with patch("scripts.curate_project.chat_json", return_value={"selected_ids": ["0"], "report_md": "report"}):
            selected, report = curate_with_llm(args, "AI security", [{"title": "New jailbreak", "year": 2026}])
        self.assertEqual(selected[0]["title"], "New jailbreak")

    def test_conference_bridge_combines_separate_spider_feeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "paperCrawler" / "crawl_conf"
            package = project / "crawl_conf"
            package.mkdir(parents=True)
            (project / "scrapy.cfg").write_text("[settings]\ndefault = crawl_conf.settings\n", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "settings.py").write_text("SPIDER_MODULES = ['crawl_conf.spiders']\nROBOTSTXT_OBEY = False\n", encoding="utf-8")
            spiders = f'''import scrapy
class One(scrapy.Spider):
    name = "one"
    start_urls = ["{self.base}/one"]
    def parse(self, response):
        yield {{"conf": "ONE2026", "title": "First paper", "abstract": "Full abstract", "pdf_url": "{self.base}/paper.pdf"}}
class Two(scrapy.Spider):
    name = "two"
    start_urls = ["{self.base}/two"]
    def parse(self, response):
        yield {{"conf": "TWO2026", "title": "Second paper", "abstract": "Full abstract", "pdf_url": "{self.base}/paper.pdf"}}
'''
            (package / "spiders.py").write_text(spiders, encoding="utf-8")
            papers, report = crawl_conferences(root / "paperCrawler", ["one", "two"], [2026], root / "bridge", timeout=30)
            self.assertEqual({paper.title for paper in papers}, {"First paper", "Second paper"})
            self.assertEqual(report["returncode"], 0)
            self.assertTrue(all(paper.abstract == "Full abstract" for paper in papers))

    def test_conference_timeout_recovers_complete_jsonl_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout" / "crawl_conf"
            checkout.mkdir(parents=True)
            (checkout / "scrapy.cfg").write_text("", encoding="utf-8")
            def timeout_run(command, **kwargs):
                output = Path(command[command.index("--output") + 1])
                feed = output.parent / "cvpr.jsonl"
                feed.write_text(json.dumps({"conf": "CVPR2024", "title": "Recovered"}) + '\n{"incomplete":', encoding="utf-8")
                raise subprocess.TimeoutExpired(command, 1)
            with patch("autopapercrawl.conferences.subprocess.run", side_effect=timeout_run):
                papers, report = crawl_conferences(checkout.parent, ["cvpr"], [2024], root / "out", timeout=1)
            self.assertEqual([paper.title for paper in papers], ["Recovered"])
            self.assertEqual(report["status"], "timeout")


if __name__ == "__main__":
    unittest.main()
