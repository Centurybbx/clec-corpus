"""Natural-semantic passage chunking.

Two inputs are supported:

* SRT/VTT-style transcripts with per-cue timestamps, merged on sentence
  punctuation and pause gaps into 400-600 character passages with a
  ``start_sec``/``end_sec`` range;
* prose documents, split on Markdown headings and natural paragraphs and then
  packed into 400-600 character passages without cutting a sentence in half.

The chunker never intentionally cuts in the middle of a sentence.  A guarded
fallback only splits a single run-on sentence at a comma, or finally at the
900-character hard ceiling, when no punctuation exists at all.

Short but meaningful sections (40-99 characters) are preserved: the Stage-1
spec allows up to 5% of passages to fall outside 100-900 characters.  Only
fragments shorter than ``MIN_FRAGMENT`` are discarded.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from .clean import normalize_whitespace, repair_text, strip_web_noise

TARGET_MIN = 400
TARGET_MAX = 600
HARD_MAX = 900
MIN_KEEP = 100
MIN_FRAGMENT = 20
UNIT_TARGET = TARGET_MIN

SENTENCE_END = "。！？!?；;…"
SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;…])")
SOFT_BREAK = re.compile(r"(?<=[，,、：:])")
HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*)$")
KNOWN_HEADINGS = {
    "本週課程概述", "本週課程重點", "本集重點", "課程重點",
    "學員問答精選", "重要 Q&A 彙整", "重要Q&A彙整", "本集週報",
}
SRT_TIME = re.compile(
    r"(?P<h1>\d{1,2}):(?P<m1>\d{2}):(?P<s1>\d{2})[,.](?P<ms1>\d{1,3})\s*-->\s*"
    r"(?P<h2>\d{1,2}):(?P<m2>\d{2}):(?P<s2>\d{2})[,.](?P<ms2>\d{1,3})"
)
VTT_TIME = re.compile(
    r"(?P<m1>\d{1,2}):(?P<s1>\d{2})[.](?P<ms1>\d{3})\s*-->\s*"
    r"(?P<m2>\d{1,2}):(?P<s2>\d{2})[.](?P<ms2>\d{3})"
)
# Timestamp headings produced by Drive's Markdown export, e.g.
# ``## [0:00:00,000 – 0:00:08,000]``.  They are cue timings, not headings.
CUE_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?\[?"
    r"(?P<h1>\d{1,2}):(?P<m1>\d{2})(?::(?P<s1>\d{2}))?[,.](?P<ms1>\d{1,3})"
    r"\s*(?:-->|[–—−-])\s*"
    r"(?P<h2>\d{1,2}):(?P<m2>\d{2})(?::(?P<s2>\d{2}))?[,.](?P<ms2>\d{1,3})"
    r"\]?\s*$"
)
TAG = re.compile(r"<[^>]+>")
BRACE = re.compile(r"\{\\[^}]+\}")


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Passage:
    body: str
    heading: str = ""
    start_sec: int | None = None
    end_sec: int | None = None


# ---------------------------------------------------------------------------
# SRT / VTT / timestamped-Markdown parsing
# ---------------------------------------------------------------------------

def _seconds(hours: str | None, minutes: str, seconds: str, millis: str) -> float:
    return (
        (int(hours) if hours else 0) * 3600
        + int(minutes) * 60
        + int(seconds) * 1
        + int(millis.ljust(3, "0")[:3]) / 1000.0
    )


def _clean_cue_text(body: str) -> str:
    body = TAG.sub("", body)
    body = BRACE.sub("", body)
    body = body.replace("&nbsp;", " ").replace("&amp;", "&")
    body = strip_web_noise(body)
    return re.sub(r"\s+", " ", body).strip()


def parse_srt(text: str) -> list[Cue]:
    """Parse SRT or WebVTT cues, ignoring malformed blocks."""
    text = repair_text(text).replace("\ufeff", "")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_index = next(
            (i for i, line in enumerate(lines) if "-->" in line), None
        )
        if time_index is None:
            continue
        time_line = lines[time_index]
        match = SRT_TIME.search(time_line)
        if match:
            start = _seconds(match.group("h1"), match.group("m1"), match.group("s1"), match.group("ms1"))
            end = _seconds(match.group("h2"), match.group("m2"), match.group("s2"), match.group("ms2"))
        else:
            match = VTT_TIME.search(time_line)
            if not match:
                continue
            start = _seconds(None, match.group("m1"), match.group("s1"), match.group("ms1"))
            end = _seconds(None, match.group("m2"), match.group("s2"), match.group("ms2"))
        body = _clean_cue_text(" ".join(lines[time_index + 1:]))
        if body:
            cues.append(Cue(start=max(0.0, start), end=max(start, end), text=body))
    return cues


def parse_cue_markdown(text: str) -> list[Cue]:
    """Parse Markdown transcripts whose headings are cue timestamps.

    The Drive Markdown export used for the 2026 lecture series looks like::

        ## [0:00:00,000 – 0:00:08,000]
        大家好,這裡是CRAC投資理財頻道

    Treating that heading as a normal Markdown heading would collapse every
    cue into its own 5-character section and lose the whole lecture.
    """
    cues: list[Cue] = []
    start: float | None = None
    end: float | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal start, end, buffer
        body = _clean_cue_text(" ".join(buffer))
        if body and start is not None and end is not None:
            cues.append(Cue(start=max(0.0, start), end=max(start, end), text=body))
        buffer = []

    saw_cue = False
    for raw_line in repair_text(text).splitlines():
        match = CUE_HEADING.match(raw_line.strip())
        if match:
            if saw_cue:
                flush()
            start = _seconds(
                match.group("h1"), match.group("m1"),
                match.group("s1") or "0", match.group("ms1"),
            )
            end = _seconds(
                match.group("h2"), match.group("m2"),
                match.group("s2") or "0", match.group("ms2"),
            )
            saw_cue = True
            continue
        if not saw_cue:
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # A genuine heading after the cue block ends the transcript.
            break
        if re.match(r"^(?:来源|來源|采集时间|採集時間|标题|標題|文件类型|文件類型|提取方法)\s*:", stripped):
            break
        buffer.append(stripped)
    if saw_cue:
        flush()
    return cues


# ---------------------------------------------------------------------------
# Sentence helpers
# ---------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    """Split prose at sentence-ending punctuation, preserving punctuation."""
    text = normalize_whitespace(text)
    if not text:
        return []
    sentences: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for part in SENTENCE_SPLIT.split(line):
            part = part.strip()
            if part:
                sentences.append(part)
    return sentences


def split_soft(text: str, limit: int) -> list[str]:
    """Split an over-long sentence at commas/target length, never mid-word."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        candidates = [m.end() for m in SOFT_BREAK.finditer(window)]
        cut = candidates[-1] if candidates else -1
        if cut < limit // 2:
            # No useful comma: fall back to the hard ceiling for run-on ASR.
            cut = limit
        piece = remaining[:cut].strip()
        if piece:
            pieces.append(piece)
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


