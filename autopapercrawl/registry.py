"""Data-driven built-ins and atomic, local-only user overrides."""
from __future__ import annotations

import json
import re
import threading
from uuid import uuid4
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = threading.RLock()


class Registry:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.local = self.root / "data" / "workbench" / "registries.json"

    def _local(self):
        return json.loads(self.local.read_text(encoding="utf-8")) if self.local.exists() else {"domains": {}, "venues": {}, "verification": {}}

    def list(self, kind, *, enabled_only=False):
        if kind not in {"domains", "venues"}:
            raise ValueError("Unknown registry kind")
        builtin = self.root / "config" / f"{kind}.json"
        if not builtin.exists():
            builtin = ROOT / "config" / f"{kind}.json"
        values = {item[f"{'domain' if kind == 'domains' else 'venue'}_id"]: item for item in json.loads(builtin.read_text(encoding="utf-8"))[kind]}
        values.update(self._local().get(kind, {}))
        result = {key: dict(value) for key, value in values.items() if not value.get("deleted") and (not enabled_only or value.get("enabled", True))}
        if kind == "domains":
            for record in result.values():
                record.setdefault("show_in_shortcuts", True)
        return result

    def save(self, kind, record):
        record = dict(record)
        if kind not in {"domains", "venues"}:
            raise ValueError("Unknown registry kind")
        id_field = "domain_id" if kind == "domains" else "venue_id"
        identifier = record.get(id_field) or f"custom_{uuid4().hex}"
        record[id_field] = identifier
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", identifier):
            raise ValueError("ID 必须为小写字母开头的字母、数字、下划线")
        name = "domain_name" if kind == "domains" else "name"
        if not str(record.get(name) or "").strip():
            raise ValueError("名称不能为空")
        list_fields = ("keywords", "excluded_keywords", "venues", "default_sources") if kind == "domains" else ("aliases", "research_domains", "openalex_source_ids", "issn", "isbn", "supported_years", "adapters", "ranking_tags")
        for field in list_fields:
            if not isinstance(record.get(field, []), list):
                raise ValueError(f"{field} 必须为数组")
            record.setdefault(field, [])
            if field != "ranking_tags" and any(not isinstance(value, (str, int)) for value in record[field]):
                raise ValueError(f"{field} 数组元素类型无效")
        if kind == "domains":
            record.setdefault("show_in_shortcuts", True)
            if not isinstance(record["show_in_shortcuts"], bool):
                raise ValueError("show_in_shortcuts 必须为布尔值")
            record.setdefault("description", "")
            record.setdefault("parent_domain", None)
            if record.get("parent_domain") == identifier:
                raise ValueError("领域不能作为自己的父领域")
            if not record["keywords"]:
                raise ValueError("至少填写一个检索关键词")
            parent = record.get("parent_domain")
            visited = {identifier}
            existing = self.list("domains")
            while parent:
                if parent in visited:
                    raise ValueError("父领域存在循环")
                if parent not in existing:
                    raise ValueError("父领域不存在")
                visited.add(parent)
                parent = existing[parent].get("parent_domain")
            if not record["default_sources"]:
                record["default_sources"] = ["openalex", "crossref", "arxiv", "dblp"]
        else:
            if record.get("venue_type") not in {"conference", "journal", "preprint", "workshop", "other"}:
                raise ValueError("会议/期刊类型无效")
            for tag in record["ranking_tags"]:
                if not isinstance(tag, dict) or not all(tag.get(key) for key in ("system", "source", "version", "year", "rank")):
                    raise ValueError("评级标签需要 system/source/version/year/rank")
            from .adapters.dblp import parse_dblp_key
            if record.get("dblp_key") or record.get("dblp_url"):
                record["dblp_key"] = parse_dblp_key(record.get("dblp_key") or record["dblp_url"])
            record.setdefault("capability_status", "configured")
        record["enabled"] = bool(record.get("enabled", True))
        if record.get("required_groups") is not None and (not isinstance(record["required_groups"], list) or any(not isinstance(group, list) or any(not isinstance(term, str) for term in group) for group in record["required_groups"])):
            raise ValueError("required_groups 需为关键词数组的数组")
        with LOCK:
            data = self._local()
            data.setdefault(kind, {})[identifier] = record
            self._write(data)
        return record

    def delete(self, kind, identifier):
        values = self.list(kind)
        if identifier not in values:
            raise ValueError("注册项不存在")
        record = {**values[identifier], "deleted": True, "enabled": False}
        with LOCK:
            data = self._local()
            data.setdefault(kind, {})[identifier] = record
            self._write(data)

    def _write(self, data):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        temp = self.local.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.local)

    def verification(self, source, venue="", result=None):
        key = source + ":" + (venue or "*")
        with LOCK:
            data = self._local()
            if result is not None:
                data.setdefault("verification", {})[key] = result
                self._write(data)
            return data.get("verification", {}).get(key)

    def venue(self, value):
        from .venues import normalize
        needle = normalize(value)
        return next((item for key, item in self.list("venues").items() if needle in {normalize(name) for name in [key, item["name"], item.get("abbreviation", ""), *item.get("aliases", [])]}), None)

    def topics(self):
        legacy_path = self.root / "config" / "security_topics.json"
        if not legacy_path.exists():
            legacy_path = ROOT / "config" / "security_topics.json"
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))["topics"]
        return {key: {**legacy.get(key, {}), **item, "label": item["domain_name"], "queries": item["keywords"]} for key, item in self.list("domains", enabled_only=True).items()}
