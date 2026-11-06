"""Reproducible acceptance of registries, adapters, identity and local workflows."""
import contextlib
import copy
import csv
import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from pypdf import PdfWriter
from autopapercrawl.adapters import ADAPTERS, capability_matrix, register_adapter
from autopapercrawl.adapters.base import UnsupportedCapability
from autopapercrawl.adapters.dblp import DBLPAdapter, normalize_hit
from autopapercrawl.adapters.publisher import PublisherAdapter, article_metadata
from autopapercrawl.database import migrate, connection, SCHEMA_VERSION
from autopapercrawl.exports import export_csv, export_json, export_bibtex
from autopapercrawl.http import HttpClient
from autopapercrawl.identity import relate_versions
from autopapercrawl.models import Paper
from autopapercrawl.pdf_validation import inspect_pdf, content_duplicate
from autopapercrawl.registry import Registry
from autopapercrawl.search import deduplicate, paper_from_crossref, paper_from_openalex, paper_from_arxiv_entry, search_arxiv
from autopapercrawl.topics import classify_paper
from autopapercrawl.venues import match_paper_venue
from autopapercrawl.workbench import Workbench


class Client:
    def __init__(self, replies):
        self.replies, self.urls = list(replies), []
        self.delay_seconds = 0

    def get_json(self, url):
        self.urls.append(url)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def dblp(*infos, total=None):
    return {"result": {"hits": {"@total": str(total if total is not None else len(infos)), "hit": [{"info": info} for info in infos]}}}


def finished(service, job):
    end = time.monotonic() + 12
    while job["status"] in {"queued", "running"} and time.monotonic() < end:
        time.sleep(0.02)
        job = service.get_job(job["id"])
    if job["status"] != "completed":
        raise AssertionError(job)
    return job


def pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    return path


