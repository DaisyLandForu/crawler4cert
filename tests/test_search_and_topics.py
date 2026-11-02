import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch
from datetime import date

from autopapercrawl.http import HttpClient
from autopapercrawl.models import Paper
from autopapercrawl.search import deduplicate, extract_crossref_year, search_all, search_arxiv, search_crossref, search_openalex
from autopapercrawl.topics import classify_paper, load_topics, security_score


def atom(entries, total=20):
    content = "".join(f'<entry><id>https://arxiv.org/abs/{identifier}</id><title>{title}</title><published>{year}-01-02T00:00:00Z</published><summary>Abstract</summary><link title="pdf" href="https://arxiv.org/pdf/{identifier}"/></entry>' for identifier, title, year in entries)
    return f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"><opensearch:totalResults>{total}</opensearch:totalResults>{content}</feed>'.encode()


class FakeClient:
    delay_seconds = 0
    timeout_seconds = 1
    retries = 0

    def __init__(self, results):
        self.results = iter(results)
        self.urls = []

    def get_bytes(self, url, headers=None):
        self.urls.append(url)
        return next(self.results)

    def get_json(self, url, headers=None):
        return self.get_bytes(url, headers)


class SearchTests(unittest.TestCase):
    def test_arxiv_pages_and_server_date_filter(self):
        client = FakeClient([atom([("2601.00001", "One", 2026), ("2501.00002", "Two", 2025)], 3), atom([("2401.00003", "Three", 2024)], 3)])
        papers = search_arxiv("encrypted traffic", 3, 2024, 2026, client, page_size=2)
        self.assertEqual(len(papers), 3)
        params = [parse_qs(urlparse(url).query) for url in client.urls]
        self.assertEqual([entry["start"][0] for entry in params], ["0", "2"])
        cutoff = min(date(2026, 12, 31), date.today()).strftime("%Y%m%d")
        self.assertIn(f"submittedDate:[202401010000 TO {cutoff}2359]", params[0]["search_query"][0])
        self.assertIn("all:encrypted AND all:traffic", params[0]["search_query"][0])
        self.assertEqual(params[0]["sortBy"], ["submittedDate"])

    def test_arxiv_filters_bad_dates_without_stopping_pagination(self):
        client = FakeClient([atom([("old", "Old", 2020)], 2), atom([("new", "New", 2026)], 2)])
        papers = search_arxiv("security", 1, 2024, 2026, client, page_size=1)
        self.assertEqual([paper.title for paper in papers], ["New"])

    def test_arxiv_repeated_page_terminates(self):
        page = atom([("one", "One", 2026)], 100)
        client = FakeClient([page, page])
        self.assertEqual(len(search_arxiv("security", 3, 2024, 2026, client, page_size=1)), 1)
        self.assertEqual(len(client.urls), 2)

    def test_arxiv_title_filter_is_sent_to_api(self):
        client = FakeClient([atom([], 0)])
        search_arxiv("traffic", 1, 2024, 2026, client, title_filter="NetCoFi")
        expression = parse_qs(urlparse(client.urls[0]).query)["search_query"][0]
        self.assertIn('ti:"NetCoFi"', expression)

    def test_openalex_keeps_page_size_constant(self):
        def item(index):
            return {"title": f"Paper {index}", "publication_year": 2026}
        client = FakeClient([{"results": [item(1), item(2)]}, {"results": [item(3), item(4)]}])
        papers = search_openalex("security", 3, 2024, 2026, client, None, page_size=2)
        self.assertEqual(len(papers), 3)
        self.assertEqual([parse_qs(urlparse(url).query)["per-page"] for url in client.urls], [["2"], ["2"]])

    def test_crossref_filters_unknown_and_out_of_range(self):
        client = FakeClient([{"message": {"items": [{"title": ["Unknown"]}, {"title": ["Old"], "published": {"date-parts": [[2020]]}}, {"title": ["New"], "published": {"date-parts": [[2026]]}}]}}])
        papers = search_crossref("security", 1, 2024, 2026, client, None)
        self.assertEqual([paper.title for paper in papers], ["New"])
        self.assertIn("sort=published", client.urls[0])

    def test_crossref_uses_earliest_publication_not_created(self):
        self.assertEqual(extract_crossref_year({"published-print": {"date-parts": [[2025]]}, "published-online": {"date-parts": [[2024, 10]]}}), 2024)
        self.assertIsNone(extract_crossref_year({"created": {"date-parts": [[2026]]}}))

    def test_newest_outweighs_citations(self):
        papers = [Paper("Old", "a", year=2024, citations=900), Paper("New", "b", year=2026, citations=0)]
        self.assertEqual(deduplicate(papers)[0].title, "New")
        self.assertEqual(deduplicate(papers, sort_by="citations")[0].title, "Old")

    def test_dedup_merges_preprint_doi_and_preserves_pdf_alternatives(self):
        papers = [Paper("Neural Security!", "arXiv", year=2025, pdf_url="https://arxiv.org/pdf/a", metadata={"arxiv_id": "a"}),
                  Paper("Neural security", "Crossref", year=2026, doi="10.1/test", abstract="Complete", pdf_url="https://example.org/test.pdf"),
                  Paper("Neural Security: extended version", "OpenAlex", doi="10.1/test", citations=2)]
        result = deduplicate(papers)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].metadata["sources"]), {"arXiv", "Crossref", "OpenAlex"})
        self.assertEqual(len(result[0].metadata["pdf_urls"]), 2)
        self.assertEqual(result[0].metadata["arxiv_id"], "a")

    def test_conflicting_dois_stay_distinct(self):
        self.assertEqual(len(deduplicate([Paper("Same title", "a", doi="10.1/a"), Paper("Same title", "b", doi="10.1/b")])), 2)

    def test_source_error_is_not_reported_as_empty(self):
        diagnostics = []
        with patch("autopapercrawl.search.search_openalex", side_effect=TimeoutError("test timeout")):
            papers = search_all("security", max_results=3, start_year=2024, end_year=2026, client=HttpClient(), sources=["openalex"], source_workers=1, diagnostics=diagnostics)
        self.assertEqual(papers, [])
        self.assertEqual(diagnostics[0]["status"], "request_failed")

    def test_security_recall_can_keep_all_sources_before_classification(self):
        with patch("autopapercrawl.search.search_openalex", return_value=[Paper("Irrelevant", "a", year=2026)]), patch("autopapercrawl.search.search_arxiv", return_value=[Paper("Satellite security", "b", year=2025)]):
            papers = search_all("security", max_results=1, start_year=2024, end_year=2026, client=HttpClient(),
                                sources=["openalex", "arxiv"], source_workers=1, truncate_results=False)
        self.assertEqual(len(papers), 2)

    def test_arxiv_pacing_survives_client_cloning(self):
        with patch.object(HttpClient, "_arxiv_last_request", 99.0), patch("autopapercrawl.http.time.monotonic", return_value=100.0), patch("autopapercrawl.http.time.sleep") as sleep:
            HttpClient(delay_seconds=0).wait("https://export.arxiv.org/api/query")
            HttpClient(delay_seconds=0).wait("https://export.arxiv.org/api/query")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 3.0])


class TopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.topics = load_topics()

    def test_multi_label_classification_uses_abstract(self):
        paper = Paper("A new measurement method", "test", abstract="Detecting malicious encrypted traffic in satellite networks improves security.")
        labels = classify_paper(paper, self.topics)
        self.assertEqual(set(labels), {"satellite_security", "encrypted_traffic"})
        self.assertTrue(all(hit["field"] == "abstract" for group in labels["satellite_security"]["evidence"] for hit in group))

    def test_all_four_directions(self):
        examples = {"satellite_security": "Satellite communication authentication", "vulnerability_detection": "Firmware vulnerability discovery",
                    "encrypted_traffic": "TLS traffic fingerprinting", "ai_security": "Jailbreak attacks on large language models"}
        for direction, title in examples.items():
            self.assertIn(direction, classify_paper(Paper(title, "test"), self.topics))

    def test_unrelated_satellite_and_tls_substrings_rejected(self):
        for title in ("Satellite image classification for crop mapping", "Traffic flow optimization", "Geophysical surface wave analysis", "Models of saltls traffic flow", "Economic vulnerability detection", "Robust neural network prediction with fuzzy analysis", "Machine learning for air traffic safety and trajectory prediction"):
            self.assertFalse(classify_paper(Paper(title, "test"), self.topics), title)

    def test_background_security_words_do_not_make_unrelated_work_relevant(self):
        examples = [("Skin lesion binary classification", "A neural network exploits complementary features for malignant lesion detection."),
                    ("RF fingerprint device identification", "IoT devices are vulnerable to attackers. We identify devices using software defined radio."),
                    ("Air traffic decision support", "Machine learning improves resilience and safety using global navigation satellite systems."),
                    ("Microclimate prediction", "A neural network model with fuzzy analysis improves robustness.")]
        for title, abstract in examples:
            self.assertFalse(classify_paper(Paper(title, "test", abstract=abstract), self.topics), title)

    def test_security_curation_replaces_geophysics_bias(self):
        self.assertGreater(security_score({"title": "Jailbreak attacks on language models", "year": 2026, "citations": 0}, self.topics),
                           security_score({"title": "MASW surface waves software workflow", "year": 2024, "citations": 5000}, self.topics))


if __name__ == "__main__":
    unittest.main()
