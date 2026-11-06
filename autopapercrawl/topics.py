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
    all_labels, labels = {}, {}
    for key, topic in topics.items():
        terms = list(dict.fromkeys([term for group in topic.get("required_groups", []) for term in group] + topic.get("keywords", topic.get("queries", []))))
        hits = []
        for term in terms:
            field = "title" if matches_term(title, normalized_term(term)) else "abstract" if matches_term(abstract, normalized_term(term)) else None
            if field:
                hits.append({"term": term, "field": field, "weight": 3 if field == "title" else 1})
        excluded = [word for word in topic.get("excluded_keywords", []) if matches_term(title + " " + abstract, normalized_term(word))]
        groups = topic.get("required_groups", [])
        grouped = [[hit for hit in hits if hit["term"] in group] for group in groups]
        covered = sum(bool(group) for group in grouped)
        coverage = covered / len(groups) if groups else 0
        phrase = any(matches_term(title, normalized_term(term)) for term in topic.get("keywords", topic.get("queries", [])))
        score = sum(hit["weight"] for hit in hits)
        if excluded or not hits:
            confidence = "low"
        elif groups:
            confidence = "high" if coverage >= 0.8 or phrase else "medium" if coverage >= 0.5 else "low"
        else:
            confidence = "high" if phrase or score >= 6 else "medium" if score >= 1 else "low"
        entry = {"label": topic.get("label", topic.get("domain_name", key)), "score": score, "confidence": confidence,
                 "evidence": grouped if groups else [hits], "matched_keywords": hits, "excluded_keywords": excluded, "rule_version": "weighted_topics_v2"}
        all_labels[key] = entry
        if confidence == "high":
            labels[key] = entry
    paper.metadata.update(directions=list(labels), classification=all_labels,
                          suggested_directions=[key for key, entry in all_labels.items() if entry["confidence"] == "medium"],
                          relevance_score=max((entry["score"] for entry in labels.values()), default=0), classification_method="weighted_topics_v2")
    paper.classification = all_labels
    return labels


def normalized_term(term):
    return " ".join(re.findall(r"\w+\*?", term.lower()))


def security_score(item: dict, topics: dict | None = None) -> float:
    """Recent, relevant cybersecurity papers outrank citation-heavy old papers."""
    topics = topics if topics is not None else load_topics()
    paper = Paper(title=item.get("title") or "", source=item.get("source") or "unknown", abstract=item.get("abstract") or (item.get("metadata") or {}).get("abstract"))
    labels = classify_paper(paper, topics)
    relevant = max((entry["score"] for entry in labels.values()), default=0)
    year = int(item.get("year") or 0)
    # Keep relevance first, then freshness; citation count contributes at most 10.
    return (1000 if labels else 0) + min(relevant, 9) * 30 + max(0, year - 2000) * 15 + min(int(item.get("citations") or 0), 100) / 10
