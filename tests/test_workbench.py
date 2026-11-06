import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import requests

from autopapercrawl.models import Paper
from autopapercrawl.search import SOURCE_IDS, CROSSREF_ISSNS, paper_from_crossref, paper_from_openalex, search_crossref, search_openalex
from autopapercrawl.venues import venue_matches
from autopapercrawl.web import handler_class
from autopapercrawl.workbench import Workbench, export_csv, export_html

from io import BytesIO
from pypdf import PdfWriter
_pdf_buffer = BytesIO()
_pdf_writer = PdfWriter()
_pdf_writer.add_blank_page(width=100, height=100)
_pdf_writer.write(_pdf_buffer)
PDF = _pdf_buffer.getvalue()


def finished(service, job):
    deadline = time.monotonic() + 10
    while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.01)
        job = service.get_job(job["id"])
    if job["status"] != "completed":
        raise AssertionError(job)
    return job


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Workbench(self.root, import_existing=False)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def store(self, title="Encrypted traffic classification", year=2026, venue="Computer Networks", direction="encrypted_traffic"):
        paper = Paper(title, "test", year=year, metadata={"venue": venue, "directions": [direction]})
        return self.service.store([paper])[0]

    def test_combined_local_filters(self):
        wanted = self.store()
        self.store("Firmware vulnerability detection", venue="IEEE Transactions on Information Forensics and Security", direction="vulnerability_detection")
        self.store("Encrypted traffic classification old", year=2020)
        rows = self.service.list_papers({"start_year": 2024, "end_year": 2026, "title": "traffic", "venues": ["comnet"], "directions": ["encrypted_traffic"]})
        self.assertEqual(rows["all_ids"], [wanted])
        self.assertTrue(venue_matches("2025 ACM SIGSAC Conference on Computer and Communications Security", "ccs"))
        self.assertFalse(venue_matches("CCS unrelated", "comnet"))

    def test_download_selected_to_custom_folder_and_create_readable_reports(self):
        selected = self.store()
        other = self.store("Another encrypted traffic classification")
        source = self.root / "old.pdf"
        source.write_bytes(PDF)
        self.service.update_download(selected, {"status": "downloaded", "outcome": "success", "path": str(source)})
        output = self.root / "用户选择目录"
        job = finished(self.service, self.service.start_download([selected], str(output)))
        self.assertEqual(len(job["results"]), 1)
        self.assertEqual(job["results"][0]["provider"], "local_copy")
        self.assertEqual(len(list((output / "pdfs").glob("*.pdf"))), 1)
        self.assertTrue((output / "encrypted_traffic" / "pdfs").is_dir())
        self.assertIn("已下载", (Path(job["report_dir"]) / "results.csv").read_text(encoding="utf-8-sig"))
        self.assertIn("打开 PDF", (Path(job["report_dir"]) / "report.html").read_text(encoding="utf-8"))
        self.assertNotIn("download", self.service.records([other])[0])
        second = finished(self.service, self.service.start_download([selected], str(output)))
        self.assertEqual(second["results"][0]["status"], "exists")

    def test_missing_local_file_is_not_shown_as_downloaded(self):
        identifier = self.store()
        self.service.update_download(identifier, {"status": "downloaded", "outcome": "success", "path": str(self.root / "missing.pdf")})
        self.assertEqual(self.service.list_papers()["items"][0]["status_label"], "请求失败")

    def test_download_archives_each_actual_direction_including_custom_and_unclassified(self):
        custom = self.service.registry.save("domains", {"domain_name": "威胁分析", "keywords": ["threat analysis"]})
        self.service.topics = self.service.registry.topics()
        directions = ["encrypted_traffic", custom["domain_id"]]
        assigned, unclassified = self.service.store([
            Paper("Cross-domain traffic threat analysis", "test", metadata={"directions": directions}),
            Paper("Unconfirmed research", "test", metadata={"directions": []}),
        ])
        source = self.root / "cached.pdf"
        source.write_bytes(PDF)
        for identifier in (assigned, unclassified):
            self.service.update_download(identifier, {"status": "downloaded", "outcome": "success", "path": str(source)})
        output = self.root / "chosen"
        job = finished(self.service, self.service.start_download([assigned, unclassified], str(output)))
        self.assertEqual(job["pdf_success"], 2)
        self.assertEqual(len(list((output / "pdfs").glob("*.pdf"))), 2)
        expected = Path(next(row["path"] for row in job["results"] if row["id"] == assigned)).name
        for direction in directions:
            self.assertEqual([path.name for path in (output / direction / "pdfs").glob("*.pdf")], [expected])
        self.assertFalse((output / "ai_security").exists())

    def test_archive_error_does_not_hide_a_saved_pdf(self):
        identifier = self.store()
        source = self.root / "cached.pdf"
        source.write_bytes(PDF)
        self.service.update_download(identifier, {"status": "downloaded", "outcome": "success", "path": str(source)})
        output = self.root / "chosen"
        output.mkdir()
        (output / "encrypted_traffic").write_text("Blocks direction subfolder", encoding="utf-8")
        job = finished(self.service, self.service.start_download([identifier], str(output)))
        self.assertEqual(job["results"][0]["outcome"], "success")
        self.assertTrue(job["results"][0]["archive_warnings"])
        self.assertTrue(Path(job["results"][0]["path"]).is_file())

    def test_import_previous_catalog_and_report_without_overwriting_new_attempt(self):
        raw = self.root / "outputs/security_research/raw/main"
        raw.mkdir(parents=True)
        paper = Paper("Encrypted traffic classification", "test", year=2026)
        (raw / "papers.json").write_text(json.dumps([paper.as_dict()]), encoding="utf-8")
        download = self.root / "downloads/security_research"
        download.mkdir(parents=True)
        (download / "download_report.json").write_text(json.dumps([{"key": paper.key, "outcome": "no_fulltext", "status": "no_fulltext"}]), encoding="utf-8")
        self.assertEqual(self.service.import_existing(), 1)
        identifier = self.service.records()[0]["id"]
        self.service.update_download(identifier, {"outcome": "request_failed", "status": "failed", "reason": "new attempt"})
        self.service.import_existing()
        self.assertEqual(self.service.records()[0]["download"]["reason"], "new attempt")

    def test_remote_search_applies_title_venue_and_direction_before_limit(self):
        papers = [Paper("Encrypted traffic classification", "a", year=2026, metadata={"venue": "Computer Networks"}),
                  Paper("Encrypted traffic classification elsewhere", "a", year=2026, metadata={"venue": "Wrong journal"}),
                  Paper("Not matching title", "a", year=2026, metadata={"venue": "Computer Networks"})]
        with patch("autopapercrawl.workbench.search_all", return_value=papers) as search:
            job = finished(self.service, self.service.start_search({"title": "traffic", "venues": ["comnet"], "directions": ["encrypted_traffic"], "max_results": 1}))
        self.assertEqual(len(job["paper_ids"]), 1)
        self.assertEqual(search.call_args.kwargs["venue"], "comnet")
        self.assertIn("arxiv", search.call_args.kwargs["sources"])
        record = self.service.records(job["paper_ids"])[0]
        self.assertEqual(record["metadata"]["venue_matches"]["comnet"]["status"], "probable")
        self.assertEqual(self.service.list_papers({"job_id": job["id"]})["total"], 1)

    def test_source_errors_remain_visible_in_job(self):
        def failing(*args, **kwargs):
            kwargs["diagnostics"].append({"status": "request_failed", "reason": "timeout"})
            return []
        with patch("autopapercrawl.workbench.search_all", side_effect=failing):
            job = finished(self.service, self.service.start_search({"title": "test", "directions": []}))
        self.assertEqual(job["warnings"], 1)
        self.assertIn("来源请求失败", job["message"])

    def test_search_conditions_preserve_labels_and_actual_queries(self):
        record = {"domain_id": "custom_threats", "domain_name": "原始方向名称", "keywords": ["original keyword"]}
        self.service.registry.save("domains", record)
        self.service.topics = self.service.registry.topics()
        with patch.object(self.service.executor, "submit"):
            job = self.service.start_search({"directions": ["custom_threats"], "venues": ["ccs"], "mode": "comprehensive", "sources": ["crossref"]})
        self.service.registry.save("domains", {**record, "domain_name": "后来改名", "keywords": ["changed keyword"]})
        self.service.topics = self.service.registry.topics()
        snapshot = self.service.get_job(job["id"])["search_conditions"]
        self.assertEqual(snapshot["directions"], [{"id": "custom_threats", "label": "原始方向名称"}])
        self.assertEqual(snapshot["queries"], ["original keyword"])
        self.assertNotEqual(snapshot["venues"][0]["label"], "ccs")
        # During automatic PDF download, the task's working options can change.
        job["options"] = {"ids": [], "directory": "chosen"}
        self.service.save_job(job)
        self.assertEqual(self.service.get_job(job["id"])["search_conditions"], snapshot)

    def test_invalid_selection_and_years_rejected(self):
        with self.assertRaises(ValueError):
            self.service.start_download(["unknown"])
        with self.assertRaises(ValueError):
            self.service.start_search({"start_year": 2026, "end_year": 2024})

    def test_readable_exports_escape_html_and_spreadsheet_formulas(self):
        rows = [{"title": '=HYPERLINK("bad")<script>', "status_label": "未下载", "status_detail": "待处理"}]
        self.assertIn("&lt;script&gt;", export_html(rows))
        self.assertNotIn("<script>", export_html(rows))
        self.assertIn("'=HYPERLINK", export_csv(rows).decode("utf-8-sig"))

    def test_preprint_and_published_record_keep_one_library_id(self):
        identifier = self.store()
        new = Paper("Encrypted traffic classification", "other", year=2026, doi="10.1/example")
        self.assertEqual(self.service.store([new]), [identifier])
        self.assertEqual(len(self.service.records()), 1)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Workbench(Path(self.tmp.name), import_existing=False)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(self.service, "test-token"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.tmp.cleanup()

    def test_static_app_and_configuration(self):
        self.assertIn("论文工作台", requests.get(self.url, timeout=3).text)
        self.assertEqual(requests.get(self.url + "/api/config", timeout=3).json()["token"], "test-token")
        self.assertIn("text/javascript", requests.get(self.url + "/app.js", timeout=3).headers["Content-Type"])

    def test_write_requests_need_local_origin_and_token(self):
        endpoint = self.url + "/api/import"
        self.assertEqual(requests.post(endpoint, json={}, timeout=3).status_code, 403)
        headers = {"X-Workbench-Token": "test-token", "Origin": "https://unrelated.example"}
        self.assertEqual(requests.post(endpoint, headers=headers, json={}, timeout=3).status_code, 403)
        headers["Origin"] = self.url
        self.assertEqual(requests.post(endpoint, headers=headers, json={}, timeout=3).status_code, 200)

    def test_registered_pdf_and_export_are_accessible(self):
        paper = Paper("Example", "test", year=2026)
        identifier = self.service.store([paper])[0]
        path = Path(self.tmp.name) / "known.pdf"
        path.write_bytes(PDF)
        self.service.update_download(identifier, {"status": "downloaded", "outcome": "success", "path": str(path)})
        self.assertEqual(requests.get(self.url + "/api/pdf/" + identifier, timeout=3).content, PDF)
        self.assertEqual(requests.get(self.url + "/api/pdf/unregistered", timeout=3).status_code, 404)
        response = requests.get(self.url + "/api/export?format=csv", timeout=3)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))

    def post(self, endpoint, data):
        return requests.post(self.url + endpoint, headers={"X-Workbench-Token": "test-token", "Origin": self.url}, json=data, timeout=5)

    def test_registry_create_update_delete_and_validation(self):
        record = {"domain_id": "custom_physics", "domain_name": "Physics", "keywords": ["quantum measurement"]}
        self.assertEqual(self.post("/api/registry/save", {"kind": "domains", "record": record}).status_code, 200)
        self.assertIn("custom_physics", requests.get(self.url + "/api/config", timeout=3).json()["topics"])
        self.assertEqual(self.post("/api/registry/save", {"kind": "domains", "record": {**record, "keywords": []}}).status_code, 400)
        self.assertEqual(self.post("/api/registry/delete", {"kind": "domains", "id": "custom_physics"}).status_code, 200)
        self.assertNotIn("custom_physics", requests.get(self.url + "/api/config", timeout=3).json()["topics"])

    def test_plan_capabilities_and_selected_complete_exports(self):
        response = self.post("/api/plan", {"directions": ["ai_security"], "mode": "comprehensive"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["query_count"], 8)
        self.assertEqual(len(requests.get(self.url + "/api/capabilities", timeout=3).json()["venues"]), 56)
        ids = self.service.store([Paper("One", "test", year=2025, authors=["Alice"]), Paper("Two", "test", year=2025)])
        response = requests.get(self.url + "/api/export?format=json&ids=" + ids[0], timeout=3)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["authors"], ["Alice"])
        response = requests.get(self.url + "/api/export?format=bib&ids=" + ids[0], timeout=3)
        self.assertIn("@misc{", response.text)
        self.assertNotIn("Two", response.text)

    def test_bulk_delete_keeps_files_and_reclassify_survives(self):
        identifier = self.service.store([Paper("Encrypted traffic classification", "test", year=2025)])[0]
        self.assertEqual(self.post("/api/library/reclassify", {"ids": [identifier]}).json()["updated"], 1)
        self.assertEqual(self.post("/api/library/delete", {"ids": [identifier]}).json()["deleted"], 1)
        self.assertEqual(self.service.list_papers()["total"], 0)


