"""Conservative identity merging and reversible publication-version links."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from .models import normalize_doi, normalize_title


def title_fingerprint(paper):
    title = " ".join(re.findall(r"\w+", paper.title.lower()))
    author = normalize_title(paper.authors[0]) if paper.authors else ""
    return title, paper.year, author


def identifiers(paper):
    values = []
    if paper.doi:
        values.append("doi:" + normalize_doi(paper.doi))
    arxiv = paper.arxiv_id or paper.metadata.get("arxiv_id")
    if arxiv:
        values.append("arxiv:" + re.sub(r"v\d+$", "", arxiv))
    dblp = paper.dblp_id or paper.metadata.get("dblp_id")
    if dblp:
        values.append("dblp:" + dblp)
    return values


def same_identity(a, b):
    if a.doi and b.doi and normalize_doi(a.doi) != normalize_doi(b.doi):
        return False
    if set(identifiers(a)) & set(identifiers(b)):
        return True
    af, bf = title_fingerprint(a), title_fingerprint(b)
    # Same title with contradictory years/authors is not an identity match.
    return af == bf and bool(af[0])


def snapshot(paper):
    return {"key": paper.key, "doi": paper.doi, "arxiv_id": paper.arxiv_id or paper.metadata.get("arxiv_id"),
            "dblp_id": paper.dblp_id or paper.metadata.get("dblp_id"), "title": paper.title, "authors": paper.authors,
            "year": paper.year, "source": paper.source, "venue": paper.venue or paper.metadata.get("venue"),
            "publication_status": paper.publication_status, "pdf_url": paper.pdf_url, "url": paper.url,
            "affiliations": paper.affiliations, "code_url": paper.code_url, "keywords": paper.keywords,
            "categories": paper.categories, "concepts": paper.concepts, "citations": paper.citations,
            "metadata": {key: value for key, value in paper.metadata.items() if key not in {"versions", "related_versions", "raw_records", "classification"}}}


def version_snapshots(*papers):
    result, seen = [], set()
    for paper in papers:
        for value in [*paper.versions, *paper.metadata.get("versions", []), snapshot(paper)]:
            key = (value.get("key"), value.get("source"), value.get("pdf_url"), value.get("venue"))
            if key not in seen:
                seen.add(key)
                result.append(value)
    return result


def relate_versions(papers):
    for index, a in enumerate(papers):
        for b in papers[index + 1:]:
            if same_identity(a, b):
                continue
            at, ay, aa = title_fingerprint(a)
            bt, by, ba = title_fingerprint(b)
            ratio = SequenceMatcher(None, at, bt).ratio()
            if ratio < 0.9 or not aa or aa != ba or not ay or not by or abs(ay - by) > 2:
                continue
            confirmed = at == bt
            relation = "publication_version" if confirmed else "possible_related_work"
            for source, target in ((a, b), (b, a)):
                entry = {"key": target.key, "paper_id": target.paper_id, "doi": target.doi, "venue": target.venue or target.metadata.get("venue"),
                         "pdf_url": target.pdf_url, "relation": relation, "status": "linked" if confirmed else "review_required", "similarity": round(ratio, 3)}
                rows = source.metadata.setdefault("related_versions", [])
                if entry not in rows:
                    rows.append(entry)
    return papers
