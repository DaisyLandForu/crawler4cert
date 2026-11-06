"""Extensible source registry; registering a source does not verify any venue."""
from .openalex import OpenAlexAdapter
from .crossref import CrossrefAdapter
from .arxiv import ArxivAdapter
from .dblp import DBLPAdapter
from .publisher import PublisherAdapter

ADAPTERS = {adapter.source_id: adapter for adapter in (OpenAlexAdapter, CrossrefAdapter, ArxivAdapter, DBLPAdapter, PublisherAdapter)}


def register_adapter(identifier, adapter_class):
    if not all(callable(getattr(adapter_class, name, None)) for name in ("search", "normalize", "health_check", "get_capabilities")):
        raise ValueError("Adapter interface incomplete")
    ADAPTERS[identifier] = adapter_class


def capability_matrix(registry):
    matrix = []
    for identifier, venue in registry.list("venues", enabled_only=True).items():
        entries = []
        for source in venue.get("adapters", []):
            implemented = source in ADAPTERS
            configured = source != "dblp" or bool(venue.get("dblp_key"))
            configured = configured and (source != "publisher" or bool(venue.get("publisher", {}).get("catalog_url")))
            status = "queryable" if implemented and configured else "not_implemented" if not implemented else "configured"
            verification = registry.verification(source, identifier)
            entries.append({"source": source, "status": verification.get("status", status) if verification else status,
                            "capabilities": ADAPTERS[source].capabilities if implemented else {}, "verification": verification})
        matrix.append({**venue, "capabilities": entries})
    return matrix
