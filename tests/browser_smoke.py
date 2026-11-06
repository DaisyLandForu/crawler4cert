"""Optional Edge/Playwright UI acceptance against temporary local data only."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pypdf import PdfWriter
from playwright.sync_api import sync_playwright, expect
from autopapercrawl.models import Paper
from autopapercrawl.registry import Registry
from autopapercrawl.web import handler_class
from autopapercrawl.workbench import Workbench


def main():
    output = ROOT / "outputs" / "browser_acceptance"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = Workbench(root, import_existing=False)
        ids = service.store([Paper(f"Encrypted traffic classification {i:03d}", "test", year=2025, authors=["Alice"], abstract="Full abstract for encrypted traffic classification.", metadata={"directions": ["encrypted_traffic"], "venue": "Computer Networks"}, citations=i) for i in range(120)])
        path = root / "cached.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(path)
        service.update_download(ids[0], {"status": "downloaded", "outcome": "success", "path": str(path)})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(service, "browser-test-token"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1480, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                def open_panel(identifier):
                    panel = page.locator(f"#{identifier}")
                    if not panel.evaluate("node => node.open"):
                        panel.locator(":scope > summary").click()
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator('#topics input[name="topic"]')).to_have_count(4)
                expect(page.locator('#more-topics input[name="topic"]')).to_have_count(10)
                expect(page.locator("#venues option")).to_have_count(56)
                expect(page.locator("#instance-info")).to_contain_text(str(root.resolve()))
                expect(page.locator("#count-total")).to_have_text("120")
                expect(page.locator("#papers tr")).to_have_count(50)
                page.locator("#next").click()
                expect(page.locator("#page-info")).to_contain_text("51–100")
                page.locator("#select-all").click()
                expect(page.locator("#selection-count")).to_have_text("已选 120 篇")
                page.locator("#clear-selection").click()
                expect(page.locator('#mode, #task-name, #incremental, #since, #query-limit')).to_have_count(0)
                expect(page.locator('#auto-download')).to_be_visible()
                expect(page.locator('#max-results')).to_be_visible()
                expect(page.locator('#sources')).to_be_visible()
                assert page.locator('#auto-download').evaluate("node => node.closest('section').getAttribute('aria-labelledby')") == 'search-options-heading'
                open_panel("tools-menu")
                page.locator("#preview-plan").click()
                expect(page.locator("#detail-content")).to_contain_text('"query_count": 29')
                assert json.loads(page.locator('#detail-content .plan-view').inner_text())['mode'] == 'comprehensive'
                page.locator("#close-detail").click()
                page.locator("#add-direction").click()
                expect(page.locator("#registry-id")).to_have_count(0)
                page.locator("#registry-name").fill("测试生物学")
                page.locator("#registry-words").fill("genome analysis\nproteomics")
                page.locator("#registry-form button[type=submit]").click()
                expect(page.locator("#toast")).to_have_text("本地配置已保存")
                page.locator("#close-registry").click()
                expect(page.locator("#topics")).to_contain_text("测试生物学")
                domain_id = next(key for key, item in Registry(root).topics().items() if item["label"] == "测试生物学")
                assert domain_id.startswith("custom_")
                open_panel("tools-menu")
                page.locator("#manage-registry").click()
                page.locator("#registry-kind").select_option("venues")
                page.locator("#registry-name").fill("测试生物学会议")
                page.locator("#registry-words").fill("Test Biology Conference")
                with page.expect_response(lambda response: response.url.endswith("/api/registry/save") and response.request.method == "POST") as saved:
                    page.locator("#registry-form button[type=submit]").click()
                assert saved.value.ok
                venue_id = saved.value.json()["venue_id"]
                assert venue_id.startswith("custom_")
                expect(page.locator(f'#venues option[value="{venue_id}"]')).to_have_count(1)
                page.locator("#close-registry").click()
                page.reload()
                expect(page.locator(f'#topics input[value="{domain_id}"]')).to_have_count(1)
                expect(page.locator(f'#venues option[value="{venue_id}"]')).to_have_text("测试生物学会议")
                persisted = Registry(root)
                assert persisted.topics()[domain_id]["queries"] == ["genome analysis", "proteomics"]
                assert persisted.venue(venue_id)["name"] == "测试生物学会议"
                page.locator("#topics-none").click()
                page.locator(f'#topics input[value="{domain_id}"]').check()
                open_panel("venue-picker")
                page.locator("#venues").select_option(venue_id)
                open_panel("tools-menu")
                page.locator("#preview-plan").click()
                expect(page.locator("#detail-content")).to_contain_text("genome analysis")
                expect(page.locator("#detail-content")).to_contain_text(venue_id)
                page.locator("#close-detail").click()
                # A direction can remain usable without occupying a shortcut.
                open_panel("tools-menu")
                page.locator("#manage-registry").click()
                page.locator("#registry-kind").select_option("domains")
                page.locator("#registry-existing").select_option(domain_id)
                page.locator("#registry-shortcut").uncheck()
                with page.expect_response(lambda response: response.url.endswith("/api/registry/save") and response.request.method == "POST"):
                    page.locator("#registry-form button[type=submit]").click()
                expect(page.locator(f'#more-topics input[value="{domain_id}"]')).to_have_count(1)
                page.locator("#close-registry").click()
                page.reload()
                expect(page.locator(f'#topics input[value="{domain_id}"]')).to_have_count(0)
                expect(page.locator(f'#more-topics input[value="{domain_id}"]')).to_have_count(1)
                assert Registry(root).topics()[domain_id]["show_in_shortcuts"] is False
                page.locator("#more-directions summary").click()
                page.locator("#topics-none").click()
                page.locator(f'#more-topics input[value="{domain_id}"]').check()
                expect(page.locator("#more-topics-count")).to_contain_text("已选 1")
                open_panel("tools-menu")
                page.locator("#preview-plan").click()
                expect(page.locator("#detail-content")).to_contain_text("genome analysis")
                page.locator("#close-detail").click()
                open_panel("venue-picker")
                page.locator("#venues").select_option([])
                open_panel("tools-menu")
                page.locator("#capabilities").click()
                expect(page.locator("#detail-content")).to_contain_text("可查询（未实测）")
                page.locator("#close-detail").click()
                page.locator("#topics-none").click()
                page.locator("#query").fill("test result")
                page.locator('#auto-download').check()
                submitted = page.evaluate('options()')
                assert submitted['mode'] == 'comprehensive' and submitted['download_pdfs'] is True
                assert not {'incremental', 'since', 'name'} & submitted.keys()
                page.locator('#auto-download').uncheck()
                page.locator(f'#more-topics input[value="{domain_id}"]').check()
                open_panel("tools-menu")
                page.locator("#preview-plan").click()
                plan = page.locator("#detail-content .plan-view").inner_text()
                assert json.loads(plan)["queries"] == ["test result"]
                page.locator("#close-detail").click()
                page.locator("#topics-none").click()
                page.locator('#topics input[value="encrypted_traffic"]').check()
                with patch("autopapercrawl.workbench.search_all", return_value=[Paper("A test result", "test", year=2025, abstract="Research abstract included in metadata export.")]):
                    page.locator("#search").click()
                    expect(page.locator("#job-state")).to_have_text("已完成", timeout=15000)
                    expect(page.locator("#count-total")).to_have_text("1", timeout=10000)
                page.locator('#query').fill('changed after task creation')
                page.locator('#topics-none').click()
                open_panel('job-extra')
                expect(page.locator('#job-conditions')).to_contain_text('加密流量分析检测')
                expect(page.locator('#job-conditions')).to_contain_text('test result')
                expect(page.locator('#job-conditions')).not_to_contain_text('changed after task creation')
                page.locator('#job-details').click()
                expect(page.locator('#detail-content .task-conditions')).to_contain_text('加密流量分析检测')
                expect(page.locator('#detail-content .task-conditions')).to_contain_text('test result')
                expect(page.locator('#detail-content .task-query-list li')).to_have_text('test result')
                page.locator('#close-detail').click()
                # Old jobs render from their own saved options and plan too.
                page.evaluate('setJob({...state.job,search_conditions:undefined})')
                expect(page.locator('#job-conditions')).to_contain_text('加密流量分析检测')
                expect(page.locator('#job-conditions')).to_contain_text('test result')
                page.locator("#papers .paper-title").click()
                expect(page.locator("#detail-content")).to_contain_text("版本及关联候选")
                page.locator("#close-detail").click()
                page.locator("#page-all").check()
                open_panel("library-actions")
                page.locator("#reclassify").click()
                expect(page.locator("#toast")).to_contain_text("已重新分类 1 篇")
                open_panel("export-menu")
                with page.expect_download() as download:
                    page.locator("#export-json").click()
                exported = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
                assert len(exported) == 1 and exported[0]["title"] == "A test result"
                assert exported[0]["abstract"] == "Research abstract included in metadata export."
                page.on("dialog", lambda dialog: dialog.accept())
                open_panel("library-actions")
                page.locator("#delete-selected").click()
                expect(page.locator("#count-total")).to_have_text("0")
                page.locator("#query").fill("")
                page.locator("#library-view").click()
                expect(page.locator("#count-total")).to_have_text("120")
                page.route("**/api/download", lambda route: route.fulfill(status=400, content_type="application/json", body=json.dumps({"error":"Single-paper download request verified"})))
                expected_id = page.locator("#papers [data-download]").first.get_attribute("data-download")
                with page.expect_request(lambda request: request.url.endswith("/api/download") and request.method == "POST") as single:
                    page.locator("#papers [data-download]").first.click()
                assert single.value.post_data_json["ids"] == [expected_id]
                expect(page.locator("#toast")).to_have_text("Single-paper download request verified")
                page.unroute("**/api/download")
                page.locator("#title").fill("000")
                open_panel("result-filters")
                page.locator("#filter").click()
                expect(page.locator("#count-total")).to_have_text("1")
                page.locator("#page-all").check()
                open_panel("download-settings")
                page.locator("#download-dir").fill(str(root / "chosen_pdfs"))
                with page.expect_response(lambda response: response.url.endswith("/api/download") and response.request.method == "POST") as created:
                    page.locator("#download").click()
                download_id = created.value.json()["id"]
                page.wait_for_function("identifier => state.job?.id === identifier", arg=download_id)
                expect(page.locator("#job-state")).to_have_text("已完成", timeout=15000)
                expect(page.locator("#papers .pdf-link")).to_have_count(1)
                assert len(list((root / "chosen_pdfs" / "pdfs").glob("*.pdf"))) == 1
                assert service.get_job(page.evaluate("state.job.id"))["results"][0]["sha256"]
                page.locator("#title").fill("")
                open_panel("result-filters")
                page.locator("#filter").click()
                expect(page.locator("#count-total")).to_have_text("120")
                page.locator("#sort").select_option("citations")
                expect(page.locator("#papers .paper-title").first).to_contain_text("119")
                page.locator("#papers .paper-abstract summary").first.click()
                expect(page.locator("#papers .paper-abstract p").first).to_be_visible()
                page.evaluate("document.querySelector('main').scrollTop=0;document.querySelector('aside').scrollTop=0")
                page.screenshot(path=str(output / "desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                expect(page.locator("#search")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"), "Mobile viewport overflow"
                page.locator('#papers .pdf-link').first.scroll_into_view_if_needed()
                expect(page.locator('#papers .pdf-link').first).to_be_in_viewport()
                page.screenshot(path=str(output / "mobile.png"), full_page=True)
                browser.close()
            assert not errors, errors
            (output / "result.json").write_text(json.dumps({"passed": True, "js_errors": errors, "checks": ["instance_directory", "pagination", "cross_page_selection", "comprehensive_plan", "custom_domain_and_venue_persist_after_reload", "custom_registry_search_plan", "capabilities", "search", "detail", "reclassify", "json_export", "delete_with_pdf_kept", "custom_folder_cached_pdf", "single_paper_download", "metadata_with_abstract", "task_conditions_independent_of_form", "legacy_task_conditions", "secondary_menus", "citation_sort", "abstract_expand", "responsive_layout"]}, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Browser acceptance passed; screenshots:", output)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            service.close()


if __name__ == "__main__":
    main()