# ---------------------------------------------------------------------------
# Document chunking
# ---------------------------------------------------------------------------

def _flush(buffer: list[str], heading: str, output: list[Passage]) -> None:
    text = "".join(buffer).strip()
    if text:
        output.append(Passage(body=text, heading=heading))
    buffer.clear()


def _soften(passages: list[Passage], limit: int = TARGET_MAX, hard_max: int = HARD_MAX) -> list[Passage]:
    """Split every passage above ``hard_max`` before short-neighbour merging."""
    output: list[Passage] = []
    for passage in passages:
        if len(passage.body) <= hard_max:
            output.append(passage)
            continue
        for piece in split_soft(passage.body, limit):
            piece = piece.strip()
            if piece:
                output.append(Passage(body=piece, heading=passage.heading))
    return output


def merge_short(passages: list[Passage], hard_max: int = HARD_MAX) -> list[Passage]:
    """Merge short neighbours where possible; keep 20-99 character sections.

    The previous implementation silently discarded every passage under 100
    characters.  Stage-1 permits up to 5% of passages outside 100-900
    characters, so only genuinely meaningless fragments are removed later.
    """
    if not passages:
        return []
    merged: list[Passage] = [passages[0]]
    for passage in passages[1:]:
        previous = merged[-1]
        if (
            (len(previous.body) < MIN_KEEP or len(passage.body) < MIN_KEEP)
            and len(previous.body) + len(passage.body) <= hard_max
        ):
            previous.body = previous.body + passage.body
            if not previous.heading:
                previous.heading = passage.heading
            if previous.start_sec is not None:
                if passage.end_sec is not None:
                    previous.end_sec = max(previous.end_sec or 0, passage.end_sec)
            else:
                previous.start_sec = passage.start_sec
                previous.end_sec = passage.end_sec
            continue
        merged.append(passage)
    return merged


