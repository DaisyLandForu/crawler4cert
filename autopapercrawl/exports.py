"""Lossless JSON, readable CSV and escaped BibTeX exports."""
from __future__ import annotations

import csv
import io
import json
import re

FIELDS = [("id", "论文ID"), ("paper_id", "原始论文ID"), ("title", "论文标题"), ("authors", "作者"), ("affiliations", "机构"), ("year", "年份"),
          ("direction_labels", "研究方向"), ("venues", "会议期刊"), ("venue_type", "出处类型"), ("doi", "DOI"), ("arxiv_id", "arXiv ID"),
          ("dblp_id", "DBLP ID"), ("abstract", "摘要"), ("keywords", "关键词"), ("categories", "类别"), ("concepts", "概念"),
          ("citations", "引用数量"), ("code_url", "代码地址"), ("pdf_url", "PDF地址"), ("url", "原文地址"), ("sources", "来源"),
          ("publication_status", "发表状态"), ("classification", "分类与置信度"), ("status_label", "下载状态"), ("status_detail", "说明"),
          ("local_path", "PDF 路径"), ("versions", "版本信息"), ("metadata", "完整扩展元数据")]


def export_csv(records):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([label for _, label in FIELDS])
    for record in records:
        values = []
        for key, _ in FIELDS:
            value = record.get(key)
            if isinstance(value, (list, dict)):
                value = "；".join(value) if isinstance(value, list) and all(isinstance(v, str) for v in value) else json.dumps(value, ensure_ascii=False)
            if str(value or "").lstrip().startswith(("=", "+", "-", "@")) or str(value or "").startswith(("\t", "\r")):
                value = "'" + str(value)
            values.append(value)
        writer.writerow(values)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def export_json(records):
    return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")


def export_bibtex(records):
    def escape(value):
        return str(value or "").replace("\\", r"\textbackslash{}").replace("{", r"\{").replace("}", r"\}").replace("%", r"\%").replace("&", r"\&").replace("_", r"\_")
    entries = []
    for index, record in enumerate(records):
        key = re.sub(r"[^a-zA-Z0-9]", "", str(record.get("id") or record.get("paper_id") or "paper" + str(index)))
        meta = record.get("metadata") or {}
        kind = record.get("publication_status") or meta.get("publication_type")
        category = "article" if kind in {"journal", "journal-article", "Journal Article"} else "inproceedings" if kind in {"conference", "proceedings-article"} else "misc"
        venue = "；".join(record.get("venues") or []) or record.get("venue") or meta.get("venue")
        fields = {"title": record.get("title"), "author": " and ".join(record.get("authors") or []), "year": record.get("year"),
                  "doi": record.get("doi"), "url": record.get("url"), "journal" if category == "article" else "booktitle": venue,
                  "eprint": record.get("arxiv_id") or meta.get("arxiv_id")}
        entries.append("@" + category + "{" + key + ",\n" + ",\n".join("  " + name + " = {" + escape(value) + "}" for name, value in fields.items() if value) + "\n}")
    return ("\n\n".join(entries) + "\n").encode("utf-8")
