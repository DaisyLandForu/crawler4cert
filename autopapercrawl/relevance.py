"""Small, explicit bilingual query normalization and auditable text checks."""
from __future__ import annotations

import re
import unicodedata


# This is a maintained vocabulary for the workbench's security presets, not a
# general-purpose translator. Unknown Chinese text is kept, never dropped.
TRANSLATIONS = {
    "网络安全威胁分析与防护": "cyber threat analysis protection",
    "卫星互联网安全": "satellite internet security",
    "加密流量分析检测": "encrypted traffic analysis detection",
    "加密流量分析": "encrypted traffic analysis",
    "恶意流量检测": "malicious traffic detection",
    "恶意流量": "malicious traffic", "加密流量": "encrypted traffic",
    "网络流量": "network traffic", "漏洞检测": "vulnerability detection",
    "漏洞挖掘": "vulnerability discovery", "人工智能安全": "AI security",
    "威胁情报": "threat intelligence", "入侵检测": "intrusion detection",
    "网站指纹": "website fingerprinting", "提示注入": "prompt injection",
    "数据投毒": "data poisoning", "后门攻击": "backdoor attack",
    "对抗样本": "adversarial examples", "联邦学习": "federated learning",
    "网络安全": "cybersecurity", "数据安全": "data security",
    "流量分析": "traffic analysis", "恶意软件": "malware",
    "大语言模型": "large language model", "大模型": "large language model",
    "安全": "security", "检测": "detection", "分析": "analysis",
    "防护": "protection", "监测": "monitoring", "与": "and",
}
STOP_WORDS = set("a an the of for to in on with and or by from using via based study approach framework method toward towards".split())
ALIASES = {
    "malicious": ("malicious", "malware", "attack*", "intrusion*", "botnet*", "恶意", "攻击"),
    "traffic": ("traffic", "flow*", "流量"),
    "encrypted": ("encrypt*", "tls", "https", "quic", "加密"),
    "detection": ("detect*", "identif*", "classif*", "recogn*", "检测", "识别"),
    "classification": ("classif*", "identif*", "recogn*", "fingerprint*", "分类", "识别"),
    "security": ("secur*", "cyber*", "attack*", "threat*", "vulnerab*", "安全", "攻击"),
    "satellite": ("satellit*", "starlink", "卫星"),
    "internet": ("internet", "network*", "broadband", "互联网", "网络"),
    "analysis": ("analys*", "analyz*", "分析"),
    "defense": ("defen*", "protect*", "mitigat*", "防御", "防护"),
    "vulnerability": ("vulnerab*", "漏洞"),
}


def normalize_query(query):
    value = unicodedata.normalize("NFKC", query).strip()
    for term in sorted(TRANSLATIONS, key=len, reverse=True):
        value = value.replace(term, " " + TRANSLATIONS[term] + " ")
    return " ".join(value.split())


def query_match(paper, query):
    """Require every meaningful term (or known synonym) in title/abstract.

    Source rank, venue name and a matching author are not relevance evidence.
    Raw candidates remain archived separately when this conservative check fails.
    """
    query = normalize_query(query).casefold()
    terms = list(dict.fromkeys(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", query)))
    terms = [term for term in terms if term not in STOP_WORDS]
    text = unicodedata.normalize("NFKC", paper.title + " " + (paper.abstract or "")).casefold()
    matched, missing = [], []
    for term in terms:
        patterns = ALIASES.get(term, (term,))
        def matches(pattern):
            if re.search(r"[\u4e00-\u9fff]", pattern):
                return pattern in text
            return bool(re.search(r"(?<![a-z0-9])" + re.escape(pattern).replace(r"\*", r"[a-z0-9]*") + r"(?![a-z0-9])", text))
        (matched if any(matches(pattern) for pattern in patterns) else missing).append(term)
    return {"matched": not missing, "query": query, "matched_terms": matched, "missing_terms": missing,
            "rule": "title_abstract_terms_v1"}
