"""Persistent scan manifests and deterministic batch matching."""
from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import db

PREVIEW_LIMIT = 20


def create(directory: str, items: Iterable[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    values = list(items)
    scan_id = uuid.uuid4().hex
    db.save_manifest(scan_id, directory, values, metadata)
    counts = Counter(str(item.get("ext") or "(none)") for item in values if not item.get("is_dir"))
    return {
        "scan_id": scan_id,
        "total": len(values),
        "scanned_count": metadata.get("scanned_count", len(values)),
        "truncated": bool(metadata.get("truncated")),
        "reason": metadata.get("reason", "none"),
        "summary": dict(sorted(counts.items())),
        "preview": values[:PREVIEW_LIMIT],
    }


def load(scan_id: str) -> dict[str, Any]:
    value = db.get_manifest(scan_id)
    if value is None:
        raise ValueError(f"扫描清单不存在或已过期: {scan_id}")
    return value


def match(scan_id: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    manifest = load(scan_id)
    filters = filters or {}
    extensions = {str(v).lower().lstrip(".") for v in filters.get("extensions", [])}
    names = {str(v).casefold() for v in filters.get("names", [])}
    contains = str(filters.get("name_contains") or "").casefold()
    min_size = filters.get("min_size")
    max_size = filters.get("max_size")
    matched: list[dict[str, Any]] = []
    for item in manifest["items"]:
        if item.get("is_dir"):
            continue
        if extensions and str(item.get("ext") or "").lower() not in extensions:
            continue
        name = str(item.get("name") or "")
        if names and name.casefold() not in names:
            continue
        if contains and contains not in name.casefold():
            continue
        size = int(item.get("size") or 0)
        if min_size is not None and size < int(min_size):
            continue
        if max_size is not None and size > int(max_size):
            continue
        matched.append(item)
    return matched