class AdapterTests(unittest.TestCase):
    def test_registered_downloader_validates_successful_output(self):
        from autopapercrawl.downloaders.router import register_downloader, download_target, DownloadOptions, CUSTOM_DOWNLOADERS
        from autopapercrawl.downloaders.common import PaperTarget, DownloadResult
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = pdf(root / "custom.pdf")
            try:
                register_downloader("custom_public", lambda target, folder, options: DownloadResult(status="downloaded", provider="custom_public", path=str(path)))
                result = download_target(PaperTarget(title="Custom"), ["custom_public"], root, DownloadOptions())
                self.assertEqual(result.outcome, "success")
                self.assertEqual(len(result.sha256), 64)
            finally:
                CUSTOM_DOWNLOADERS.pop("custom_public", None)

    def test_all_interfaces_and_no_false_pdf_claims(self):
        for key, adapter in ADAPTERS.items():
            instance = adapter(None, Registry())
            for method in ("search", "normalize", "health_check", "get_capabilities"):
                self.assertTrue(callable(getattr(instance, method)))
            self.assertFalse(instance.get_capabilities()["pdf"])
        with self.assertRaises(ValueError):
            register_adapter("broken", object)

    def test_dblp_pagination_stream_year_raw_doi_and_authors(self):
        one = {"title": "Paper A", "authors": {"author": {"@pid": "1", "text": "Alice"}}, "year": "2025", "venue": "SP", "key": "conf/sp/a", "ee": "https://doi.org/10.1/A"}
        two = {**one, "key": "conf/sp/b", "title": "Paper B"}
        client = Client([dblp(one, total=50), dblp(one, total=2), dblp(two, total=2)])
        records = DBLPAdapter(client, Registry()).search("security", {"venue": "sp", "start_year": 2025, "end_year": 2025, "max_results": 2, "page_size": 1})
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].authors, ["Alice"])
        self.assertEqual(records[0].doi, "10.1/a")
        self.assertEqual(records[0].metadata["raw_records"][0]["record"], one)
        self.assertEqual(records[0].dblp_id, "conf/sp/a")
        self.assertIn("stream:streams/conf/sp:", parse_qs(urlparse(client.urls[-1]).query)["q"][0])
        self.assertIn("year:2025:", parse_qs(urlparse(client.urls[-1]).query)["q"][0])
        self.assertEqual(parse_qs(urlparse(client.urls[-1]).query)["f"], ["1"])

    def test_dblp_missing_directory_and_empty_year_are_different(self):
        with self.assertRaises(UnsupportedCapability):
            DBLPAdapter(Client([]), Registry()).search("x", {"venue": "unregistered_custom_journal"})
        with self.assertRaises(UnsupportedCapability):
            DBLPAdapter(Client([dblp()]), Registry()).search("x", {"venue": "sp"})
        self.assertEqual(DBLPAdapter(Client([dblp({"title": "Probe"}), dblp()]), Registry()).search("x", {"venue": "sp", "start_year": 2025, "end_year": 2025}), [])

    def test_dblp_custom_journal_and_repeated_page_stop(self):
        one = {"title": "Article", "year": "2025", "key": "journals/new/a", "type": "Journal Articles"}
        client = Client([dblp(one), dblp(one, total=100), dblp(one, total=100)])
        papers = DBLPAdapter(client).search("", {"dblp_key": "journals/new", "max_results": 3, "page_size": 1})
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].publication_status, "journal")
        self.assertEqual(len(client.urls), 3)

    def test_publisher_metadata_and_explicit_configuration_required(self):
        body = '<meta name="citation_title" content="Robotics"><meta name="citation_author" content="Alice"><meta name="citation_publication_date" content="2025/01/02"><meta name="citation_pdf_url" content="/a.pdf"><meta name="citation_doi" content="10.1/x">'
        paper = article_metadata(body, "https://example.org/article")
        self.assertEqual(paper.pdf_url, "https://example.org/a.pdf")
        self.assertEqual(paper.authors, ["Alice"])
        self.assertEqual(paper.year, 2025)
        self.assertTrue(paper.metadata["raw_records"])
        with self.assertRaises(UnsupportedCapability):
            PublisherAdapter(None, Registry()).search("robotics", {})

    def test_publisher_robots_and_public_catalog(self):
        from unittest.mock import Mock
        client = Mock()
        client.get_bytes.side_effect = [b"User-agent: *\nAllow: /", b'<a href="/article/a">A</a><a href="https://other.org/article/a">Other</a>', b'<meta name="citation_title" content="Robotics"><meta name="citation_date" content="2025/01/01">']
        records = PublisherAdapter(client).search("robotics", {"venue_record": {"name": "Journal", "publisher": {"catalog_url": "https://example.org/index", "article_path": "/article/"}}, "max_results": 1})
        self.assertEqual(len(records), 1)
        self.assertEqual(client.get_bytes.call_count, 3)
        client.get_bytes.side_effect = [b"User-agent: *\nDisallow: /"]
        with self.assertRaises(UnsupportedCapability):
            PublisherAdapter(client).search("", {"venue_record": {"publisher": {"catalog_url": "https://example.org/index"}}})

    def test_normalizers_keep_full_metadata(self):
        from autopapercrawl.search import merge_papers
        zero = merge_papers(Paper("X", "a", citations="0", year="2025"), Paper("X", "b", citations=None))
        self.assertEqual(zero.citations, 0)
        self.assertEqual(zero.year, 2025)
        crossref = {"title": ["X"], "DOI": "10.1/a", "author": [{"given": "A", "family": "B", "affiliation": [{"name": "University"}]}], "container-title": ["JMLR"], "ISSN": ["1532-4435"], "subject": ["ML"], "type": "journal-article"}
        p = paper_from_crossref(crossref)
        self.assertEqual(p.affiliations, ["University"])
        self.assertEqual(p.keywords, ["ML"])
        self.assertEqual(p.venue_type, "journal")
        self.assertEqual(p.metadata["raw_records"][0]["record"], crossref)
        p = paper_from_openalex({"title": "X", "concepts": [{"display_name": "ML"}], "authorships": [{"author": {"display_name": "A"}, "institutions": [{"display_name": "U"}]}]})
        self.assertEqual(p.concepts, [{"display_name": "ML"}])
        self.assertEqual(p.affiliations, ["U"])

    def test_arxiv_journal_doi_categories_and_native_window(self):
        from xml.etree import ElementTree as ET
        body = '<entry xmlns="http://www.w3.org/2005/Atom" xmlns:a="http://arxiv.org/schemas/atom"><id>https://arxiv.org/abs/2501.00001v2</id><title>Paper</title><published>2025-01-02</published><a:doi>10.1/a</a:doi><a:journal_ref>JMLR 2025</a:journal_ref><category term="cs.LG"/></entry>'
        p = paper_from_arxiv_entry(ET.fromstring(body), {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"})
        self.assertEqual(p.doi, "10.1/a")
        self.assertEqual(p.categories, ["cs.LG"])
        self.assertEqual(p.metadata["journal_ref"], "JMLR 2025")
        from unittest.mock import Mock
        client = Mock(delay_seconds=0)
        client.get_bytes.return_value = b'<feed xmlns="http://www.w3.org/2005/Atom"/>'
        search_arxiv("learning", 1, 2024, 2026, client, from_date="2026-10-01")
        self.assertIn("202610010000", client.get_bytes.call_args.args[0])


class IdentityAndClassificationTests(unittest.TestCase):
    def test_doi_arxiv_dblp_identity_order_and_snapshots(self):
        a = Paper("First", "arXiv", doi="10.1/a", metadata={"arxiv_id": "2501.00001v1"}, pdf_url="https://a/a.pdf")
        b = Paper("Second", "Crossref", doi="https://doi.org/10.1/A", pdf_url="https://b/b.pdf")
        result = deduplicate([a, b])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].versions), 2)
        self.assertEqual(len(result[0].metadata["pdf_urls"]), 2)
        self.assertEqual(len(deduplicate([Paper("A", "a", arxiv_id="2501.00001v1"), Paper("B", "a", arxiv_id="2501.00001v2")])), 1)
        self.assertEqual(len(deduplicate([Paper("A", "a", dblp_id="conf/sp/a"), Paper("B", "a", dblp_id="conf/sp/a")])), 1)
        self.assertEqual(len(deduplicate([Paper("A", "a", doi="10.1/a", arxiv_id="2501.00001"), Paper("A", "a", doi="10.1/b", arxiv_id="2501.00001")])), 2)

    def test_different_year_or_author_never_irreversibly_merges(self):
        records = [Paper("A study of model security", "a", authors=["Alice"], year=2024, doi="10.1/pre"), Paper("A study of model security", "b", authors=["Alice"], year=2025, doi="10.1/pub"), Paper("A study of model securities", "c", authors=["Alice"], year=2025)]
        result = deduplicate(records)
        self.assertEqual(len(result), 3)
        relations = [relation for paper in result for relation in paper.metadata.get("related_versions", [])]
        self.assertTrue(any(relation["status"] == "linked" for relation in relations))
        self.assertTrue(any(relation["status"] == "review_required" for relation in relations))
        self.assertEqual(len(deduplicate([Paper("X", "a", year=2025, authors=["Alice"]), Paper("X", "a", year=2025, authors=["Bob"])])), 2)

    def test_confidence_multilabel_exclusions_and_unknown_retention(self):
        topics = {"robotics": {"label": "Robotics", "keywords": ["robot learning"], "excluded_keywords": ["fiction"]}, "ml": {"label": "ML", "keywords": ["learning"]}}
        paper = Paper("Robot learning with data", "a")
        self.assertEqual(set(classify_paper(paper, topics)), {"robotics", "ml"})
        paper = Paper("Other title", "a", abstract="We study robot learning")
        self.assertFalse(classify_paper(paper, {"robotics": topics["robotics"]}))
        self.assertEqual(paper.classification["robotics"]["confidence"], "medium")
        paper = Paper("Robot learning in fiction", "a")
        classify_paper(paper, topics)
        self.assertEqual(paper.classification["robotics"]["confidence"], "low")
        self.assertEqual(paper.classification["robotics"]["excluded_keywords"], ["fiction"])

    def test_venue_evidence_does_not_treat_name_as_absolute_proof(self):
        paper = Paper("X", "Crossref", metadata={"venue": "Computer Networks"})
        self.assertEqual(match_paper_venue(paper, "comnet")["status"], "probable")
        paper.metadata["issn"] = ["13891286"]
        self.assertEqual(match_paper_venue(paper, "comnet")["status"], "confirmed")
        self.assertEqual(match_paper_venue(paper, "sp")["status"], "unconfirmed")


class DatabaseAndWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Workbench(self.root, import_existing=False)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def search(self, papers, **options):
        supplied = {"directions": [], "query": "example", "sources": ["arxiv"], "enrich_metadata": False, **options}
        def fake(*args, **kwargs):
            kwargs["diagnostics"].append({"source": "arxiv", "query": args[0], "venue": kwargs.get("venue"), "status": "ok", "count": len(papers), "requests": 1})
            return copy.deepcopy(papers)
        with patch("autopapercrawl.workbench.search_all", side_effect=fake):
            return finished(self.service, self.service.start_search(supplied))

    def test_modes_preview_matches_full_execution(self):
        options = {"directions": ["ai_security"], "sources": ["arxiv"]}
        quick = self.service.preview_plan({**options, "mode": "quick"})
        standard = self.service.preview_plan({**options, "mode": "standard"})
        full = self.service.preview_plan({**options, "mode": "comprehensive", "query_limit": 1})
        self.assertEqual(quick["query_count"], 1)
        self.assertEqual(standard["query_count"], 3)
        self.assertEqual(full["query_count"], len(self.service.topics["ai_security"]["queries"]))
        self.assertTrue(full["all_topic_queries"])
        with patch("autopapercrawl.workbench.search_all", return_value=[]) as search:
            job = finished(self.service, self.service.start_search({**options, "mode": "comprehensive"}))
        self.assertEqual([call.args[0] for call in search.call_args_list], full["queries"])
        self.assertEqual(job["plan"]["entries"], full["entries"])

    def test_low_confidence_and_unconfirmed_arxiv_are_preserved(self):
        paper = Paper("QUIC attack analysis", "arXiv", year=2025, arxiv_id="2501.00001", abstract="Malicious traffic over QUIC")
        job = self.search([paper], query="QUIC malicious traffic", directions=["ai_security"], venues=["sp"])
        records = self.service.records(job["paper_ids"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["metadata"]["review_confidence"], "low")
        self.assertTrue(records[0]["metadata"]["venue_review_required"])
        self.assertEqual(len(self.service.list_papers({"job_id": job["id"]})["items"]), 1)
        self.assertTrue((Path(job["report_dir"]) / "raw_records.jsonl").exists())
        self.assertTrue((Path(job["report_dir"]) / "papers.bib").exists())

    def test_checkpoint_scoped_overlap_and_failure_do_not_advance(self):
        job = self.search([Paper("Example", "arXiv", year=2025)], incremental=True)
        plan = self.service.preview_plan({**job["options"], "incremental": True})
        self.assertEqual(plan["since"], (date.fromisoformat(job["started"][:10]) - timedelta(days=7)).isoformat())
        other = self.service.preview_plan({**job["options"], "query": "different", "incremental": True})
        self.assertIsNone(other["since"])
        def failing(*args, **kwargs):
            kwargs["diagnostics"].append({"source": "arxiv", "query": args[0], "status": "request_failed", "count": 0, "error": "timeout"})
            return []
        with patch("autopapercrawl.workbench.search_all", side_effect=failing):
            failed = finished(self.service, self.service.start_search({**job["options"], "query": "different"}))
        self.assertIsNone(self.service.preview_plan(failed["options"])["checkpoint"])

    def test_capped_results_never_advance_incremental_cursor(self):
        job = self.search([Paper("A", "arXiv", year=2025)], per_query=1, incremental=True)
        self.assertIsNone(self.service.preview_plan(job["options"])["checkpoint"])

    def test_source_states_keep_their_actual_venue(self):
        paper = Paper("Example", "arXiv", year=2025)
        self.search([paper], venues=["sp", "cvpr"])
        self.assertEqual({item["venue"] for item in self.service.source_states()}, {"sp", "cvpr"})

    def test_sql_filters_sort_pagination_selection_and_no_full_document_scan(self):
        ids = self.service.store([Paper("A", "a", authors=["Alice"], abstract="Robotics", year=2025, citations=3, code_url="https://github.com/a/b"), Paper("B", "b", authors=["Bob"], year=2026, citations=7)])
        with patch.object(self.service, "records", side_effect=AssertionError("SQL list must not load all documents")):
            self.assertEqual(self.service.list_papers({"author": "alice", "abstract": "robot", "has_code": "true"})["all_ids"], [ids[0]])
            self.assertEqual(self.service.list_papers({"sort": "citations"}, limit=1)["items"][0]["title"], "B")
            self.assertEqual(len(self.service.list_papers({"ids": ids[0]})["items"]), 1)
            self.assertEqual(self.service.list_papers({"job_id": "missing"})["total"], 0)

    def test_delete_is_reversible_and_keeps_pdf_files(self):
        identifier = self.service.store([Paper("A", "a", year=2025)])[0]
        path = pdf(self.root / "paper.pdf")
        self.service.update_download(identifier, {"status": "downloaded", "path": str(path), "outcome": "success"})
        result = self.service.delete_records([identifier])
        self.assertEqual(self.service.records(), [])
        self.assertTrue(path.exists())
        with connection(result["backup"]) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM papers").fetchone()[0], 1)
        with self.service.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM deleted_papers").fetchone()[0], 1)

    def test_reclassify_custom_domain_persists_and_download_survives_update(self):
        identifier = self.service.store([Paper("Robot planning", "test", year=2025)])[0]
        self.service.update_download(identifier, {"status": "no_fulltext", "outcome": "no_fulltext"})
        self.service.registry.save("domains", {"domain_id": "robot_plans", "domain_name": "Plans", "keywords": ["robot planning"]})
        self.assertEqual(self.service.reclassify([identifier]), 1)
        record = self.service.records([identifier])[0]
        self.assertIn("robot_plans", record["directions"])
        self.assertEqual(record["download"]["status"], "no_fulltext")

    def test_retry_uses_failed_source_entries_and_preserves_original_task(self):
        def failing(*args, **kwargs):
            kwargs["diagnostics"].append({"source": "arxiv", "query": "example", "venue": None, "status": "request_failed", "count": 0})
            return []
        with patch("autopapercrawl.workbench.search_all", side_effect=failing):
            first = finished(self.service, self.service.start_search({"directions": [], "query": "example", "sources": ["arxiv"]}))
        with patch("autopapercrawl.workbench.search_all", return_value=[]):
            second = finished(self.service, self.service.retry_job(first["id"]))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["options"]["retry_of"], first["id"])
        self.assertEqual(second["plan"]["source_calls"], 1)

    def test_old_schema_migration_preserves_data_and_makes_backup(self):
        database = self.root / "legacy.sqlite3"
        record = {"id": "old", "title": "Legacy", "source": "a", "year": 2025, "authors": ["Alice"], "metadata": {"custom": "kept"}}
        with connection(database) as conn:
            conn.execute("CREATE TABLE papers (id TEXT PRIMARY KEY,paper_key TEXT UNIQUE,title_key TEXT,doi TEXT,document TEXT)")
            conn.execute("INSERT INTO papers VALUES (?,?,?,?,?)", ("old", "title:legacy", "legacy", None, json.dumps(record)))
        backup = migrate(database)
        self.assertTrue(Path(backup).is_file())
        with connection(database) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(json.loads(conn.execute("SELECT document FROM papers").fetchone()[0]), record)
            self.assertEqual(conn.execute("SELECT author_text FROM papers").fetchone()[0], "alice")
        self.assertIsNone(migrate(database))

    def test_version_links_persist_across_separate_collections(self):
        first = self.service.store([Paper("Same research", "arXiv", authors=["Alice"], year=2024, doi="10.1/pre")])[0]
        second = self.service.store([Paper("Same research", "Crossref", authors=["Alice"], year=2025, doi="10.1/published")])[0]
        self.assertNotEqual(first, second)
        self.assertTrue(all(record["metadata"].get("related_versions") for record in self.service.records()))

    def test_ai_security_does_not_recommend_water_security_or_securitization(self):
        for title in ("Water footprint of large AI models: risks to global water security", "Artificial intelligence and securitization in banking"):
            paper = Paper(title, "test")
            classify_paper(paper, self.service.topics)
            self.assertNotEqual(paper.classification["ai_security"]["confidence"], "high")

    def test_model_roundtrip_and_complete_exports(self):
        record = Paper("=Unsafe {Title}", "a", authors=["Alice"], year=2025, affiliations=["U"], concepts=[{"id": 1}], code_url="https://github.com/a/b", metadata={"raw_records": [{"source": "a", "record": {"custom": 1}}]}).as_dict()
        csv_data = list(csv.reader(io.StringIO(export_csv([record]).decode("utf-8-sig"))))
        self.assertIn("作者", csv_data[0])
        self.assertIn("完整扩展元数据", csv_data[0])
        self.assertTrue(csv_data[1][csv_data[0].index("论文标题")].startswith("'="))
        self.assertEqual(json.loads(export_json([record]))[0]["concepts"], [{"id": 1}])
        self.assertIn(r"\{Title\}", export_bibtex([record]).decode())


