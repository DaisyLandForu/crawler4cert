import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autopapercrawl.models import Paper
from autopapercrawl.relevance import normalize_query, query_match
from autopapercrawl.workbench import Workbench
from test_workbench import finished


class QueryRelevanceTests(unittest.TestCase):
    def test_mixed_chinese_query_and_protocol_boundaries(self):
        self.assertEqual(normalize_query("QUIC 恶意流量"), "QUIC malicious traffic")
        self.assertTrue(query_match(Paper("基于QUIC的恶意流量检测", "test"), "QUIC 恶意流量")["matched"])
        self.assertTrue(query_match(Paper("QUIC network flow analysis", "test", abstract="Identifies malware attacks."), "QUIC 恶意流量")["matched"])
        for title in ("HPV vaccination data analysis", "AI film ethics", "Quicker malicious traffic analysis", "QUIC video traffic classification"):
            self.assertFalse(query_match(Paper(title, "test"), "QUIC 恶意流量")["matched"], title)
        self.assertEqual(normalize_query("尚未支持的中文主题"), "尚未支持的中文主题")

    def test_unrelated_recall_is_archived_but_never_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Workbench(Path(directory), import_existing=False)
            try:
                papers = [Paper("QUIC malicious traffic detection", "Crossref", year=2025),
                          Paper("HPV vaccination data analysis", "Crossref", year=2026),
                          Paper("AI film ethics", "Crossref", year=2026)]
                with patch("autopapercrawl.workbench.search_all", return_value=papers) as search, patch.object(service, "run_download") as download:
                    job = finished(service, service.start_search({"query": "QUIC 恶意流量", "directions": ["encrypted_traffic"], "sources": ["crossref"], "enrich_metadata": False, "download_pdfs": True}))
                self.assertEqual(search.call_args.args[0], "QUIC malicious traffic")
                self.assertEqual(job["candidate_count"], 3)
                self.assertEqual(job["excluded_by_relevance"], 2)
                self.assertEqual([row["title"] for row in service.records(job["paper_ids"])], ["QUIC malicious traffic detection"])
                self.assertEqual(download.call_args.kwargs["download_options"]["ids"], job["paper_ids"])
                self.assertEqual(job["options"]["query"], "QUIC 恶意流量")
                self.assertEqual(job["search_conditions"]["query"], "QUIC 恶意流量")
                self.assertEqual(len(json.loads((Path(job["report_dir"]) / "candidates.json").read_text(encoding="utf-8"))), 3)
            finally:
                service.close()

    def test_original_conditions_are_persisted_through_download_even_without_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Workbench(Path(directory), import_existing=False)
            try:
                with patch.object(service.executor, "submit"):
                    job = service.start_search({"query": "QUIC 恶意流量", "directions": ["encrypted_traffic"], "sources": ["crossref"]})
                original = copy.deepcopy(job["options"])
                job.pop("search_conditions")  # Exercise the legacy-job path.
                def inspect_download(current, **kwargs):
                    service.progress(current, total=1, done=0)
                    self.assertEqual(service.get_job(current["id"])["options"], original)
                    self.assertEqual(kwargs["download_options"]["ids"], [])
                    raise RuntimeError("Simulated interrupted download")
                with patch.object(service, "run_download", side_effect=inspect_download):
                    with self.assertRaises(RuntimeError):
                        service.run_search_download(job)
                self.assertEqual(service.get_job(job["id"])["options"], original)
            finally:
                service.close()
