"""Whisper hallucination gate.

A transcript passage is marked ``quality=4`` (suspect) when at least one of
three independent signals fires:

1. zlib compression ratio > 3.0 (uncompressed UTF-8 bytes / compressed bytes),
   high enough that normal spoken Chinese does not trip it;
2. a >= 8-character CJK phrase with at least five distinct characters repeats
   at high density (>= 4 occurrences and covers at least 18% of the passage);
3. a high-specificity subtitle-credit hallucination phrase appears anywhere,
   or a generic "thanks for watching" phrase is the whole passage.

The gate is deliberately conservative: it marks for down-weighting, it does
not delete text.  The real quarantine episodes are handled separately by a
hard blocklist in the build pipeline.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from dataclasses import dataclass

COMPRESSION_THRESHOLD = 3.0
REPEAT_MIN_COUNT = 4
REPEAT_MIN_COVERAGE = 0.18
CJK_NGRAM = 8

CJK_ONLY = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
WORD_OR_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaffA-Za-z0-9]+")

# Phrases that are unambiguously inserted by a subtitle group / Whisper
# artefact and may appear in the middle of an otherwise normal passage.
HIGH_SPECIFICITY_PHRASES = (
    "請不吝點贊訂閱轉發打賞支持明鏡與點點欄目",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "明鏡與點點欄目",
    "明镜与点点栏目",
    "字幕由amara.org社区提供",
    "字幕由amara.org社群提供",
    "字幕由amaraorg",
    "amara.org",
    "字幕志願者",
    "字幕志愿者",
    "www.zeebee.com",
    "中文字幕由",
    "字幕組提供",
    "字幕组提供",
)

# Generic spoken / site phrases: only suspicious when they are essentially the
# whole passage, otherwise a real sign-off would be mislabelled.
LOW_SPECIFICITY_PHRASES = (
    "感謝您的觀看",
    "感谢您的观看",
    "感謝觀看",
    "感谢观看",
    "謝謝觀看",
    "谢谢观看",
    "請訂閱頻道",
    "请订阅频道",
    "訂閱我們的頻道",
    "订阅我们的频道",
    "請大家記得訂閱",
    "大家記得訂閱",
    "點贊訂閱",
    "点赞订阅",
    "thanksforwatching",
    "thankyouforwatching",
    "subscribed",
    "subtitlesby",
)


@dataclass
class HallucinationResult:
    suspect: bool
    reasons: list[str]
    compression_ratio: float
    max_repeat_count: int
    repeat_coverage: float
    phrase: str = ""


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", "", text)


def _compact(text: str) -> str:
    return "".join(WORD_OR_CJK.findall(_normalise(text)))


def compression_ratio(text: str) -> float:
    raw = text.encode("utf-8", errors="replace")
    if not raw:
        return 0.0
    compressed = zlib.compress(raw, 6)
    return len(raw) / max(1, len(compressed))


def _repeat_signal(text: str) -> tuple[int, float, str]:
    normalised = _normalise(text)
    best: tuple[int, float, str] = (0, 0.0, "")

    cjk = "".join(CJK_ONLY.findall(normalised))
    if len(cjk) >= CJK_NGRAM:
        counts: dict[str, int] = {}
        for index in range(len(cjk) - CJK_NGRAM + 1):
            gram = cjk[index:index + CJK_NGRAM]
            counts[gram] = counts.get(gram, 0) + 1
        phrase, count = max(counts.items(), key=lambda item: (item[1], item[0]))
        coverage = (count * CJK_NGRAM) / max(1, len(cjk))
        # A phrase made of one or two repeating characters ("謝謝謝謝",
        # "美元美元美元") is normal spoken emphasis, not a Whisper loop.
        if (
            count >= REPEAT_MIN_COUNT
            and coverage >= REPEAT_MIN_COVERAGE
            and len(set(phrase)) >= 5
        ):
            best = (count, coverage, phrase)
    return best


def _phrase_signal(text: str) -> str:
    compact = _compact(text)
    if not compact:
        return ""
    for phrase in HIGH_SPECIFICITY_PHRASES:
        needle = _compact(phrase)
        if needle and needle in compact:
            return phrase
    for phrase in LOW_SPECIFICITY_PHRASES:
        needle = _compact(phrase)
        if not needle or needle not in compact:
            continue
        standalone = len(compact) <= len(needle) * 3
        if standalone:
            return phrase
    return ""


def check_transcript(body: str) -> HallucinationResult:
    ratio = compression_ratio(body)
    count, coverage, phrase = _repeat_signal(body)
    boundary_phrase = _phrase_signal(body)
    reasons: list[str] = []
    if ratio > COMPRESSION_THRESHOLD:
        reasons.append(f"zlib_ratio={ratio:.3f}>3.0")
    if count >= REPEAT_MIN_COUNT and coverage >= REPEAT_MIN_COVERAGE:
        reasons.append(
            f"repeat={count}x,coverage={coverage:.3f},phrase={phrase!r}"
        )
    if boundary_phrase:
        reasons.append(f"isolated_hallucination_phrase={boundary_phrase!r}")
    return HallucinationResult(
        suspect=bool(reasons),
        reasons=reasons,
        compression_ratio=ratio,
        max_repeat_count=count,
        repeat_coverage=coverage,
        phrase=phrase,
    )
