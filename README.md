# crawler4cert

A local academic literature workbench built on [AutoPaperCrawl](https://github.com/Shirmer/AutoPaperCrawl). Cybersecurity is a preset application, not a restriction on subjects or venues. The Python package remains named `autopapercrawl`; Conda is optional.

[中文完整说明](README.zh-CN.md) · [Implementation and audit](docs/implementation.md) · [Acceptance results](docs/delivery.zh-CN.md) · [Venue capabilities](docs/capabilities.md)

## Install and run

Install **Python 3.11** (the tested version). Conda is not required. On Windows, create a local virtual environment and install dependencies once:

```powershell
cd E:\crawler4cert
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then double-click **`start_workbench.cmd`**. No environment activation is needed. The launcher selects the first Python 3.11+ interpreter that can import `requests` and `pypdf`, in this order:

1. Project `.venv`, then `venv`.
2. Active virtual environment, then active Conda environment.
3. Windows Python launcher (`py -3.11`), then `python` on PATH.
4. Existing `autopapercrawl` Conda environment, if Conda is available.

An unusable candidate is skipped. If no candidate is usable, the launcher shows the setup commands and exits; it does not install packages or change environments automatically. It prints the selected interpreter and project directory. It uses port **8767** and opens the local browser UI. Additional arguments are forwarded, for example `start_workbench.cmd --port 8770 --no-browser`.

To start manually:

```powershell
.\.venv\Scripts\python.exe -m autopapercrawl serve --port 8767
```

If you already use Conda, it remains supported:

```powershell
conda env create -f environment.yml
conda activate autopapercrawl
python -m autopapercrawl serve --port 8767
```

For an existing environment, install/update dependencies with that environment's `python -m pip install -r requirements.txt`. Requests and pypdf are required; the browser UI needs no Node build. CLI examples below assume that `python` refers to a prepared environment; with a project venv, substitute `.\.venv\Scripts\python.exe` or activate it first.

Open `http://127.0.0.1:8767`. The server binds only to loopback. The CLI defaults to port 8765 when no port is specified. Keep the terminal open while using the workbench and press Ctrl+C to stop it. Configuration management is under **Tools and settings** (工具与设置).

## What changed

- Four cybersecurity direction shortcuts by default; ten other templates stay under More directions. Create/edit/delete directions and venues in the UI with automatically assigned IDs, and choose whether each direction appears in the persistent shortcut list. An explicit keyword overrides preset query terms while selected directions still classify results.
- Data-driven registry of 56 initial venues. Ranking tags are optional and require a source, version and year. Configuration is distinct from live verification.
- Independent OpenAlex, Crossref, arXiv, DBLP and public publisher-catalog adapters. They implement search, normalization, health check and capability reporting. Source-specific compatibility functions remain available.
- Explicit quick/standard/comprehensive query plans. Comprehensive executes every selected domain keyword group; candidate and final-result limits remain visible.
- DOI/arXiv/DBLP/fingerprint identity, preserved version snapshots and non-destructive related-version candidates. arXiv remains enabled in formal venue searches, with uncertain affiliation shown for review.
- High/Medium/Low multi-label classification with evidence; low-confidence records and original source responses are retained.
- Indexed SQLite library, additive migrations with backups, full JSON/CSV/BibTeX export, source/task statistics, retry and overlapping incremental publication windows.
- Public PDF retrieval with streaming size limits, pypdf page validation, SHA-256, content reuse, skip-existing and recorded failures. No paywall/challenge bypass.

## CLI

```powershell
python -m autopapercrawl search --direction ai_ml --mode comprehensive --plan-only
python -m autopapercrawl search --direction encrypted_traffic --mode standard --download --output-dir "E:/Papers"
python -m autopapercrawl search --venue aaai --query "learning" --limit 30
python -m autopapercrawl library --title "traffic" --export papers.json
python -m autopapercrawl library --export references.bib
python -m autopapercrawl download --all --direction ai_security --output-dir "E:/Papers"
python -m autopapercrawl run --config config/task.example.json --plan-only
python -m autopapercrawl run --config config/task.example.json
python -m autopapercrawl retry JOB_ID
python -m autopapercrawl backup
```

`--direction`, `--venue` and `--id` are repeatable where applicable. Explicit title/keyword/venue searches without a direction are unrestricted. The saved-task runner returns exit code 2 on source warnings, allowing Windows Task Scheduler to report partial failures.

## Local files

- `config/domains.json`, `config/venues.json`: built-in templates; `config/security_topics.json`: legacy cybersecurity rule groups.
- `data/workbench/registries.json`: user overrides and local verification state; built-ins are not overwritten by UI edits.
- `data/workbench/library.sqlite3`: library and jobs; `backups/`: migration/deletion backups.
- `outputs/tasks/<job_id>/`: original records, plan/task report, JSON/CSV/BibTeX/HTML.
- Selected download folder: `pdfs/`, per-domain archives and `reports/<job_id>/`.

Optional `.env` settings: `PAPER_EMAIL` enables Unpaywall; `OPENALEX_API_KEY` is sent only as an Authorization header to OpenAlex. No key is needed for basic local functionality. See the [official OpenAlex authentication guide](https://help.openalex.org/api/authentication/). LLM settings are only for optional curation.

## Capabilities and limits

DBLP accepts `conf/<key>`, `journals/<key>` and official DBLP catalog/stream URLs. It uses structured JSON search, year filtering and offset pagination; no paperCrawler code was copied. The upstream checkout had no explicit source license, while [DBLP metadata is CC0](https://dblp.org/faq/1474677.html). Missing directories are explicit unsupported results.

**Implemented does not mean externally verified.** During local acceptance DBLP returned access-challenge pages and OpenAlex frequently returned 429. They are reported as unavailable, never empty successful crawls. ICML and JMLR could not be verified in that run. Crossref and arXiv provided real records, and selected public PDFs downloaded successfully. See [measured results and failure reasons](docs/delivery.zh-CN.md).

Publisher discovery is a bounded, explicit public catalog adapter using citation meta tags and robots.txt. It is not a universal publisher search engine and has not been verified against every publisher. ScienceDirect/CNKI/OnePetro native providers remain placeholders; generic public PDF URLs are a separate implemented route. Legacy SEG/GSW authorized-access implementations are retained but were not live-validated in this task.

Classification is automated triage, not a quality/relevance guarantee. Alias matches are probable venue evidence, not definitive proof. Fuzzy matches are review candidates, never automatic irreversible merges. Incremental windows use publication dates and DBLP years, not a guarantee of recovering arbitrarily late index updates; periodically run a full-year search. Capped/failed/cancelled tasks do not advance the automatic checkpoint.

## Tests and Windows scheduling

```powershell
python -m unittest discover -s tests -p "test_*.py"
# Optional browser acceptance, using installed Microsoft Edge:
python -m pip install playwright
python tests/browser_smoke.py
# Opt-in real network acceptance; saves results locally:
python scripts/verify_sources.py --limit 3 --pdf-sample 1
```

`requirements-security.txt` is optional for the external paperCrawler conference bridge. The existing reusable batch and curation entrypoints remain in `scripts/`; see the Chinese guide for their boundaries.

For a daily local task, copy `config/task.example.json` to your own file, review its plan, then preview the scheduler helper. Add `-Register` only when you want to register it:

```powershell
powershell -NoProfile -File scripts/windows_schedule.ps1 -PythonPath "E:/crawler4cert/.venv/Scripts/python.exe" -ConfigPath "config/task.example.json" -At "09:00"
```

PDFs, databases, credentials and generated reports remain ignored by Git. Source attribution to AutoPaperCrawl is retained.
