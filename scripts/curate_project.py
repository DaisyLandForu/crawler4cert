#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.llm_utils import chat_json, load_dotenv
from autopapercrawl.topics import load_topics, security_score


DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
SECURITY_TOPICS = load_topics()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Curate papers from outputs/<project>/raw/* into outputs/<project>/curated.",
    )
    parser.add_argument("--project-dir", type=Path, required=True, help="Project directory, e.g. outputs/masw.")
    parser.add_argument("--topic", help="Topic label used in the curated report.")
    parser.add_argument("--output-dir", type=Path, help="Curated output directory. Defaults to project-dir/curated.")
    parser.add_argument("--top-n", type=int, default=15, help="Maximum papers to keep in curated DOI list.")
    parser.add_argument("--candidate-limit", type=int, default=60, help="Maximum candidates sent to the LLM.")
    parser.add_argument("--use-llm", action="store_true", help="Use an OpenAI-compatible chat API for curation.")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY", help="Environment variable containing the API key.")
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL), help="OpenAI-compatible base URL.")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL), help="Chat model name.")
    parser.add_argument("--language", default="zh", choices=["zh", "en"], help="Report language.")
    parser.add_argument("--skip-bibtex", action="store_true", help="Do not generate references.bib.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(ROOT / ".env")
    output_dir = args.output_dir or (args.project_dir / "curated")
    output_dir.mkdir(parents=True, exist_ok=True)

    topic = args.topic or infer_topic_from_raw(args.project_dir)
    candidates = load_candidates(args.project_dir)
    if not candidates:
        raise SystemExit(f"No candidates found under {args.project_dir}/raw/*/papers_table.json")

    ranked = rank_candidates(candidates)
    (output_dir / "candidates.json").write_text(json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.use_llm:
        selected, report = curate_with_llm(args, topic, ranked[: args.candidate_limit])
    else:
        selected = ranked[: args.top_n]
        report = render_rule_report(topic, selected, ranked, args.language)

    selected = selected[: args.top_n]
    dois = unique([paper.get("doi") for paper in selected if paper.get("doi")])

    (output_dir / "selected_papers.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "dois.txt").write_text("\n".join(dois) + ("\n" if dois else ""), encoding="utf-8")
    (output_dir / "report.md").write_text(report, encoding="utf-8")

    if dois and not args.skip_bibtex:
        run_bibtex(output_dir / "dois.txt", output_dir / "references.bib")

    print(f"project_dir: {args.project_dir}")
    print(f"candidates: {len(ranked)}")
    print(f"selected: {len(selected)}")
    print(f"dois: {len(dois)}")
    print(f"curated_dir: {output_dir}")
    print(f"report: {output_dir / 'report.md'}")
    return 0


def load_candidates(project_dir: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for run_dir in sorted((project_dir / "raw").glob("*")):
        path = run_dir / "papers.json"
        if not path.exists():
            path = run_dir / "papers_table.json"
        if not path.exists():
            continue
        run_name = path.parent.name
        try:
            papers = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"warning: failed to read {path}: {exc}", file=sys.stderr)
            continue
        for paper in papers:
            item = dict(paper)
            item["run"] = run_name
            candidates.append(item)
    return dedupe(candidates)


def infer_topic_from_raw(project_dir: Path) -> str:
    queries = []
    for path in sorted((project_dir / "raw").glob("*/query.txt")):
        text = path.read_text(encoding="utf-8").strip()
        if text and text not in queries:
            queries.append(text)

    if not queries:
        return project_dir.name
    if len(queries) == 1:
        return queries[0]

    project_label = project_dir.name.replace("_", " ")
    return f"{project_label}: " + " ; ".join(queries[:5])


def dedupe(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for paper in papers:
        key = paper.get("doi") or normalize_title(paper.get("title", ""))
        if not key:
            continue
        existing = seen.get(key)
        if not existing or score_paper(paper) > score_paper(existing):
            seen[key] = paper
    return list(seen.values())


def rank_candidates(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(papers, key=score_paper, reverse=True)


def score_paper(paper: dict[str, Any]) -> float:
    return security_score(paper, SECURITY_TOPICS)


def curate_with_llm(args: argparse.Namespace, topic: str, candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    prompt = build_llm_prompt(topic, candidates, args.top_n, args.language)
    try:
        payload = chat_json(
            system=(
                "You are a rigorous research assistant. Curate representative academic papers from metadata. "
                "Prioritize cybersecurity relevance and recent research, including satellite Internet security, "
                "software vulnerability detection, encrypted traffic analysis, and AI security. "
                "Do not penalize a recent preprint for lacking citations or a DOI. "
                "Return valid JSON only."
            ),
            user=prompt,
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            model=args.model,
            temperature=0.1,
            timeout=120,
        )
    except RuntimeError as exc:
        raise SystemExit(f"--use-llm was set, but {exc}") from exc
    selected_ids = [str(x) for x in payload.get("selected_ids", [])]
    selected = [candidates[int(identifier)] for identifier in dict.fromkeys(selected_ids) if identifier.isdigit() and int(identifier) < len(candidates)]
    selected_dois = [str(x).lower() for x in payload.get("selected_dois", [])]
    by_doi = {str(p.get("doi", "")).lower(): p for p in candidates if p.get("doi")}
    if not selected:
        selected = [by_doi[doi] for doi in selected_dois if doi in by_doi]
    if not selected:
        selected = candidates[: args.top_n]

    report = payload.get("report_md")
    if not report:
        report = render_rule_report(topic, selected, candidates, args.language)
    return selected, report


def build_llm_prompt(topic: str, candidates: list[dict[str, Any]], top_n: int, language: str) -> str:
    compact = [
        {
            "id": str(index),
            "title": p.get("title"),
            "abstract": (p.get("abstract") or "")[:1800],
            "directions": p.get("directions") or (p.get("metadata") or {}).get("directions", []),
            "year": p.get("year"),
            "doi": p.get("doi"),
            "citations": p.get("citations"),
            "oa_status": p.get("oa_status"),
            "url": p.get("url"),
            "run": p.get("run"),
        }
        for index, p in enumerate(candidates)
    ]
    lang_note = "Chinese" if language == "zh" else "English"
    return (
        f"Topic: {topic}\n"
        f"Select up to {top_n} representative papers.\n"
        "Group by satellite Internet security, vulnerability detection, encrypted traffic analysis, and AI security when appropriate. A paper may belong to multiple groups.\n"
        "For each selected paper explain briefly why it matters.\n"
        f"Write report_md in {lang_note} Markdown.\n"
        "Return JSON with exactly these keys: selected_ids (candidate id strings, including papers without a DOI), report_md.\n\n"
        f"Candidates JSON:\n{json.dumps(compact, ensure_ascii=False, indent=2)}"
    )


def render_rule_report(topic: str, selected: list[dict[str, Any]], candidates: list[dict[str, Any]], language: str) -> str:
    if language == "en":
        title = f"# Curated Papers: {topic}"
        intro = "Rule-based first pass. Use `--use-llm` for LLM-assisted curation."
        heading = "## Selected Papers"
    else:
        title = f"# 精选论文：{topic}"
        intro = "规则排序生成的第一版精选。配置 API key 后可用 `--use-llm` 生成 LLM 辅助精选报告。"
        heading = "## 优先阅读"

    lines = [title, "", intro, "", f"候选论文数：{len(candidates)}" if language == "zh" else f"Candidate papers: {len(candidates)}", "", heading, ""]
    for idx, paper in enumerate(selected, 1):
        bits = []
        if paper.get("year"):
            bits.append(str(paper["year"]))
        if paper.get("citations") is not None:
            bits.append(f"{paper['citations']} citations")
        if paper.get("doi"):
            bits.append(f"DOI: `{paper['doi']}`")
        lines.append(f"{idx}. {paper.get('title', 'Untitled')} ({'; '.join(bits)})")
        if paper.get("url"):
            lines.append(f"   - Link: {paper['url']}")
        if paper.get("run"):
            lines.append(f"   - Source run: `{paper['run']}`")
    lines.append("")
    return "\n".join(lines)


def run_bibtex(doi_path: Path, bibtex_path: Path) -> None:
    script = ROOT / "citation-management" / "scripts" / "doi_to_bibtex.py"
    if not script.exists():
        print("warning: citation-management DOI converter not found; BibTeX skipped", file=sys.stderr)
        return
    subprocess.run(
        [sys.executable, str(script), "--input", str(doi_path), "--output", str(bibtex_path), "--delay", "1.0"],
        cwd=ROOT,
        check=False,
    )


def unique(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def normalize_title(title: str) -> str:
    return " ".join(title.lower().strip().split())


if __name__ == "__main__":
    raise SystemExit(main())