class PDFTests(unittest.TestCase):
    def test_real_parse_sha_content_reuse_and_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = pdf(Path(tmp) / "a.pdf")
            info = inspect_pdf(first)
            self.assertEqual(info["pages"], 1)
            self.assertEqual(len(info["sha256"]), 64)
            second = Path(tmp) / "b.pdf.part"
            second.write_bytes(first.read_bytes())
            self.assertEqual(content_duplicate(second, inspect_pdf(second)), first)
            with self.assertRaises(ValueError):
                inspect_pdf(first, max_bytes=10)

    def test_pdf_markers_are_not_enough_to_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.pdf"
            path.write_bytes(b"%PDF-1.4\nnot really a PDF\n%%EOF")
            with self.assertRaises(ValueError):
                inspect_pdf(path)

    def test_auth_key_is_only_sent_to_openalex_and_not_written_in_url(self):
        from unittest.mock import Mock
        response = Mock(status_code=200, content=b"{}", headers={})
        with patch.dict("os.environ", {"OPENALEX_API_KEY": "test-secret"}), patch("autopapercrawl.http.requests.get", return_value=response) as get, patch.object(HttpClient, "wait"):
            client = HttpClient(retries=0)
            client.get_json("https://api.openalex.org/works")
            self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer test-secret")
            self.assertNotIn("test-secret", get.call_args.args[0])
            client.get_json("https://api.crossref.org/works")
            self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    def test_stream_size_limit_and_duplicate_hash_on_download(self):
        from unittest.mock import Mock
        from autopapercrawl.downloaders.common import PaperTarget
        from autopapercrawl.downloaders.pdf_url import download_pdf_url
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = pdf(root / "existing.pdf").read_bytes()
            response = Mock(headers={"Content-Type": "application/pdf"}, url="https://example.org/a.pdf")
            response.__enter__ = lambda self: response
            response.__exit__ = lambda *args: False
            response.iter_content.return_value = [content]
            with patch("autopapercrawl.downloaders.pdf_url.requests.get", return_value=response):
                result = download_pdf_url(PaperTarget(title="Other title", pdf_url=response.url), root, timeout=2, retries=0, sleep_seconds=0)
            self.assertEqual(result.outcome, "success")
            self.assertEqual(result.duplicate_of, str(root / "existing.pdf"))
            self.assertEqual(len(result.sha256), 64)
            response.iter_content.return_value = [content]
            with patch("autopapercrawl.downloaders.pdf_url.requests.get", return_value=response):
                result = download_pdf_url(PaperTarget(title="Too big", pdf_url=response.url), root, timeout=2, retries=0, sleep_seconds=0, max_bytes=20)
            self.assertEqual(result.outcome, "invalid_pdf")
            self.assertFalse(list(root.glob("*.part")))

    def test_arxiv_fuzzy_fulltext_substitution_requires_author_evidence(self):
        from autopapercrawl.open_access import resolve_arxiv_title_urls
        from autopapercrawl.downloaders.common import PaperTarget
        candidate = Paper("A framework for encrypted traffic classification!", "arXiv", year=2025, authors=["Other"], pdf_url="https://arxiv.org/pdf/2501.1")
        target = PaperTarget(title="A framework for encrypted traffic classifications", metadata={"year": 2025, "authors": ["Alice"]})
        with patch("autopapercrawl.open_access.search_arxiv", return_value=[candidate]):
            self.assertEqual(resolve_arxiv_title_urls(target, None, []), [])

    def test_access_challenge_is_explicit_failure_without_bypass(self):
        from unittest.mock import Mock
        response = Mock(status_code=202, headers={"Content-Type": "text/html"}, text='<script id="anubis_challenge">challenge</script>')
        with patch("autopapercrawl.http.requests.get", return_value=response), patch.object(HttpClient, "wait"):
            with self.assertRaisesRegex(ValueError, "访问校验"):
                HttpClient(retries=0).get_json("https://dblp.org/search/publ/api?q=test")


if __name__ == "__main__":
    unittest.main()