def chunk_prose(
    text: str,
    *,
    min_chars: int = MIN_FRAGMENT,
    use_markdown_headings: bool = True,
) -> list[Passage]:
    """Chunk prose into 400-600 character passages on natural boundaries."""
    text = normalize_whitespace(text)
    if not text:
        return []

    heading = ""
    buffer: list[str] = []
    output: list[Passage] = []
    units: list[tuple[str, str]] = []  # (unit text, heading)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading_match = HEADING_LINE.match(line) if use_markdown_headings else None
        if heading_match:
            heading = heading_match.group(2).strip()
            continue
        if line in KNOWN_HEADINGS or (
            len(line) <= 40
            and line.endswith(("概述", "重點", "精選", "彙整"))
            and not line.endswith(("。", "，"))
        ):
            heading = line.strip("# ").strip()
            continue
        for sentence in split_sentences(line):
            units.append((sentence, heading))

    if not units:
        return []

    current_heading = units[0][1]
    for unit, unit_heading in units:
        if unit_heading != current_heading:
            _flush(buffer, current_heading, output)
            current_heading = unit_heading
        length = len("".join(buffer))
        if length == 0:
            buffer.append(unit)
            continue
        candidate = length + len(unit)
        if candidate <= TARGET_MAX:
            buffer.append(unit)
        elif length >= TARGET_MIN:
            _flush(buffer, current_heading, output)
            buffer.append(unit)
        elif candidate <= HARD_MAX:
            buffer.append(unit)
        else:
            _flush(buffer, current_heading, output)
            buffer.append(unit)
    _flush(buffer, current_heading, output)

    # split_soft() first, then merge short neighbours.  A short document or a
    # short section is preserved rather than silently deleted.
    output = _soften(output)
    output = merge_short(output, hard_max=HARD_MAX)
    return [p for p in output if len(p.body) >= min_chars]


def force_chunk(text: str, *, hard_max: int = HARD_MAX) -> list[Passage]:
    """Last-resort chunker guaranteeing output for non-trivial documents."""
    text = normalize_whitespace(text)
    if not text:
        return []
    pieces: list[str] = []
    for sentence in split_sentences(text):
        if len(sentence) > hard_max:
            pieces.extend(split_soft(sentence, TARGET_MAX))
        else:
            pieces.append(sentence)
    if not pieces:
        pieces = split_soft(text, TARGET_MAX)
    output: list[Passage] = []
    buffer = ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) > TARGET_MAX:
            output.append(Passage(body=buffer))
            buffer = piece
        else:
            buffer += piece
    if buffer:
        output.append(Passage(body=buffer))
    return [p for p in output if len(p.body) >= MIN_FRAGMENT]


# ---------------------------------------------------------------------------
# Transcript chunking with timestamps
# ---------------------------------------------------------------------------

def _cue_units(cues: list[Cue]) -> list[tuple[str, float, float]]:
    """Group cues into sentence/pause units carrying their time range.

    Units are also capped at ``UNIT_TARGET`` characters so that a long,
    unpunctuated ASR stream does not become one single 27,000-character unit
    sharing a single zero-to-end timestamp range.
    """
    units: list[tuple[str, float, float]] = []
    buffer: list[str] = []
    start = 0.0
    end = 0.0
    length = 0
    for index, cue in enumerate(cues):
        if not buffer:
            start = cue.start
        buffer.append(cue.text)
        length += len(cue.text)
        end = cue.end
        gap = (cues[index + 1].start - cue.end) if index + 1 < len(cues) else 99.0
        ends_sentence = bool(cue.text) and cue.text[-1] in SENTENCE_END
        if ends_sentence or gap >= 1.8 or length >= UNIT_TARGET:
            text = _join_cues(buffer)
            if text:
                units.append((text, start, end))
            buffer = []
            length = 0
    if buffer:
        text = _join_cues(buffer)
        if text:
            units.append((text, start, end))
    return units


