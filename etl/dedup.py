"""Deduplication and heterogeneous complement alignment.

The pipeline uses two layers:

1. explicit same-``work_id`` + same-``kind`` resolution (Drive SRT beats the
   clectw plain transcript for the same episode);
2. 5-gram MinHash LSH candidate generation followed by an exact containment
   check (``|A & B| / min(|A|, |B|) >= 0.80``) within the same ``kind``.

Different kinds are never merged.  After deduplication, every surviving
document receives ``other_kinds`` describing the complementary forms that
exist under the same ``work_id``.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

NGRAM = 5
NUM_PERM = 24
BAND_ROWS = 3
BANDS = NUM_PERM // BAND_ROWS
CONTAINMENT_THRESHOLD = 0.80
MAX_SIGNATURE_SHINGLES = 2500
MASK = (1 << 64) - 1

TOKEN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaffA-Za-z0-9]+")
KIND_ORDER = ("doc", "transcript", "qa", "summary")


def _permutations(count: int = NUM_PERM) -> list[tuple[int, int]]:
    """Deterministic odd multipliers for MinHash permutations."""
    result: list[tuple[int, int]] = []
    state = 0x9E3779B97F4A7C15
    for _ in range(count):
        state = (state * 6364136223846793005 + 1442695040888963407) & MASK
        a = (state | 1) & MASK
        state = (state * 6364136223846793005 + 1442695040888963407) & MASK
        b = state & MASK
        result.append((a, b))
    return result


PERMS = _permutations()


def normalise_for_shingles(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(TOKEN.findall(text))


def shingles(text: str) -> set[int]:
    """5-gram shingles as stable 32-bit integers."""
    compact = normalise_for_shingles(text)
    if len(compact) < NGRAM:
        return {zlib.crc32(compact.encode("utf-8"))} if compact else set()
    return {
        zlib.crc32(compact[index:index + NGRAM].encode("utf-8"))
        for index in range(len(compact) - NGRAM + 1)
    }


def minhash_signature(gram_set: set[int]) -> tuple[int, ...]:
    values = list(gram_set)
    if len(values) > MAX_SIGNATURE_SHINGLES:
        # Deterministic subsample keeps LSH fast on hour-long transcripts.
        values = values[:: max(1, len(values) // MAX_SIGNATURE_SHINGLES)][:MAX_SIGNATURE_SHINGLES]
    signature: list[int] = []
    for a, b in PERMS:
        minimum = MASK
        for value in values:
            candidate = (value * a + b) & MASK
            if candidate < minimum:
                minimum = candidate
        signature.append(minimum)
    return tuple(signature)


def containment(left: set[int], right: set[int]) -> float:
    if not left or not right:
        return 0.0
    smaller = min(len(left), len(right))
    return len(left & right) / smaller


@dataclass
class DedupResult:
    kept: list[dict]
    removed: list[dict] = field(default_factory=list)
    same_work_merges: int = 0
    near_duplicate_merges: int = 0

    @property
    def before(self) -> int:
        return len(self.kept) + len(self.removed)


def _sort_key(doc: dict) -> tuple:
    return (
        -int(doc.get("priority", 0)),
        -len(doc.get("text", "")),
        doc.get("source_id", ""),
    )


def _prefer(left: dict, right: dict) -> dict:
    """Return the better of two documents under the source priority rules."""
    return right if _sort_key(right) < _sort_key(left) else left


def deduplicate_documents(
    docs: Iterable[dict],
    *,
    threshold: float = CONTAINMENT_THRESHOLD,
) -> DedupResult:
    """Merge same-work and near-duplicate documents of the same kind."""
    docs = [dict(doc) for doc in docs]
    removed: list[dict] = []
    same_work_merges = 0

    # Layer 1: same work_id + kind.  This is the explicit Drive-SRT-over-
    # clectw-transcript rule, applied before fuzzy matching.
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for doc in docs:
        if doc["kind"] == "transcript" and doc.get("source_type") in {
            "drive_transcripts", "clectw_transcripts", "asr_transcripts"
        }:
            grouped[(doc["work_id"], doc["kind"])].append(doc)
        else:
            grouped[("__independent__", doc["source_id"])].append(doc)
    stage_one: list[dict] = []
    for key, group in grouped.items():
        group.sort(key=_sort_key)
        winner = group[0]
        if len(group) > 1:
            winner["merged_source_ids"] = [
                doc["source_id"] for doc in group[1:]
            ]
            for doc in group[1:]:
                doc["duplicate_of"] = winner["source_id"]
                doc["duplicate_reason"] = "same_work_kind"
                removed.append(doc)
            same_work_merges += len(group) - 1
        stage_one.append(winner)

    # Layer 2: 5-gram MinHash LSH, grouped by kind so that a summary is never
    # merged into its transcript.
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for doc in stage_one:
        by_kind[doc["kind"]].append(doc)

    kept: list[dict] = []
    near_duplicate_merges = 0
    for kind in KIND_ORDER:
        group = by_kind.get(kind, [])
        if not group:
            continue
        group.sort(key=_sort_key)
        gram_sets: dict[str, set[int]] = {}
        buckets: dict[tuple[int, tuple[int, ...]], list[str]] = defaultdict(list)
        existing: list[dict] = []

        for doc in group:
            grams = shingles(doc.get("text", ""))
            gram_sets[doc["source_id"]] = grams
            if not grams:
                existing.append(doc)
                kept.append(doc)
                continue

            signature = minhash_signature(grams)
            candidates: set[str] = set()
            for band in range(BANDS):
                start = band * BAND_ROWS
                key = (band, signature[start:start + BAND_ROWS])
                candidates.update(buckets[key])
            match = None
            for other_id in candidates:
                other = next(d for d in existing if d["source_id"] == other_id)
                left_len = len(normalise_for_shingles(doc.get("text", "")))
                right_len = len(normalise_for_shingles(other.get("text", "")))
                if max(left_len, right_len) and min(left_len, right_len) / max(left_len, right_len) < 0.5:
                    continue
                overlap = len(grams & gram_sets[other_id])
                if overlap / max(1, len(grams | gram_sets[other_id])) < 0.45:
                    continue
                if containment(grams, gram_sets[other_id]) >= threshold:
                    match = other
                    break
            if match is not None:
                doc["duplicate_of"] = match["source_id"]
                doc["duplicate_of_work"] = match["work_id"]
                doc["duplicate_reason"] = "near_duplicate_5gram"
                doc["duplicate_containment"] = round(containment(grams, gram_sets[match["source_id"]]), 4)
                removed.append(doc)
                near_duplicate_merges += 1
                continue

            existing.append(doc)
            kept.append(doc)
            for band in range(BANDS):
                start = band * BAND_ROWS
                buckets[(band, signature[start:start + BAND_ROWS])].append(doc["source_id"])

    return DedupResult(
        kept=kept,
        removed=removed,
        same_work_merges=same_work_merges,
        near_duplicate_merges=near_duplicate_merges,
    )


def annotate_other_kinds(docs: Iterable[dict]) -> list[dict]:
    """Write complementary kinds for every document sharing a work_id."""
    docs = [dict(doc) for doc in docs]
    kinds_by_work: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        kinds_by_work[doc["work_id"]].add(doc["kind"])
    for doc in docs:
        others = kinds_by_work[doc["work_id"]] - {doc["kind"]}
        doc["other_kinds"] = [kind for kind in KIND_ORDER if kind in others]
    return docs


def complement_counts(docs: Iterable[dict]) -> dict:
    kinds_by_work: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        kinds_by_work[doc["work_id"]].add(doc["kind"])
    return {
        "works_with_transcript_and_summary": sum(
            1 for kinds in kinds_by_work.values()
            if "transcript" in kinds and "summary" in kinds
        ),
        "works_with_multiple_kinds": sum(
            1 for kinds in kinds_by_work.values() if len(kinds) > 1
        ),
        "works": len(kinds_by_work),
    }
