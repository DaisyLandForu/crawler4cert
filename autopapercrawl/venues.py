"""Conference/journal aliases shared by search, local filters and the UI."""
from __future__ import annotations

import re

VENUES = {
    "usenix": ["USENIX Security Symposium", "USENIX Security"],
    "sp": ["IEEE Symposium on Security and Privacy", "IEEE S&P", "IEEE SP"],
    "ccs": ["ACM SIGSAC Conference on Computer and Communications Security", "ACM CCS", "CCS"],
    "ndss": ["Network and Distributed System Security Symposium", "NDSS"],
    "tdsc": ["IEEE Transactions on Dependable and Secure Computing", "TDSC"],
    "tifs": ["IEEE Transactions on Information Forensics and Security", "TIFS"],
    "icse": ["International Conference on Software Engineering", "ICSE"],
    "issta": ["International Symposium on Software Testing and Analysis", "ISSTA"],
    "iclr": ["International Conference on Learning Representations", "ICLR"],
    "icml": ["International Conference on Machine Learning", "ICML"],
    "nips": ["Advances in Neural Information Processing Systems", "NeurIPS", "NIPS"],
    "cvpr": ["IEEE/CVF Conference on Computer Vision and Pattern Recognition", "CVPR"],
    "comnet": ["Computer Networks"],
    "cose": ["Computers & Security", "Computers and Security"],
    "jisa": ["Journal of Information Security and Applications"],
    "arxiv": ["arXiv", "arXiv preprint"],
}


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower().replace("&", " and ")))


def aliases(value: str) -> list[str]:
    key = value.lower().strip()
    for identifier, names in VENUES.items():
        if key == identifier or any(normalize(value) == normalize(name) for name in names):
            return [identifier, *names]
    return [value.strip()]


def canonical(value: str) -> str:
    names = aliases(value)
    return names[1] if len(names) > 1 else names[0]


def is_conference(value: str) -> bool:
    return aliases(value)[0] in {"usenix", "sp", "ccs", "ndss", "icse", "issta", "iclr", "icml", "nips", "cvpr"}


def venue_matches(actual: str, wanted: str) -> bool:
    actual = normalize(actual)
    if not actual:
        return False
    for name in aliases(wanted):
        name = normalize(name)
        if actual == name or re.search(r"(?:^| )" + re.escape(name) + r"(?: |$)", actual):
            return True
    return False


def paper_venues(paper) -> list[str]:
    metadata = paper.metadata
    values = [metadata.get("venue"), *metadata.get("venues", [])]
    if metadata.get("conference"):
        values.append(re.sub(r"\d{4}$", "", metadata["conference"]))
    return list(dict.fromkeys(value for value in values if value))