class VenueMetadataTests(unittest.TestCase):
    def test_source_metadata_is_preserved(self):
        paper = paper_from_openalex({"title": "Test", "primary_location": {"source": {"display_name": "Computer Networks"}}})
        self.assertEqual(paper.metadata["venue"], "Computer Networks")
        paper = paper_from_crossref({"title": ["Test"], "container-title": ["Computer Networks"]})
        self.assertEqual(paper.metadata["venue"], "Computer Networks")

    def test_venue_and_title_constraints_reach_publisher_apis(self):
        from test_search_and_topics import FakeClient
        client = FakeClient([{"results": [{"id": "https://openalex.org/S123", "display_name": "Test Journal"}]}, {"results": [{"title": "Encrypted traffic", "publication_year": 2026}]}])
        SOURCE_IDS.pop("Test Journal", None)
        search_openalex("traffic", 1, 2024, 2026, client, None, venue="Test Journal", title_filter="Encrypted: traffic")
        filters = parse_qs(urlparse(client.urls[-1]).query)["filter"][0]
        self.assertIn("primary_location.source.id:S123", filters)
        self.assertIn("title.search:Encrypted  traffic", filters)
        CROSSREF_ISSNS.pop("Computer Networks", None)
        client = FakeClient([{"message": {"items": [{"title": "Computer Networks", "ISSN": ["1389-1286"]}]}}, {"message": {"items": []}}])
        search_crossref("traffic", 1, 2024, 2026, client, None, venue="comnet", title_filter="encrypted traffic")
        params = parse_qs(urlparse(client.urls[-1]).query)
        self.assertEqual(params["query.container-title"], ["Computer Networks"])
        self.assertEqual(params["query.title"], ["encrypted traffic"])
        self.assertIn("issn:1389-1286", params["filter"][0])


if __name__ == "__main__":
    unittest.main()