def _join_cues(parts: Iterable[str]) -> str:
    text = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not text:
            text = part
        elif text[-1] in "，,、。！？!?；;：:…" or part[0] in "，,、。！？!?；;：:…":
            text += part
        else:
            text += part
    return re.sub(r"\s+", " ", text).strip()


def _split_unit_with_time(
    output: list[Passage],
    unit: str,
    start: float,
    end: float,
) -> None:
    """Split one over-long unit and interpolate its time range by characters.

    Linear interpolation keeps ``start_sec``/``end_sec`` monotonic and avoids
    many passages in the same source sharing one huge ``0–1955`` second range.
    """
    pieces = split_soft(unit, TARGET_MAX)
    total = sum(len(piece) for piece in pieces) or 1
    cursor = 0
    if end <= start:
        for index, piece in enumerate(pieces):
            piece_start = start + index
            piece_end = piece_start + 1
            output.append(
                Passage(
                    body=piece,
                    start_sec=max(0, int(math.floor(piece_start))),
                    end_sec=max(0, int(math.ceil(piece_end))),
                )
            )
        return
    for piece in pieces:
        piece_start = start + (end - start) * (cursor / total)
        cursor += len(piece)
        piece_end = start + (end - start) * (cursor / total)
        start_sec = max(0, int(math.floor(piece_start)))
        end_sec = max(0, int(math.ceil(piece_end)))
        if end_sec <= start_sec:
            end_sec = start_sec + 1
        output.append(Passage(body=piece, start_sec=start_sec, end_sec=end_sec))


def chunk_srt(cues: list[Cue]) -> list[Passage]:
    """Merge timestamped cues into 400-600 character passages."""
    if not cues:
        return []
    units = _cue_units(cues)
    if not units:
        return []
    output: list[Passage] = []
    buffer: list[str] = []
    buffer_start: float | None = None
    buffer_end: float | None = None

    def flush() -> None:
        nonlocal buffer, buffer_start, buffer_end
        text = _join_cues(buffer)
        if text and buffer_start is not None and buffer_end is not None:
            output.append(
                Passage(
                    body=text,
                    start_sec=max(0, int(math.floor(buffer_start))),
                    end_sec=max(0, int(math.ceil(buffer_end))),
                )
            )
        buffer = []
        buffer_start = None
        buffer_end = None

    for unit, start, end in units:
        length = len(_join_cues(buffer)) if buffer else 0
        unit_len = len(unit)
        if length == 0:
            if unit_len > HARD_MAX:
                _split_unit_with_time(output, unit, start, end)
            else:
                buffer = [unit]
                buffer_start, buffer_end = start, end
            continue
        candidate = length + unit_len
        if candidate <= TARGET_MAX:
            buffer.append(unit)
            buffer_end = max(buffer_end or end, end)
        elif length >= TARGET_MIN:
            flush()
            if unit_len > HARD_MAX:
                _split_unit_with_time(output, unit, start, end)
            else:
                buffer = [unit]
                buffer_start, buffer_end = start, end
        elif candidate <= HARD_MAX:
            buffer.append(unit)
            buffer_end = max(buffer_end or end, end)
        else:
            flush()
            if unit_len > HARD_MAX:
                _split_unit_with_time(output, unit, start, end)
            else:
                buffer = [unit]
                buffer_start, buffer_end = start, end
    flush()

    output = _soften(output)
    output = merge_short(output, hard_max=HARD_MAX)

    # Enforce monotonic, non-overlapping timestamps within one source.  This
    # also guarantees that two passages never share the exact same range.
    final: list[Passage] = []
    last_end = 0
    for passage in output:
        if len(passage.body) < MIN_FRAGMENT:
            continue
        start_sec = passage.start_sec
        end_sec = passage.end_sec
        if start_sec is None or end_sec is None:
            final.append(passage)
            continue
        if start_sec < last_end:
            start_sec = last_end
        if end_sec <= start_sec:
            end_sec = start_sec + 1
        if final and start_sec == final[-1].start_sec and end_sec == final[-1].end_sec:
            end_sec = start_sec + 1
        passage.start_sec = start_sec
        passage.end_sec = end_sec
        last_end = end_sec
        final.append(passage)
    return final


def chunk_plain_transcript(text: str) -> list[Passage]:
    return chunk_prose(text, use_markdown_headings=False)
