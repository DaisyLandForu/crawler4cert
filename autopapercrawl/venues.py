"""Conference/journal aliases shared by search, local filters and the UI."""
from __future__ import annotations

import re

from .registry import Registry

VENUES = {key: [item["name"], *item["aliases"]] for key, item in Registry().list("venues").items()}


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower().replace("&", " and ")))


def aliases(value: str, registry=None) -> list[str]:
    key = value.lower().strip()
    entries = {k: [v["name"], *v["aliases"]] for k, v in (registry or Registry()).list("venues").items()}
    for identifier, names in entries.items():
        if key == identifier or any(normalize(value) == normalize(name) for name in names):
            return [identifier, *names]
    return [value.strip()]


def canonical(value: str) -> str:
    names = aliases(value)
    return names[1] if len(names) > 1 else names[0]


def is_conference(value: str) -> bool:
    item = Registry().venue(value)
    return bool(item and item["venue_type"] in {"conference", "workshop"})


def venue_matches(actual: str, wanted: str, registry=None) -> bool:
    actual = normalize(actual)
    if not actual:
        return False
    for name in aliases(wanted, registry):
        name = normalize(name)
        if actual == name or (len(name) > 3 and re.search(r"(?:^| )" + re.escape(name) + r"(?: |$)", actual)):
            return True
    return False


def paper_venues(paper) -> list[str]:
    metadata = paper.metadata
    values = [metadata.get("venue"), *metadata.get("venues", [])]
    if metadata.get("conference"):
        values.append(re.sub(r"\d{4}$", "", metadata["conference"]))
    return list(dict.fromkeys(value for value in values if value))


def match_paper_venue(paper, wanted, registry=None):
    registry = registry or Registry()
    item = registry.venue(wanted) if isinstance(wanted, str) else wanted
    identifier = item["venue_id"] if item else str(wanted)
    evidence = []
    if item:
        for field in ("issn", "isbn", "openalex_source_ids"):
            norm = lambda value: re.sub(r"[^a-z0-9]", "", str(value).lower().rsplit("/", 1)[-1])
            if set(map(norm, paper.metadata.get(field, []))) & set(map(norm, item.get(field, []))):
                evidence.append(field)
        if paper.metadata.get("dblp_stream") and paper.metadata["dblp_stream"] == item.get("dblp_key"):
            evidence.append("dblp_stream")
    status = "confirmed" if evidence else "probable" if any(venue_matches(actual, identifier, registry) for actual in paper_venues(paper)) else "unconfirmed"
    if status == "probable":
        evidence.append("name_or_alias")
        formal_sources = [record.get("source", "").lower() for record in paper.metadata.get("raw_records", [])]
        if paper.doi and paper.arxiv_id and any(source in {"crossref", "openalex", "dblp"} for source in formal_sources):
            evidence.append("doi_publication_link")
            status = "confirmed"
    result = {"venue_id": identifier, "status": status, "evidence": evidence}
    paper.metadata.setdefault("venue_matches", {})[identifier] = result
    return result
