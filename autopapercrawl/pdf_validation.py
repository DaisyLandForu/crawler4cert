"""Bounded PDF validation and content identity, shared by all public routes."""
from __future__ import annotations

import hashlib
from pathlib import Path
from pypdf import PdfReader

DEFAULT_MAX_BYTES = 100 * 1024 * 1024


def inspect_pdf(path, max_bytes=DEFAULT_MAX_BYTES):
    path = Path(path)
    size = path.stat().st_size
    if not 0 < size <= max_bytes:
        raise ValueError(f"PDF 文件大小超出范围：{size} / {max_bytes} bytes")
    with path.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            raise ValueError("缺少 PDF 文件头")
        handle.seek(max(0, size - 65536))
        if b"%%EOF" not in handle.read():
            raise ValueError("PDF 缺少完整结束标记")
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF 加密，无法验证页面")
        pages = len(reader.pages)
        if not 0 < pages < 100000:
            raise ValueError("PDF 页面数量无效")
    except Exception as exc:
        raise ValueError(f"PDF 解析验证失败：{exc}") from exc
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "pages": pages, "bytes": size, "validation": "pypdf"}


def content_duplicate(path, info):
    for other in sorted(path.parent.glob("*.pdf")):
        if other == path or other.stat().st_size != info["bytes"]:
            continue
        try:
            if inspect_pdf(other)["sha256"] == info["sha256"]:
                return other
        except (ValueError, OSError):
            continue
    return None
