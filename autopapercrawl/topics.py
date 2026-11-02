"""Auditable, multi-label topic filtering over titles and abstracts."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Paper


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "security_topics.json"


def load_topics(path: Path = DEFAULT_CONFIG) -> dict:
    topics = json.loads(path.read_text(encoding="utf-8"))["topics"]
    for key, topic in topics.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise ValueError(f"Invalid topic directory name: {key}")
        if not topic.get("queries") or not topic.get("required_groups") or any(not group for group in topic["required_groups"]):
            raise ValueError(f"Topic {key} needs queries and non-empty required_groups")
    return topics


def normalized_text(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower(), flags=re.UNICODE))


def matches_term(text: str, term: str) -> bool:
    # Only trailing '*' in a word expands; phrases tolerate punctuation/hyphens.
    words = term.lower().split()
    pattern = r"\s+".join(re.escape(word).replace(r"\*", r"\w*") for word in words)
    return bool(re.search(r"(?<!\w)" + pattern + r"(?!\w)", text))


def classify_paper(paper: Paper, topics: dict) -> dict:
    title = normalized_text(paper.title)
    abstract = normalized_text(paper.abstract or "")
    labels = {}
    for key, topic in topics.items():
        evidence = []
        for group in topic["required_groups"]:
            hits = []
            for term in group:
                if matches_term(title, term):
                    hits.append({"term": term, "field": "title", "weight": 3})
                elif matches_term(abstract, term):
                    hits.append({"term": term, "field": "abstract", "weight": 1})
            if not hits:
                break
            evidence.append(hits)
        if len(evidence) == len(topic["required_groups"]):
            score = sum(max(hit["weight"] for hit in group) for group in evidence)
            labels[key] = {"label": topic["label"], "score": score, "evidence": evidence}
    paper.metadata["directions"] = list(labels)
    paper.metadata["classification"] = labels
    paper.metadata["relevance_score"] = max((item["score"] for item in labels.values()), default=0)
    paper.metadata["classification_method"] = "title_abstract_rules_v1"
    return labels


def security_score(item: dict, topics: dict | None = None) -> float:
    """Recent, relevant cybersecurity papers outrank citation-heavy old papers."""
    topics = topics if topics is not None else load_topics()
    paper = Paper(title=item.get("title") or "", source=item.get("source") or "unknown", abstract=item.get("abstract") or (item.get("metadata") or {}).get("abstract"))
    labels = classify_paper(paper, topics)
    relevant = max((entry["score"] for entry in labels.values()), default=0)
    year = int(item.get("year") or 0)
    # Keep relevance first, then freshness; citation count contributes at most 10.
    return (1000 if labels else 0) + min(relevant, 9) * 30 + max(0, year - 2000) * 15 + min(int(item.get("citations") or 0), 100) / 10
