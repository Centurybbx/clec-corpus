"""CLEC corpus ETL entry point.

Usage:
    python3 -m etl.build_corpus \
        --source-dir /Users/century/Vibing/clec/collection \
        --out-dir dist

The collection directory is only ever opened for reading.  Raw text is never
copied into this repository; all output goes to the gitignored ``dist/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from . import chunk, clean, dedup, hallucination, schema

BLOCKED_WORKS = ("ep-00176", "ep-00260", "ep-00295")
DEFAULT_BLOCKED_WORKS = BLOCKED_WORKS
KIND_QUALITY = {"doc": 1, "transcript": 2, "qa": 2, "summary": 3}
KIND_ORDER = ("doc", "transcript", "qa", "summary")
CURRENT_PACK_VERSION = date.today().isoformat()

KNOWN_GAPS = [
    "ep-00176 / ep-00260 / ep-00295 因严重语音转写幻觉已隔离剔除，请回看原片。",
    "自动逐字稿与网页摘要均未经逐字人工校对，关键数字与观点请回溯原始出处。",
    "sources/podcast 仅用于补全节目日期与链接元数据，未作为正文入库。",
]

SOURCE_LABELS = {
    "drive_text": "Drive 正文文档",
    "local_text": "本地讲义/电子表格",
    "clec_md": "clec-md 知识库与文章",
    "drive_transcripts": "Drive SRT 逐字稿",
    "clectw_transcripts": "clectw 逐字稿",
    "clectw_summaries": "clectw 节目摘要",
    "asr_transcripts": "本地 ASR / Podcast 逐字稿",
    "cfnavi": "cfnavi 摘要",
    "clec168_blog": "clec168 博客",
    "clec168_docs": "clec168 书籍/文档",
    "clec168_presentations": "clec168 简报",
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def safe_slug(value: str, limit: int = 80) -> str:
    value = clean.normalize_title(value)
    value = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "-", value).strip("-").lower()
    return value[:limit] or hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def short_hash(value: str, length: int = 8) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def is_blocked(work_id: str) -> bool:
    return (work_id or "").lower() in BLOCKED_WORKS


def doc_priority(source_type: str) -> int:
    return {
        "drive_transcripts": 100,
        "asr_transcripts": 80,
        "drive_text": 70,
        "local_text": 65,
        "clectw_summaries": 60,
        "clec_md": 60,
        "clec168_blog": 60,
        "clec168_docs": 60,
        "clec168_presentations": 60,
        "clectw_transcripts": 55,
        "cfnavi": 55,
    }.get(source_type, 50)


def make_doc(
    *,
    work_id: str,
    source_id: str,
    title: str,
    kind: str,
    text: str,
    source_type: str,
    source_path: Path,
    source_dir: Path,
    date_value: str | None = None,
    heading: str = "",
    origin_url: str = "",
    cues: list | None = None,
    rules: list[str] | None = None,
    raw_before: str = "",
    priority: int | None = None,
) -> dict:
    normalized_date = clean.normalize_date(date_value)
    capture_date = clean.capture_date_from_text(raw_before)
    if normalized_date and capture_date and normalized_date == capture_date:
        normalized_date = None
    return {
        "work_id": work_id,
        "source_id": source_id,
        "title": title or work_id,
        "kind": kind,
        "quality": KIND_QUALITY[kind],
        "date": normalized_date,
        "text": text,
        "heading": heading or "",
        "start_sec": None,
        "end_sec": None,
        "cues": cues,
        "origin_url": origin_url or "",
        "source_type": source_type,
        "source_path": relpath(source_path, source_dir),
        "priority": priority if priority is not None else doc_priority(source_type),
        "rules": list(dict.fromkeys(rules or [])),
        "raw_before": raw_before,
    }


# ---------------------------------------------------------------------------
# Source policy
# ---------------------------------------------------------------------------

def load_policy() -> dict:
    path = Path(__file__).resolve().parent.parent / "config/source_policy.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


_SOURCE_POLICY = load_policy()
BLOCKED_WORKS = tuple(
    (_SOURCE_POLICY.get("sources") or {})
    .get("quarantine", {})
    .get("blocked_works")
    or DEFAULT_BLOCKED_WORKS
)


# ---------------------------------------------------------------------------
# clectw registry
# ---------------------------------------------------------------------------

class ClectwIndex:
    def __init__(self, root: Path):
        self.root = root
        self.text_dir = root / "web/clectw/text/ep"
        self.transcript_dir = root / "web/clectw/transcripts"
        self.meta: dict[str, dict] = {}
        self.stems: set[str] = set()
        self.transcript_texts: dict[str, str] = {}
        self._shingles: dict[str, set[int]] | None = None
        self._scan()

    def _scan(self) -> None:
        for path in sorted(self.text_dir.glob("*.txt")):
            stem = path.stem.lower()
            self.stems.add(stem)
            raw = read_text(path)
            header = clean.parse_clectw_header(raw)
            header["origin_url"] = f"https://clectw.com/ep/{stem}"
            self.meta[stem] = header
        for path in sorted(self.transcript_dir.glob("*.txt")):
            stem = path.stem.lower()
            self.stems.add(stem)
            self.transcript_texts[stem] = read_text(path)

    @property
    def shingles(self) -> dict[str, set[int]]:
        if self._shingles is None:
            self._shingles = {
                stem: dedup.shingles(text)
                for stem, text in self.transcript_texts.items()
                if not is_blocked(stem)
            }
        return self._shingles

    def title_for(self, stem: str) -> str:
        meta = self.meta.get(stem, {})
        return meta.get("title") or stem

    def date_for(self, stem: str) -> str | None:
        meta = self.meta.get(stem, {})
        return meta.get("date")

    def duration_for(self, stem: str) -> int | None:
        meta = self.meta.get(stem, {})
        return meta.get("duration_sec")

    def origin_for(self, stem: str) -> str:
        meta = self.meta.get(stem, {})
        return meta.get("origin_url", f"https://clectw.com/ep/{stem}")


NUMBER_TOKEN = re.compile(r"(?<!\d)(\d{4,6})([A-Za-z])?(?!\d)")
EXPLICIT_NUMBER_PATTERNS = (
    re.compile(r"【\s*(\d{4,6})([A-Za-z])?\s*】"),
    re.compile(r"[（(]\s*(\d{4,6})([A-Za-z])?\s*[）)]"),
    re.compile(r"(?i)(?:第|ep|集)\s*(\d{4,6})([A-Za-z])?"),
    re.compile(r"(?:^|[\s，,。；;])(\d{4,6})([A-Za-z])?(?=[\s【（(『「，,。；;]|$)"),
)


def _is_year(number: str) -> bool:
    return len(number) == 4 and 1990 <= int(number) <= 2035


def _identifier(number: str, suffix: str = "") -> str:
    if len(number) == 4:
        number = number.zfill(5)
    return number + (suffix or "").lower()


def _work_hints(text: str) -> list[str]:
    lower = text.lower()
    hints: list[str] = []
    if "短篇" in text or "短片" in text or "short" in lower:
        hints.append("short")
    if "漫談" in text or "漫谈" in text or "talk" in lower:
        hints.append("talk")
    if "緣起" in text or "缘起" in text:
        hints.append("origin")
    if "閒聊" in text or "闲聊" in text or "chat" in lower:
        hints.append("chat")
    if "專題" in text or "专题" in text or "special" in lower:
        hints.append("special")
    hints.append("ep")
    return hints


def _stem_candidates(stem_set: set[str], identifier: str) -> list[str]:
    """Match ``-<id>`` and split-episode suffixes such as ``ep-00480a``."""
    escaped = re.escape(identifier.lower())
    pattern = re.compile(rf"-{escaped}[a-z]?$")
    return sorted(stem for stem in stem_set if pattern.search(stem.lower()))


def infer_work_id(
    title: str,
    *,
    stem_set: set[str],
    fallback: str,
    allow_unique_fallback: bool = True,
) -> str:
    """Map a document title to a clectw work stem when possible.

    Four-digit years (1990-2035) are never treated as episode numbers.  A bare
    number is only turned into ``ep-xxxxx`` when the title has explicit episode
    context (``【00694】``, ``第 123 集``, ``EP123``, a leading ID column, or a
    parenthesised number).  Otherwise the caller's stable fallback ID wins.
    """
    text = clean.normalize_title(title)
    lower = text.lower()

    identifiers: list[str] = []
    for match in NUMBER_TOKEN.finditer(text):
        number, suffix = match.group(1), (match.group(2) or "")
        if _is_year(number):
            continue
        identifier = _identifier(number, suffix)
        if identifier not in identifiers:
            identifiers.append(identifier)

    hints = _work_hints(text)
    for identifier in identifiers:
        for hint in hints:
            candidate = f"{hint}-{identifier}"
            if candidate in stem_set:
                return candidate
        candidates = _stem_candidates(stem_set, identifier)
        if len(candidates) == 1 and allow_unique_fallback:
            return candidates[0]

    if ("貼文" in text or "post" in lower) and identifiers:
        return f"post-{identifiers[0]}"

    for pattern in EXPLICIT_NUMBER_PATTERNS:
        for match in pattern.finditer(text):
            number, suffix = match.group(1), (match.group(2) or "")
            if _is_year(number):
                continue
            identifier = _identifier(number, suffix)
            for hint in hints:
                candidate = f"{hint}-{identifier}"
                if candidate in stem_set:
                    return candidate
            return f"ep-{identifier}"

    return fallback


def best_match(
    query_grams: set[int],
    candidates: dict[str, set[int]],
    *,
    minimum: float = 0.08,
) -> tuple[str | None, float]:
    best_stem = None
    best_score = 0.0
    for stem, grams in candidates.items():
        if not grams:
            continue
        score = len(query_grams & grams) / max(1, len(query_grams | grams))
        if score > best_score:
            best_stem, best_score = stem, score
    if best_score < minimum:
        return None, best_score
    return best_stem, best_score


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------

class Builder:
    def __init__(self, source_dir: Path):
        self.root = source_dir
        self.docs: list[dict] = []
        self.stats: dict[str, dict] = defaultdict(
            lambda: {
                "files": 0,
                "raw_chars": 0,
                "docs_before_dedup": 0,
                "docs_after_dedup": 0,
                "skipped": 0,
                "passages": 0,
                "passage_chars": 0,
            }
        )
        self.skipped_blocked = 0
        self.superseded_versions = 0
        self.template_examples: list[dict] = []
        self.clectw = ClectwIndex(source_dir)
        self.policy = load_policy()
        self.podcast_meta = self._load_podcast_metadata()

    def enabled(self, source_key: str) -> bool:
        source = (self.policy.get("sources") or {}).get(source_key, {})
        return bool(source.get("enabled", True))

    # -- helpers ---------------------------------------------------------

    def add(self, doc: dict, *, raw_chars: int = 0) -> None:
        source_type = doc["source_type"]
        stats = self.stats[source_type]
        stats["files"] += 1
        stats["raw_chars"] += raw_chars
        if is_blocked(doc["work_id"]):
            self.skipped_blocked += 1
            stats["skipped"] += 1
            return
        if not doc["text"] and not doc.get("cues"):
            stats["skipped"] += 1
            return
        if doc["text"] and len(doc["text"]) < 40 and not doc.get("cues"):
            stats["skipped"] += 1
            return
        self.docs.append(doc)
        stats["docs_before_dedup"] += 1

    def skip(self, source_type: str, *, raw_chars: int = 0, blocked: bool = False) -> None:
        stats = self.stats[source_type]
        stats["files"] += 1
        stats["raw_chars"] += raw_chars
        stats["skipped"] += 1
        if blocked:
            self.skipped_blocked += 1

    def collect_example(self, doc: dict) -> None:
        if not doc.get("rules") or not doc.get("raw_before"):
            return
        after = doc.get("text", "")
        before = doc.get("raw_before", "")
        # Only keep examples where the rule actually changed the text.
        if before.strip() == after.strip():
            return
        key = tuple(doc["rules"])
        if any(tuple(item["rules"]) == key for item in self.template_examples):
            return
        self.template_examples.append(
            {
                "source_path": doc["source_path"],
                "source_type": doc["source_type"],
                "rules": doc["rules"],
                "before": before[:700],
                "after": after[:700],
            }
        )

    # -- clectw ----------------------------------------------------------

    def read_clectw_summaries(self) -> None:
        for path in sorted(self.clectw.text_dir.glob("*.txt")):
            stem = path.stem.lower()
            raw = read_text(path)
            if is_blocked(stem):
                self.skip("clectw_summaries", raw_chars=len(raw), blocked=True)
                continue
            page = clean.clean_clectw_summary(raw)
            if not page.summary or len(page.summary) < 80:
                self.skip("clectw_summaries", raw_chars=len(raw))
                continue
            title = page.title or self.clectw.title_for(stem)
            doc = make_doc(
                work_id=stem,
                source_id=f"clectw_sum_{stem}",
                title=title,
                kind="summary",
                text=page.summary,
                source_type="clectw_summaries",
                source_path=path,
                source_dir=self.root,
                date_value=page.date,
                origin_url=f"https://clectw.com/ep/{stem}",
                rules=page.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

    def read_clectw_transcripts(self) -> None:
        for path in sorted(self.clectw.transcript_dir.glob("*.txt")):
            stem = path.stem.lower()
            raw = read_text(path)
            if is_blocked(stem):
                self.skip("clectw_transcripts", raw_chars=len(raw), blocked=True)
                continue
            cleaned = clean.clean_clectw_transcript(raw)
            if len(cleaned.text) < 80:
                self.skip("clectw_transcripts", raw_chars=len(raw))
                continue
            doc = make_doc(
                work_id=stem,
                source_id=f"clectw_tr_{stem}",
                title=self.clectw.title_for(stem),
                kind="transcript",
                text=cleaned.text,
                source_type="clectw_transcripts",
                source_path=path,
                source_dir=self.root,
                date_value=self.clectw.date_for(stem),
                origin_url=f"https://clectw.com/ep/{stem}",
                rules=cleaned.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

    # -- Drive text ------------------------------------------------------

    def read_drive_text(self) -> None:
        directory = self.root / "sources/drive/text"
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.txt")):
            raw = read_text(path)
            cleaned = clean.clean_drive_document(raw)
            title = cleaned.meta.get("title") or path.stem
            origin_match = re.search(r"(?m)^\s*来源\s*:\s*(https?://\S+)", raw)
            origin_url = origin_match.group(1) if origin_match else ""
            cues = chunk.parse_cue_markdown(cleaned.text)
            text = "".join(cue.text for cue in cues) if cues else cleaned.text
            fallback = f"drive-doc-{short_hash(path.stem)}"
            identity = clean.book_identity(title) or clean.book_identity(path.name)
            if identity:
                group, version, _language = identity
                work_id = f"book-{group}"
            else:
                group, version = "", ()
                work_id = infer_work_id(
                    title, stem_set=self.clectw.stems, fallback=fallback
                )
            doc = make_doc(
                work_id=work_id,
                source_id=f"drive_doc_{safe_slug(path.stem)}",
                title=title,
                kind="doc",
                text=text,
                source_type="drive_text",
                source_path=path,
                source_dir=self.root,
                date_value=clean.first_date(title),
                origin_url=origin_url,
                cues=cues or None,
                rules=cleaned.rules,
                raw_before=raw,
            )
            if group:
                doc["book_group"] = group
                doc["book_version"] = version
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

    # -- local text ------------------------------------------------------

    def read_local_text(self) -> None:
        directory = self.root / "text/local"
        if not directory.exists():
            return
        for path in sorted(directory.rglob("*.txt")):
            raw = read_text(path)
            is_spreadsheet = path.name.lower().endswith((".xlsx.txt", ".xls.txt"))
            scenario_match = re.search(r"\((\d{1,3})%\)", path.name)
            scenario = f"{scenario_match.group(1)}%" if scenario_match else ""
            if is_spreadsheet:
                cleaned = clean.clean_spreadsheet(raw, scenario=scenario)
            else:
                cleaned = clean.clean_local_document(raw)
            title = clean.normalize_title(path.name)
            title = re.sub(r"\.(?:pdf|docx?|pptx?|xlsx?|epub)\.txt$", "", title, flags=re.I)
            title = clean.normalize_title(title)
            fallback = f"local-{short_hash(path.name)}"
            identity = clean.book_identity(title) or clean.book_identity(path.name)
            if identity:
                group, version, _language = identity
                work_id = f"book-{group}"
            else:
                group, version = "", ()
                work_id = infer_work_id(
                    title, stem_set=self.clectw.stems, fallback=fallback
                )
            doc = make_doc(
                work_id=work_id,
                source_id=f"local_{safe_slug(path.parent.name)}_{safe_slug(path.stem)}",
                title=title,
                kind="doc",
                text=cleaned.text,
                source_type="local_text",
                source_path=path,
                source_dir=self.root,
                date_value=clean.first_date(title),
                rules=cleaned.rules,
                raw_before=raw,
            )
            if scenario:
                doc["scenario"] = scenario
            if group:
                doc["book_group"] = group
                doc["book_version"] = version
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

    # -- clec-md ---------------------------------------------------------

    def read_clec_md(self) -> None:
        root = self.root / "sources/clec-md"
        for path in sorted((root / "knowledge").rglob("*.md")):
            raw = read_text(path)
            front, body = parse_frontmatter(raw)
            cleaned = clean.clean_markdown(body)
            category = str(front.get("category", "")).strip().strip("'\"")
            relative_parts = path.relative_to(root / "knowledge").parts
            lang = "en" if relative_parts and relative_parts[0] == "en" else "zh"
            kind = "qa" if category == "qa" else "summary"
            title = clean.normalize_title(str(front.get("title", path.stem)))
            slug = safe_slug(path.stem)
            work_id = f"kb-{lang}-{safe_slug(category or 'general')}-{slug}"
            doc = make_doc(
                work_id=work_id,
                source_id=f"cmd_kb_{lang}_{safe_slug(category or 'general')}_{slug}",
                title=title,
                kind=kind,
                text=cleaned.text,
                source_type="clec_md",
                source_path=path,
                source_dir=self.root,
                date_value=front.get("date"),
                origin_url=str(front.get("sourceUrl", "")).strip().strip("'\""),
                rules=cleaned.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

        for path in sorted((root / "posts").rglob("*.md")):
            raw = read_text(path)
            front, body = parse_frontmatter(raw)
            cleaned = clean.clean_markdown(body)
            title = clean.normalize_title(str(front.get("title", path.stem)))
            slug = safe_slug(path.stem)
            identifier = re.search(r"(?<!\d)(\d{1,4})(?![0-9])", path.stem)
            work_id = f"post-{slug}"
            doc = make_doc(
                work_id=work_id,
                source_id=f"cmd_post_{slug}",
                title=title,
                kind="doc",
                text=cleaned.text,
                source_type="clec_md",
                source_path=path,
                source_dir=self.root,
                date_value=front.get("date"),
                origin_url=str(front.get("sourceUrl", "")).strip().strip("'\""),
                rules=cleaned.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

    # -- Drive transcripts ----------------------------------------------

    def read_drive_transcripts(self) -> None:
        directory = self.root / "sources/drive/archive-transcripts"
        if not directory.exists():
            return
        groups: dict[str, dict[str, Path]] = defaultdict(dict)
        for path in directory.iterdir():
            match = re.match(r"^(\d{5})-(0[123])\.(srt|str|txt|md)$", path.name, re.I)
            if not match:
                continue
            groups[match.group(1)][match.group(3).lower()] = path
        for group, files in sorted(groups.items()):
            md_path = files.get("md")
            raw_md = read_text(md_path) if md_path else ""
            title, date_value = parse_drive_md(raw_md, group)
            srt_path = files.get("srt") or files.get("str")
            raw_srt = read_text(srt_path) if srt_path else ""
            cues = chunk.parse_srt(raw_srt) if raw_srt.strip() else []
            if not srt_path or not cues:
                self.skip("drive_transcripts", raw_chars=len(raw_srt))
                continue
            text = "".join(cue.text for cue in cues)
            inferred = infer_work_id(
                title, stem_set=self.clectw.stems, fallback=f"drive-{group}",
                allow_unique_fallback=False,
            )
            if is_blocked(inferred):
                self.skip("drive_transcripts", raw_chars=len(raw_srt), blocked=True)
                continue
            work_id = self.map_drive_work(
                group, title, text, inferred, duration=cues[-1].end
            )
            if is_blocked(work_id):
                self.skip("drive_transcripts", raw_chars=len(raw_srt), blocked=True)
                continue
            doc = make_doc(
                work_id=work_id,
                source_id=f"drive_srt_{group}",
                title=title or work_id,
                kind="transcript",
                text=text,
                source_type="drive_transcripts",
                source_path=srt_path,
                source_dir=self.root,
                date_value=date_value,
                origin_url=f"https://clectw.com/ep/{work_id}" if work_id.startswith(("ep-", "short-", "talk-", "origin-", "chat-")) else "",
                cues=cues,
                rules=[],
                raw_before=raw_srt,
            )
            self.add(doc, raw_chars=len(raw_srt))

    def map_drive_work(
        self,
        group: str,
        title: str,
        text: str,
        inferred: str,
        *,
        duration: float = 0.0,
    ) -> str:
        """Align a Drive SRT group with its clectw work only when confident.

        A low-confidence content match must never replace the clectw verbatim
        transcript.  The old code fell back to the title identifier even when
        the two texts had zero overlap (``drive_srt_00070`` -> ``short-00001``)
        or mismatched durations (27-minute clip -> 3h18 Clubhouse episode).
        """
        query = dedup.shingles(text)
        if not query:
            return f"drive-{group}"

        identifiers: list[str] = []
        for match in NUMBER_TOKEN.finditer(clean.normalize_title(title)):
            number, suffix = match.group(1), (match.group(2) or "")
            if _is_year(number):
                continue
            identifier = _identifier(number, suffix)
            if identifier not in identifiers:
                identifiers.append(identifier)

        candidate_stems: set[str] = set()
        for identifier in identifiers:
            candidate_stems.update(_stem_candidates(self.clectw.stems, identifier))
        if inferred and inferred in self.clectw.stems:
            candidate_stems.add(inferred)
        candidates = {
            stem: self.clectw.shingles[stem]
            for stem in candidate_stems
            if stem in self.clectw.shingles and not is_blocked(stem)
        }
        match, score = self._best_content_match(query, candidates, duration)
        if match:
            return match

        # No exact stem, or the same-number candidate was not convincing:
        # allow a global content match with a duration guard.
        match, score = self._best_content_match(
            query, self.clectw.shingles, duration, minimum=0.45
        )
        if match:
            return match
        return f"drive-{group}"

    def _best_content_match(
        self,
        query: set[int],
        candidates: dict[str, set[int]],
        duration: float,
        *,
        minimum: float = 0.45,
    ) -> tuple[str | None, float]:
        best_stem = None
        best_score = 0.0
        for stem, grams in candidates.items():
            if not grams or is_blocked(stem):
                continue
            reference = self.clectw.duration_for(stem)
            if duration > 0 and reference:
                ratio = min(duration, reference) / max(duration, reference)
                if ratio < 0.70:
                    continue
            score = dedup.containment(query, grams)
            if score > best_score:
                best_stem, best_score = stem, score
        if best_score < minimum:
            return None, best_score
        return best_stem, best_score

    # -- ASR transcripts -------------------------------------------------

    def read_asr_transcripts(self) -> None:
        roots = [
            (self.root / "transcripts/local-asr", "local_asr"),
            (self.root / "transcripts/podcast-asr", "podcast_asr"),
        ]
        for directory, label in roots:
            if not directory.exists():
                continue
            for sub in sorted(directory.iterdir()):
                if not sub.is_dir():
                    continue
                raw = ""
                srt_path = next(iter(sorted(sub.glob("*.srt"))), None)
                if srt_path:
                    raw = read_text(srt_path)
                cues = chunk.parse_srt(raw) if raw else []
                if not cues:
                    for candidate in sorted(sub.glob("*.txt")):
                        raw = read_text(candidate)
                        break
                    text = raw
                    cues = None
                else:
                    text = "".join(cue.text for cue in cues)
                if len(text) < 80:
                    self.skip("asr_transcripts", raw_chars=len(raw))
                    continue
                meta = read_json(sub / "metadata.json")
                work_id = meta.get("id") or sub.name
                title = (
                    meta.get("source_title")
                    or meta.get("title")
                    or (cues[0].text if cues else sub.name)
                )
                date_value = self._podcast_date(meta)
                origin_url = meta.get("source") or meta.get("media_url") or ""
                doc = make_doc(
                    work_id=work_id,
                    source_id=f"{label}_{safe_slug(sub.name)}",
                    title=clean.normalize_title(title),
                    kind="transcript",
                    text=text,
                    source_type="asr_transcripts",
                    source_path=srt_path or sub,
                    source_dir=self.root,
                    date_value=date_value,
                    origin_url=origin_url if str(origin_url).startswith("http") else "",
                    cues=cues,
                    rules=["asr_srt_preferred"] if srt_path else [],
                    raw_before=raw,
                )
                self.add(doc, raw_chars=len(raw))

    def _load_podcast_metadata(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        if not self.enabled("podcast_metadata"):
            return result
        inventory = self.root / "sources/podcast/episode-inventory.jsonl"
        if inventory.exists():
            for line in read_text(inventory).splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                guid = str(row.get("guid", "")).strip()
                if not guid:
                    continue
                result[guid] = {
                    "published": row.get("published"),
                    "title": row.get("title", ""),
                    "link": (row.get("possible_matches") or [{}])[0].get("url", "")
                    if row.get("possible_matches") else "",
                    "enclosure": row.get("enclosure_url", ""),
                }
        return result

    def _podcast_date(self, meta: dict) -> str | None:
        guid = str(meta.get("source_guid", "")).strip()
        if guid and guid in self.podcast_meta:
            return clean.normalize_date(self.podcast_meta[guid].get("published"))
        return clean.normalize_date(meta.get("published") or meta.get("date"))

    # -- cfnavi ----------------------------------------------------------

    def read_cfnavi(self) -> None:
        directory = self.root / "web/cfnavi/text"
        manifest: dict[str, str] = {}
        manifest_path = self.root / "web/cfnavi/manifest.jsonl"
        for line in read_text(manifest_path).splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            text_path = str(row.get("text", "")).split("collection/")[-1]
            if text_path:
                manifest[text_path] = row.get("url", "")
        for path in sorted(directory.glob("*.txt")):
            raw = read_text(path)
            cleaned = clean.clean_cfnavi(raw)
            if len(cleaned.text) < 150 or clean.is_listing_page(cleaned.text):
                self.skip("cfnavi", raw_chars=len(raw))
                continue
            html_title = html_title_path(
                self.root / "web/cfnavi/html" / path.with_suffix(".html").name
            )
            html_title = re.sub(r"^\[[^\]]+\]\s*", "", html_title).strip()
            title = html_title or cleaned.meta.get("title") or path.stem
            body = cleaned.text
            if title:
                lines = body.splitlines()
                while lines:
                    first = lines[0].strip()
                    if first and (first in title or title in first) and not first.endswith(("。", "！", "？")):
                        lines.pop(0)
                    else:
                        break
                body = "\n".join(lines)
            label = cleaned.meta.get("label", "")
            work_id = self._cfnavi_work_id(label, path)
            if is_blocked(work_id):
                self.skip("cfnavi", raw_chars=len(raw), blocked=True)
                continue
            origin_url = manifest.get(str(path.relative_to(self.root)), "")
            doc = make_doc(
                work_id=work_id,
                source_id=f"cfnavi_{safe_slug(path.stem)}",
                title=title,
                kind="summary",
                text=body,
                source_type="cfnavi",
                source_path=path,
                source_dir=self.root,
                date_value=clean.first_date(body[:400], title),
                origin_url=origin_url,
                rules=cleaned.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

    def _cfnavi_work_id(self, label: str, path: Path) -> str:
        match = re.match(r"(cl|cj)-(\d+)", label or "")
        if match:
            number = match.group(2).zfill(5 if match.group(1) == "cl" else 5)
            prefix = "ep" if match.group(1) == "cl" else "talk"
            candidate = f"{prefix}-{number}"
            if candidate in self.clectw.stems:
                return candidate
        return f"cfnavi-{safe_slug(path.stem)}"

    # -- clec168 ---------------------------------------------------------

    def read_clec168(self) -> None:
        text_root = self.root / "web/clec168/text"
        for path in sorted((text_root / "blog").rglob("*.txt")):
            if "page" in path.parts:
                continue
            raw = read_text(path)
            cleaned = clean.clean_clec168_blog(raw)
            title = html_title_for(path, self.root)
            if not title:
                title = first_content_line(cleaned.text) or path.stem
            if len(cleaned.text) < 120 or clean.is_listing_page(cleaned.text):
                self.skip("clec168_blog", raw_chars=len(raw))
                continue
            doc = make_doc(
                work_id=f"c168-blog-{safe_slug(path.stem)}",
                source_id=f"c168_blog_{safe_slug(path.stem)}",
                title=title,
                kind="doc",
                text=cleaned.text,
                source_type="clec168_blog",
                source_path=path,
                source_dir=self.root,
                date_value=clean.first_date(cleaned.text[:200], title),
                origin_url=capture_url(raw),
                rules=cleaned.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

        for path in sorted((text_root / "docs").rglob("*.txt")):
            raw = read_text(path)
            cleaned = clean.clean_clec168_doc(raw)
            if len(cleaned.text) < 120 or clean.is_listing_page(cleaned.text):
                self.skip("clec168_docs", raw_chars=len(raw))
                continue
            title = html_title_for(path, self.root) or first_content_line(cleaned.text) or path.stem
            book = self._book_work_id(path)
            doc = make_doc(
                work_id=book,
                source_id=f"c168_doc_{safe_slug(book.replace('c168-doc-', ''))}_{safe_slug(path.stem)}",
                title=title,
                kind="doc",
                text=cleaned.text,
                source_type="clec168_docs",
                source_path=path,
                source_dir=self.root,
                date_value=clean.first_date(title),
                origin_url=capture_url(raw),
                heading=title,
                rules=cleaned.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

        important = text_root / "database/important"
        for path in sorted(important.glob("*.txt")):
            raw = read_text(path)
            cleaned = clean.clean_clec168_doc(raw)
            if len(cleaned.text) < 120 or clean.is_listing_page(cleaned.text):
                self.skip("clec168_docs", raw_chars=len(raw))
                continue
            title = html_title_for(path, self.root) or first_content_line(cleaned.text) or path.stem
            doc = make_doc(
                work_id=f"c168-ref-{safe_slug(path.stem)}",
                source_id=f"c168_ref_{safe_slug(path.stem)}",
                title=title,
                kind="doc",
                text=cleaned.text,
                source_type="clec168_docs",
                source_path=path,
                source_dir=self.root,
                date_value=clean.first_date(cleaned.text[:250], title),
                origin_url=capture_url(raw),
                heading=title,
                rules=cleaned.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

        presentations = text_root / "database/presentations"
        for path in sorted(presentations.rglob("*.txt")):
            if path.parent == presentations:
                continue  # year/category listing pages
            raw = read_text(path)
            cleaned = clean.clean_clec168_presentation(raw)
            if len(cleaned.text) < 120 or clean.is_listing_page(cleaned.text):
                self.skip("clec168_presentations", raw_chars=len(raw))
                continue
            title = html_title_for(path, self.root) or first_content_line(cleaned.text) or path.stem
            published = re.search(r"(?m)^\s*發布日期\s*:\s*([0-9-]+)", raw)
            doc = make_doc(
                work_id=f"c168-pres-{safe_slug(path.stem)}",
                source_id=f"c168_pres_{safe_slug(path.parent.name)}_{safe_slug(path.stem)}",
                title=title,
                kind="doc",
                text=cleaned.text,
                source_type="clec168_presentations",
                source_path=path,
                source_dir=self.root,
                date_value=published.group(1) if published else clean.first_date(title),
                origin_url=capture_url(raw),
                heading=title,
                rules=cleaned.rules,
                raw_before=raw,
            )
            self.collect_example(doc)
            self.add(doc, raw_chars=len(raw))

    @staticmethod
    def _book_work_id(path: Path) -> str:
        name = str(path).lower()
        if "clec-wisdom" in name or "clec寶典" in name or "clec宝典" in name:
            return "c168-doc-clec-wisdom"
        if "wealth-bible" in name or "理財聖經" in name or "理财圣经" in name:
            return "c168-doc-wealth-bible"
        if "investment-lecture" in name or "投資講座" in name or "投资讲座" in name:
            return "c168-doc-investment-lecture"
        return f"c168-doc-{safe_slug(path.parent.name)}"

    # -- metadata fill ---------------------------------------------------

    def finalize_book_versions(self) -> None:
        """Keep only the newest / most complete edition of each book.

        ``CLEC 寶典`` and ``The Wisdom of CLEC`` exist as V1.5.1 (local text)
        and V1.14.2 (Drive text); keeping both produces thousands of exact
        duplicate passages across different ``work_id`` values.
        """
        groups: dict[str, list[dict]] = defaultdict(list)
        for doc in self.docs:
            if doc.get("book_group"):
                groups[doc["book_group"]].append(doc)
        removed = 0
        for group, docs in groups.items():
            if len(docs) <= 1:
                continue
            winner = max(
                docs,
                key=lambda doc: (
                    tuple(doc.get("book_version") or ()),
                    len(doc.get("text", "")),
                    int(doc.get("priority", 0)),
                    doc.get("source_id", ""),
                ),
            )
            for doc in docs:
                if doc is winner:
                    continue
                doc["superseded_by"] = winner["source_id"]
                self.docs.remove(doc)
                stats = self.stats[doc["source_type"]]
                stats["docs_before_dedup"] = max(0, stats["docs_before_dedup"] - 1)
                stats["skipped"] += 1
                removed += 1
        self.superseded_versions = removed

    def fill_work_metadata(self) -> None:
        dates: dict[str, str] = {}
        origins: dict[str, str] = {}
        titles: dict[str, str] = {}
        for doc in sorted(self.docs, key=lambda item: -item["priority"]):
            if doc.get("date"):
                dates.setdefault(doc["work_id"], doc["date"])
            if doc.get("origin_url"):
                origins.setdefault(doc["work_id"], doc["origin_url"])
            if doc.get("title"):
                titles.setdefault(doc["work_id"], doc["title"])
        for doc in self.docs:
            doc["date"] = doc.get("date") or dates.get(doc["work_id"])
            doc["origin_url"] = doc.get("origin_url") or origins.get(doc["work_id"], "")
            if not doc.get("title"):
                doc["title"] = titles.get(doc["work_id"], doc["work_id"])


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    text = clean.repair_text(text)
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    front: dict = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.lstrip().startswith(("-", "#")):
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        front[key.strip()] = value
    return front, text[end + 4:]


def parse_drive_md(raw_md: str, group: str) -> tuple[str, str | None]:
    first = raw_md.splitlines()[0] if raw_md.splitlines() else ""
    first = re.sub(r"^#\s*", "", first.strip())
    title = clean.normalize_title(first)
    title = re.sub(r"^語音筆記\s*[：:]\s*", "", title)
    date_value = clean.first_date(first)
    if not title:
        title = f"Drive 逐字稿 {group}"
    return title, date_value


def html_title_for(text_path: Path, root: Path) -> str:
    try:
        relative = text_path.relative_to(root / "web/clec168/text")
    except ValueError:
        return ""
    return html_title_path(root / "web/clec168/html" / relative.with_suffix(".html"))


def html_title_path(html_path: Path) -> str:
    if not html_path.exists():
        return ""
    raw = read_text(html_path)
    match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if not match:
        return ""
    return clean.clean_clec168_html_title(match.group(1))


def capture_url(raw: str) -> str:
    match = clean.CAPTURE_HEADER.match(raw)
    if not match:
        match = re.search(r"(?m)^\s*(?:来源|來源)\s*:\s*(https?://\S+)", raw)
        return match.group(1) if match else ""
    line = raw.splitlines()[0]
    found = re.search(r"https?://\S+", line)
    return found.group(0) if found else ""


def first_content_line(text: str) -> str:
    for line in clean.normalize_whitespace(text).splitlines():
        line = line.strip()
        if line and len(line) > 2:
            return clean.normalize_title(line)
    return ""


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Passage assembly
# ---------------------------------------------------------------------------

def build_passages(docs: list[dict]) -> tuple[list[dict], list[dict]]:
    passages: list[dict] = []
    suspect_examples: list[dict] = []
    for doc in sorted(docs, key=lambda item: (item["work_id"], item["source_id"])):
        if doc.get("cues"):
            chunks = chunk.chunk_srt(doc["cues"])
        elif doc["kind"] == "transcript":
            chunks = chunk.chunk_plain_transcript(doc["text"])
        else:
            chunks = chunk.chunk_prose(doc["text"])
        if not chunks and len(clean.normalize_whitespace(doc["text"])) >= chunk.MIN_KEEP:
            chunks = chunk.force_chunk(doc["text"])
        source_hash = short_hash(doc["source_id"])
        ordinal = 0
        for piece in chunks:
            body = clean.strip_web_noise(piece.body).strip()
            if len(body) < chunk.MIN_FRAGMENT:
                continue
            quality = doc["quality"]
            if doc["kind"] == "transcript":
                result = hallucination.check_transcript(body)
                if result.suspect:
                    quality = 4
                    if len(suspect_examples) < 20:
                        suspect_examples.append(
                            {
                                "doc_id": f"p_{safe_slug(doc['work_id'])}_tr_{source_hash}_{ordinal:04d}",
                                "work_id": doc["work_id"],
                                "source_id": doc["source_id"],
                                "title": doc["title"],
                                "body": body,
                                "reasons": result.reasons,
                                "compression_ratio": round(result.compression_ratio, 4),
                                "max_repeat_count": result.max_repeat_count,
                                "repeat_coverage": round(result.repeat_coverage, 4),
                            }
                        )
            doc_id = f"p_{safe_slug(doc['work_id'])}_{doc['kind'][:2]}_{source_hash}_{ordinal:04d}"
            passages.append(
                {
                    "doc_id": doc_id,
                    "work_id": doc["work_id"],
                    "source_id": doc["source_id"],
                    "ordinal": ordinal,
                    "kind": doc["kind"],
                    "quality": quality,
                    "date": doc["date"],
                    "start_sec": piece.start_sec,
                    "end_sec": piece.end_sec,
                    "title": doc["title"],
                    "heading": piece.heading or doc.get("heading", "") or "",
                    "body": body,
                    "origin_url": doc.get("origin_url", ""),
                    "other_kinds": list(doc.get("other_kinds", [])),
                    "license_mode": "full",
                }
            )
            ordinal += 1
    return passages, suspect_examples


def make_doc_manifest(docs: list[dict], passages: list[dict]) -> list[dict]:
    """Record per-document passage production for the independent audit."""
    counts = Counter(row["source_id"] for row in passages)
    manifest: list[dict] = []
    for doc in docs:
        text_chars = len(clean.normalize_whitespace(doc.get("text", "")))
        manifest.append(
            {
                "source_id": doc["source_id"],
                "work_id": doc["work_id"],
                "kind": doc["kind"],
                "text_chars": text_chars,
                "passage_count": counts.get(doc["source_id"], 0),
            }
        )
    return manifest


def deduplicate_cross_work_passages(
    passages: list[dict],
    priority_by_source: dict[str, int],
) -> list[dict]:
    """Drop exact ``body`` duplicates that appear under different works.

    Keeps the higher-priority source and renumbers ordinals/``doc_id`` so the
    contract's contiguous-ordinal invariant still holds.
    """
    by_body: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(passages):
        by_body[row["body"]].append(index)
    drop: set[int] = set()
    for indices in by_body.values():
        if len(indices) < 2:
            continue
        if len({passages[index]["work_id"] for index in indices}) < 2:
            continue
        winner = max(
            indices,
            key=lambda index: (
                priority_by_source.get(passages[index]["source_id"], 0),
                -index,
            ),
        )
        winner_source = passages[winner]["source_id"]
        for index in indices:
            if passages[index]["source_id"] != winner_source:
                drop.add(index)

    kept = [row for index, row in enumerate(passages) if index not in drop]
    counters: dict[str, int] = defaultdict(int)
    for row in kept:
        source_id = row["source_id"]
        ordinal = counters[source_id]
        counters[source_id] = ordinal + 1
        row["ordinal"] = ordinal
        row["doc_id"] = (
            f"p_{safe_slug(row['work_id'])}_{row['kind'][:2]}_"
            f"{short_hash(source_id)}_{ordinal:04d}"
        )
    return kept


def assign_passage_other_kinds(passages: list[dict]) -> None:
    """Backfill ``other_kinds`` from the final, de-duplicated passages.

    A map keyed only by ``work_id`` is wrong when a work has several
    documents of different kinds: the last document overwrites the earlier
    ones and every passage receives the same (possibly self-referential)
    list.  Aggregate the kinds that actually survived into ``passages`` and
    compute the complement for each individual row instead.
    """
    kinds_by_work: dict[str, set[str]] = defaultdict(set)
    for row in passages:
        kinds_by_work[row["work_id"]].add(row["kind"])
    for row in passages:
        row["other_kinds"] = sorted(kinds_by_work[row["work_id"]] - {row["kind"]})


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def make_meta(passages: list[dict]) -> dict:
    kind_counts = Counter(row["kind"] for row in passages)
    quality_counts = Counter(row["quality"] for row in passages)
    years = Counter(row["date"][:4] for row in passages if row["date"])
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "pack_name": "clec",
        "pack_version": CURRENT_PACK_VERSION,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {
            "works": len({row["work_id"] for row in passages}),
            "sources": len({row["source_id"] for row in passages}),
            "passages": len(passages),
            "by_kind": {kind: kind_counts.get(kind, 0) for kind in KIND_ORDER},
            "by_quality": {str(q): quality_counts.get(q, 0) for q in (1, 2, 3, 4)},
            "by_year": dict(sorted(years.items())),
        },
        "known_gaps": KNOWN_GAPS,
        "disclaimer": "本知识库为非官方学习检索工具，不构成投资建议。",
    }


def select_template_examples(examples: list[dict], limit: int = 10) -> list[dict]:
    """Pick diverse before/after examples, prioritising real web templates."""
    priority = (
        "web_capture_header",
        "episode_player_control",
        "clectw_episode_footer",
        "blog_excerpt_before_source",
        "blog_related_footer",
        "publisher_mailing_footer",
        "cfnavi_navigation_footer",
        "cfnavi_social_card",
        "presentation_metadata_block",
        "book_pagination_footer",
        "drive_capture_header",
        "episode_resource_links",
        "weekly_ai_disclaimer",
    )
    web_sources = {
        "clectw_summaries",
        "clectw_transcripts",
        "drive_text",
        "drive_transcripts",
        "cfnavi",
        "clec168_blog",
        "clec168_docs",
        "clec168_presentations",
    }
    selected: list[dict] = []
    web = [example for example in examples if example.get("source_type") in web_sources]
    everything = web + [example for example in examples if example not in web]

    def add(example: dict) -> None:
        if example not in selected and len(selected) < limit:
            selected.append(example)

    for rule in priority:
        for example in web:
            if rule in example.get("rules", []):
                add(example)
                break
    for example in everything:
        if len(selected) >= limit:
            break
        add(example)
    return selected[:limit]


def build(source_dir: Path, out_dir: Path) -> dict:
    builder = Builder(source_dir)
    readers = (
        ("clectw_summaries", builder.read_clectw_summaries),
        ("clectw_transcripts", builder.read_clectw_transcripts),
        ("drive_text", builder.read_drive_text),
        ("local_text", builder.read_local_text),
        ("clec_md", builder.read_clec_md),
        ("drive_transcripts", builder.read_drive_transcripts),
        ("asr_transcripts", builder.read_asr_transcripts),
        ("cfnavi", builder.read_cfnavi),
        ("clec168", builder.read_clec168),
    )
    for source_key, reader in readers:
        if builder.enabled(source_key):
            reader()
    builder.finalize_book_versions()
    builder.fill_work_metadata()

    result = dedup.deduplicate_documents(builder.docs)
    docs = result.kept
    docs.sort(key=lambda item: (item["source_type"], item["source_id"]))
    passages, suspect_examples = build_passages(docs)

    # Hard invariant: every cleaned document of at least 100 characters must
    # produce at least one passage.  A build that drops a whole document is a
    # failure, not a silent statistics change.
    manifest = make_doc_manifest(docs, passages)
    zero_passage_docs = [
        entry for entry in manifest
        if entry["text_chars"] >= chunk.MIN_KEEP and entry["passage_count"] == 0
    ]
    if zero_passage_docs:
        names = ", ".join(entry["source_id"] for entry in zero_passage_docs[:10])
        raise RuntimeError(
            f"docs_with_zero_passages={len(zero_passage_docs)}: {names}"
        )

    priority_by_source = {doc["source_id"]: doc["priority"] for doc in docs}
    pre_cross_work_count = len(passages)
    passages = deduplicate_cross_work_passages(passages, priority_by_source)
    assign_passage_other_kinds(passages)

    final_counts = Counter(row["source_id"] for row in passages)
    for entry in manifest:
        entry["final_passage_count"] = final_counts.get(entry["source_id"], 0)

    # Per-source post-dedup/passage stats.
    for doc in docs:
        stats = builder.stats[doc["source_type"]]
        stats["docs_after_dedup"] += 1
    source_by_id = {doc["source_id"]: doc["source_type"] for doc in docs}
    for row in passages:
        doc_source = source_by_id.get(row["source_id"])
        if doc_source:
            builder.stats[doc_source]["passages"] += 1
            builder.stats[doc_source]["passage_chars"] += len(row["body"])

    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = out_dir / "corpus.jsonl"
    with corpus_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in passages:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    manifest_path = out_dir / "doc-manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in manifest:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    meta = make_meta(passages)
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    template_examples = select_template_examples(builder.template_examples, 10)
    (out_dir / "audit-samples.json").write_text(
        json.dumps(
            {
                "template_examples": template_examples,
                "suspect_examples": suspect_examples,
                "blocked_works": list(BLOCKED_WORKS),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    stats_payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_dir": str(source_dir),
        "source_labels": SOURCE_LABELS,
        "sources": {key: dict(value) for key, value in builder.stats.items()},
        "dedup": {
            "before": result.before,
            "after": len(result.kept),
            "same_work_merges": result.same_work_merges,
            "near_duplicate_merges": result.near_duplicate_merges,
            "removed": len(result.removed),
        },
        "skipped_blocked": builder.skipped_blocked,
        "blocked_works": list(BLOCKED_WORKS),
        "book_versions_removed": builder.superseded_versions,
        "cross_work_exact_passages_removed": pre_cross_work_count - len(passages),
        "docs_with_zero_passages": len(zero_passage_docs),
        "complements": dedup.complement_counts(docs),
    }
    (out_dir / "build-stats.json").write_text(
        json.dumps(stats_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "corpus": str(corpus_path),
        "meta": str(out_dir / "meta.json"),
        "manifest": str(manifest_path),
        "passages": len(passages),
        "works": meta["counts"]["works"],
        "stats": stats_payload,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-dir", type=Path, default=Path("/Users/century/Vibing/clec/collection"))
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    source_dir = args.source_dir.resolve()
    if not (source_dir / "web/clectw").exists():
        parser.error(f"source directory does not look like the CLEC collection: {source_dir}")
    result = build(source_dir, args.out_dir)
    print(json.dumps({k: result[k] for k in ("corpus", "meta", "passages", "works")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
