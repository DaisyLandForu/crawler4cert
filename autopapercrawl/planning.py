"""The same explicit query plan is previewed, persisted, and executed."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json

MODES = {"quick": 1, "standard": 3, "comprehensive": 0}


def build_plan(options, topics, source_states=None, checkpoint=None):
    mode = options.get("mode", "standard")
    if mode not in MODES:
        raise ValueError("模式应为 quick、standard 或 comprehensive")
    limit = MODES[mode]
    explicit = options.get("query") or options.get("title")
    if explicit:
        queries = [explicit]
    else:
        queries = list(dict.fromkeys(query for identifier in options.get("directions", []) for query in topics[identifier]["queries"][:limit or None])) or [""]
    entries = options.get("retry_plan") or [{"query": query, "venue": venue, "sources": list(options["sources"])} for query in queries for venue in options.get("venues") or [None]]
    identity = {"entries": entries, "years": [options["start_year"], options["end_year"]], "title": options.get("title"), "directions": sorted(options.get("directions", []))}
    plan_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    since = options.get("since") or None
    if options.get("incremental") and not since and checkpoint:
        since = (date.fromisoformat(checkpoint[:10]) - timedelta(days=int(options.get("overlap_days", 7)))).isoformat()
    if since:
        if date.fromisoformat(since) > date.today():
            raise ValueError("增量起始日期不能晚于今天")
        since = max(since, str(options["start_year"]) + "-01-01")
        if since > str(options["end_year"]) + "-12-31":
            raise ValueError("增量起始日期不在所选年份范围内")
    return {"mode": mode, "explicit_query": bool(explicit), "queries": queries, "entries": entries,
            "plan_key": plan_key, "checkpoint": checkpoint,
            "query_count": len(queries), "source_calls": sum(len(entry["sources"]) for entry in entries),
            "start_year": options["start_year"], "end_year": options["end_year"], "since": since,
            "per_query": options["per_query"], "max_results": options["max_results"], "all_topic_queries": mode == "comprehensive" and not explicit}
