"""Minimal interface shared by metadata and public publisher adapters."""
from __future__ import annotations


class UnsupportedCapability(ValueError):
    pass


class Adapter:
    source_id = ""
    capabilities = {"metadata": True, "venue_catalog": False, "preprint": False, "pdf": False}

    def __init__(self, client, registry=None):
        self.client = client
        self.registry = registry

    def search(self, query, filters):
        raise NotImplementedError

    def normalize(self, raw_record):
        raise NotImplementedError

    def get_capabilities(self):
        return {"source": self.source_id, **self.capabilities, "status": "queryable"}

    def health_check(self):
        try:
            result = self.search("machine learning", {"max_results": 1, "page_size": 1})
            return {"status": "verified" if result else "queryable", "count": len(result)}
        except Exception as exc:
            return {"status": "unavailable", "reason": str(exc)}
