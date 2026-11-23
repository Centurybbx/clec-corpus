"""Version-2 corpus contract validation shared by build and audit."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = 2
KINDS = ("doc", "transcript", "qa", "summary")
QUALITIES = (1, 2, 3, 4)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUIRED_STRINGS = ("doc_id", "work_id", "source_id", "title", "body")
OPTIONAL_STRINGS = ("heading", "origin_url")
LICENSE_MODES = ("full", "snippet_only")


class SchemaError(ValueError):
    pass


def valid_strict_date(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def validate_passage(row: object) -> list[str]:
    """Return a list of contract violations for one corpus row."""
    errors: list[str] = []
    if not isinstance(row, dict):
        return ["row_not_object"]
    for key in REQUIRED_STRINGS:
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key}_missing_or_empty")
    if not isinstance(row.get("ordinal"), int) or isinstance(row.get("ordinal"), bool) or row.get("ordinal", -1) < 0:
        errors.append("ordinal_not_nonnegative_int")
    if row.get("kind") not in KINDS:
        errors.append("kind_invalid")
    if row.get("quality") not in QUALITIES:
        errors.append("quality_invalid")
    if not valid_strict_date(row.get("date")):
        errors.append("date_invalid")
    for key in ("start_sec", "end_sec"):
        value = row.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            errors.append(f"{key}_invalid")
    if row.get("start_sec") is not None and row.get("end_sec") is not None:
        if row["start_sec"] > row["end_sec"]:
            errors.append("time_range_inverted")
    if not isinstance(row.get("other_kinds"), list) or any(k not in KINDS for k in row.get("other_kinds", [])):
        errors.append("other_kinds_invalid")
    if row.get("license_mode") not in LICENSE_MODES:
        errors.append("license_mode_invalid")
    for key in OPTIONAL_STRINGS:
        if key in row and row[key] is not None and not isinstance(row[key], str):
            errors.append(f"{key}_not_string")
    if not isinstance(row.get("body"), str):
        errors.append("body_not_string")
    else:
        if "\x00" in row["body"]:
            errors.append("body_contains_nul")
        try:
            row["body"].encode("utf-8")
        except UnicodeEncodeError:
            errors.append("body_not_utf8")
    return errors


def iter_corpus(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise SchemaError(f"invalid JSONL at line {line_number}: {exc}") from exc


def audit_rows(rows: Iterable[dict]) -> dict:
    """Compute the hard contract metrics over an iterable of passages."""
    rows = list(rows)
    errors: list[str] = []
    doc_ids: Counter[str] = Counter()
    by_source: dict[str, list[int]] = defaultdict(list)
    kind_counts: Counter[str] = Counter()
    quality_counts: Counter[int] = Counter()
    year_counts: Counter[str] = Counter()
    length_ok = 0
    for index, row in enumerate(rows, 1):
        problems = validate_passage(row)
        for problem in problems:
            errors.append(f"row {index}: {problem}")
        if problems:
            continue
        doc_ids[row["doc_id"]] += 1
        by_source[row["source_id"]].append(row["ordinal"])
        kind_counts[row["kind"]] += 1
        quality_counts[int(row["quality"])] += 1
        if row["date"]:
            year_counts[row["date"][:4]] += 1
        if 100 <= len(row["body"]) <= 900:
            length_ok += 1

    duplicate_doc_ids = [doc_id for doc_id, count in doc_ids.items() if count > 1]
    if duplicate_doc_ids:
        errors.append(f"duplicate_doc_ids={len(duplicate_doc_ids)}")
    ordinal_errors = []
    for source_id, ordinals in by_source.items():
        if sorted(ordinals) != list(range(len(ordinals))):
            ordinal_errors.append(source_id)
    if ordinal_errors:
        errors.append(f"ordinal_gaps_or_duplicates={len(ordinal_errors)}")

    total = len(rows)
    return {
        "total": total,
        "errors": errors,
        "duplicate_doc_ids": duplicate_doc_ids,
        "ordinal_error_sources": ordinal_errors,
        "length_ok": length_ok,
        "length_ok_ratio": (length_ok / total) if total else 0.0,
        "by_kind": {kind: kind_counts.get(kind, 0) for kind in KINDS},
        "by_quality": {str(q): quality_counts.get(q, 0) for q in QUALITIES},
        "by_year": dict(sorted(year_counts.items())),
        "sources": len(by_source),
    }
