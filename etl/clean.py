"""Cleaning and normalisation helpers for the CLEC corpus ETL.

The original collection is treated as strictly read-only.  Every helper in
this module returns new strings; no source file is ever modified.

The web-template rules are a strengthened version of the rules used by the
legacy lexical benchmark (evaluation/clec-v1/local-benchmark/lexical.py,
lines 61-82), expanded to cover the site chrome that actually appears in the
2,821 ``web/clec168`` captures and the 2,246 ``web/clectw/text`` captures.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Iterable


# ---------------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------------

CAPTURE_HEADER = re.compile(
    r"\A(?:来源|來源)\s*:\s*https?://[^\n\r]*\r?\n"
    r"(?:采集时间|採集時間)\s*:\s*\d{4}-\d{2}-\d{2}[^\n\r]*\r?\n"
    r"(?:\r?\n)?"
)

DRIVE_HEADER = re.compile(
    r"\A(?:来源|來源)\s*:\s*https?://[^\n\r]*\r?\n"
    r"标题\s*:\s*(?P<title>[^\n\r]*)\r?\n"
    r"(?:文件类型|文件類型|提取方法)\s*:\s*[^\n\r]*\r?\n"
    r"(?:\r?\n)?"
)

PLAYER_CONTROL = re.compile(r"(?m)^\s*播完自動接下一集\s*\r?\n?")
RELATED_EPISODES = re.compile(r"(?m)^\s*▶\s*相關集數\s*$")
TRANSCRIPT_DISCLAIMER = re.compile(
    r"(?m)^\s*※\s*逐字稿由影片語音自動轉錄[^\n\r]*\r?\n?"
)
WEEKLY_DISCLAIMER = re.compile(
    r"(?m)^\s*※\s*本週報由\s*AI[^\n\r]*\r?\n?"
)
RESOURCE_BLOCK = re.compile(
    r"(?ms)^\s*本集講義／教材（可下載）\s*$.*?(?=^\s*📰\s*本集週報\s*$)"
)
BLOG_RELATED = re.compile(r"(?m)^\s*全部文章\s*$")
BLOG_MAILING = re.compile(r"(?m)^\s*郵件清單\s*$")
BLOG_MORE = re.compile(r"(?m)^\s*(?:更多文章|全部文章)\s*$")
VIDEO_RELATED = re.compile(r"(?m)^\s*(?:全部影片|更多影片)\s*$")
MAILING_BLOCK = re.compile(
    r"(?ms)^\s*郵件清單\s*$\n\s*加入我們的社區\s*$\n.*\Z"
)
CHAPTER_PAGER = re.compile(
    r"(?ms)^\s*(?:上一頁|上一页)\s*$\n.*?(?:下一頁|下一页)\s*$\n?\Z"
)
GENERIC_PAGER = re.compile(r"(?m)^\s*(?:上一頁|下一页|下一頁|上一页)\s*$")
CFNAVI_NAV = re.compile(
    r"(?m)^\s*(?:標籤|搜尋|最新文章|你可能會喜歡|發佈留言|上一頁|下一頁)\s*$"
)
CFNAVI_LABEL = re.compile(r"^\s*\[(?:cl|cj|inv)-[^\]]+\]\s*$")
PRESENTATION_META = re.compile(
    r"(?m)^\s*(?:投資簡報[^\n]*|編號\s*:[^\n]*|發布日期\s*:[^\n]*|"
    r"頁數\s*:[^\n]*|原始檔名\s*:[^\n]*|原始\s*PDF\s*:[^\n]*|"
    r"相關資料\s*|上一篇簡報[^\n]*|下一篇簡報[^\n]*|文件正文\s*)\r?\n?"
)
SOURCE_CITATION = re.compile(
    r"(?m)^\s*(?:來源|来源|原始文件)\s*[：:]\s*https?://[^\n\r]*\r?\n?"
)
WRITTEN_DATE = re.compile(r"(?m)^\s*撰文日期\s*[：:][^\n\r]*\r?\n?")
PHOTO_CREDIT = re.compile(r"(?m)^\s*▲\s*圖\s*[\d-]+[^\n\r]*\r?\n?")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)")
WIKI_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
FOOTNOTE_MARK = re.compile(r"\[\^[^\]]+\]")
FOOTNOTE_DEF = re.compile(r"(?m)^\s*\[\^[^\]]+\]\s*:[^\n\r]*(?:\r?\n)?")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'）】]+", re.I)
PERCENT_URL_RE = re.compile(r"\bu=https?%3A%2F%2F[^\s]+", re.I)
TRACKING_PARAM_RE = re.compile(r"[?&](?:fbclid|gclid|utm_[a-z]+)=[^\s&]+", re.I)
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
REPLACEMENT_CHARS = re.compile("[\ufffd\ufeff]")
MULTI_BLANK = re.compile(r"\n{3,}")
TRAILING_SPACE = re.compile(r"[ \t]+\n")
CAPTURE_DATE = re.compile(r"(?m)^\s*(?:采集时间|採集時間)\s*:\s*(\d{4}-\d{2}-\d{2})")
# Standard clectw / blog page footer.  The lookahead keeps the rule limited to
# the tail of a document so a book chapter that discusses risk is not cut.
STANDARD_DISCLAIMER = re.compile(
    r"(?s)(?:免責宣告|免责宣告|免責聲明|免责声明|投資風險預告|投资风险预告|"
    r"風險預告|风险预告)\s*[：:]?.{0,100}?"
    r"投資有風險\s*[，,]\s*入市需謹慎[。.!！]?"
    r"(?:\s*(?:感謝觀看|感謝收看|谢谢观看|謝謝觀看|記得訂閱[^\n]*|"
    r"在Apple Podcast上聆聽[^\n]*|在Spotify Podcast上聆聽[^\n]*|"
    r"Spotify Podcast[^\n]*|Follow 我們的 Facebook[^\n]*|"
    r"\[email protected\][^\n]*|CLEC 投資理財頻道重要資訊[^\n]*)[。.!！]?)*\s*\Z"
)
FOOTER_MARKERS = (
    "在Apple Podcast上聆聽",
    "在Spotify Podcast上聆聽",
    "Spotify Podcast",
    "Follow 我們的 Facebook",
    "[email protected]",
    "CLEC 投資理財頻道重要資訊",
)
FOOTER_LINE = re.compile(
    r"(?m)^\s*(?:在Apple Podcast上聆聽|在Spotify Podcast上聆聽|Spotify Podcast|"
    r"Follow 我們的 Facebook[^\n]*|\[email protected\][^\n]*|"
    r"CLEC 投資理財頻道重要資訊[^\n]*)\s*$\n?"
)
CELL_COORD = re.compile(r"^\s*[A-Z]{1,3}\d+\s*:\s*")
CELL_COORD_ANY = re.compile(r"(?:^|(?<=\s))[A-Z]{1,3}\d+\s*:\s*")
LOCAL_PATH = re.compile(r"(?:/Users/[^\s\t]+|/Volumes/[^\s\t]+|[A-Za-z]:\\\\Users\\\\[^\s\t]+)")
FORMULA_NOTE = re.compile(r"\s*\[公式:[^\]]*\]")
VERSION_RE = re.compile(r"(?i)(?:^|[_\-\s])v?(\d{1,3}(?:\.\d{1,3}){1,3})(?:[_\-\s.]|$)")
SITE_SUFFIXES = (
    re.compile(r"\s*[|｜]\s*CLEC\s*投資理財\s*$"),
    re.compile(r"\s*[—–-]\s*CLEC\s*(?:台灣|臺灣)\s*$"),
    re.compile(r"\s*[—–-]\s*乘豐領航生命艦隊\s*$"),
    re.compile(r"\s*[—–-]\s*CLEC\s*投資理財\s*$"),
)
AUDIO_SUFFIX = re.compile(r"\s*\(\s*\d+\s*kbit[_A-Za-z]*\s*\)\s*$", re.I)
FILE_SUFFIX = re.compile(
    r"(?:\.(?:txt|md|srt|vtt|html?|json|pdf|docx?|pptx?|xlsx?|epub|zip))+$",
    re.I,
)
LEADING_BULLET = re.compile(r"^\s*(?:[•·●▪◦]\s*)+")
MULTISPACE = re.compile(r"[ \t\u3000]+")


# ---------------------------------------------------------------------------
# Small data containers
# ---------------------------------------------------------------------------

@dataclass
class Cleaned:
    """Cleaned text plus an audit trail of applied rules."""

    text: str
    rules: list[str] = field(default_factory=list)
    before: str = ""
    meta: dict = field(default_factory=dict)

    def add_rule(self, rule: str) -> None:
        if rule not in self.rules:
            self.rules.append(rule)


@dataclass
class ClectwPage:
    summary: str
    title: str
    category: str
    date: str | None
    origin_url: str
    rules: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Basic text hygiene
# ---------------------------------------------------------------------------

def repair_text(text: str) -> str:
    """Normalise newlines and remove characters that break JSONL cleanliness."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = html.unescape(text)
    text = text.replace("\u00a0", " ").replace("\u2028", "\n").replace("\u2029", "\n")
    text = CONTROL_CHARS.sub("", text)
    text = REPLACEMENT_CHARS.sub("", text)
    return text


def normalize_whitespace(text: str) -> str:
    text = repair_text(text)
    text = TRAILING_SPACE.sub("\n", text)
    text = MULTI_BLANK.sub("\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return text.strip()


def strip_web_noise(text: str) -> str:
    """Remove tracking URLs and link markup that are not editorial content."""
    text = IMAGE_LINK.sub(r"\1", text)
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = WIKI_LINK.sub(r"\1", text)
    # PDF extraction often wraps a URL after the scheme: "https:/\n/docs...".
    text = re.sub(r"(?i)\b(https?)\s*:\s*/\s*\n?\s*/", r"\1://", text)
    text = PERCENT_URL_RE.sub("", text)
    text = URL_RE.sub("", text)
    text = TRACKING_PARAM_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def normalize_title(title: str, fallback: str = "") -> str:
    """Normalise a display title without converting Traditional/Simplified."""
    value = repair_text(html.unescape(title or "")).strip()
    value = LEADING_BULLET.sub("", value)
    value = re.sub(r"^#+\s*", "", value)
    value = re.sub(r"^語音筆記\s*[：:]\s*", "", value)
    value = re.sub(r"^(?:标题|標題)\s*[：:]\s*", "", value)
    value = AUDIO_SUFFIX.sub("", value)
    value = re.sub(r"\s*[Ww]ith\s+[Ss]ubtitles\s*", " ", value)
    for _ in range(3):
        value = FILE_SUFFIX.sub("", value)
    for suffix in SITE_SUFFIXES:
        value = suffix.sub("", value)
    value = URL_RE.sub("", value)
    value = value.strip(" \t'\"“”‘’")
    value = MULTISPACE.sub(" ", value).strip()
    if not value:
        value = normalize_whitespace(fallback)
    return value


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _make_date(year: int, month: int, day: int) -> str | None:
    if not (1900 <= year <= 2100):
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_date(value: object) -> str | None:
    """Return a strict ``YYYY-MM-DD`` date or ``None``.

    Empty strings, malformed dates and invalid leap days are rejected rather
    than silently coerced.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None

    # RFC-2822 dates from RSS feeds.
    if re.search(r"[A-Za-z]{3},?\s+\d{1,2}\s+[A-Za-z]{3}", text):
        try:
            return parsedate_to_datetime(text).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass

    patterns = (
        (r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)", (1, 2, 3)),
        (r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]?", (1, 2, 3)),
        (r"(?<!\d)(\d{4})\s+(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]?", (1, 2, 3)),
        (r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})(?!\d)", (3, 1, 2)),
        (r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", (1, 2, 3)),
    )
    for pattern, order in patterns:
        for match in re.finditer(pattern, text):
            parts = [int(match.group(i)) for i in order]
            result = _make_date(*parts)
            if result:
                return result

    # "May 20, 2016" / "20 May 2016"
    match = re.search(
        r"(?:(?P<mon1>[A-Za-z]{3,9})\s+(?P<day1>\d{1,2}),?\s+(?P<year1>\d{4}))"
        r"|(?:(?P<day2>\d{1,2})\s+(?P<mon2>[A-Za-z]{3,9})\s+(?P<year2>\d{4}))",
        text,
    )
    if match:
        if match.group("mon1"):
            month = _MONTHS.get(match.group("mon1").lower())
            day, year = int(match.group("day1")), int(match.group("year1"))
        else:
            month = _MONTHS.get(match.group("mon2").lower())
            day, year = int(match.group("day2")), int(match.group("year2"))
        if month:
            return _make_date(year, month, day)
    return None


def first_date(*values: object) -> str | None:
    for value in values:
        result = normalize_date(value)
        if result:
            return result
    return None


def capture_date_from_text(text: str | None) -> str | None:
    """Return the ``采集时间`` value of a capture header, if present."""
    if not text:
        return None
    match = CAPTURE_DATE.search(repair_text(text))
    return normalize_date(match.group(1)) if match else None


def parse_duration(text: str) -> int | None:
    """Parse a clectw header duration such as ``3:18:11`` or ``6:27``."""
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2}):(\d{2})(?!\d)", text or "")
    if match:
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
    match = re.search(r"(?<!\d)(\d{1,3}):(\d{2})(?!\d)", text or "")
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    return None


def remove_standard_footer(text: str) -> Cleaned:
    """Strip only the site-wide footer, never a body paragraph about risk.

    The disclaimer rule is anchored at ``\\Z`` (allowing only known footer
    links afterwards), so a book chapter that discusses
    ``投資有風險，入市需謹慎`` in the middle of its prose is left untouched.
    """
    before = text
    rules: list[str] = []
    match = STANDARD_DISCLAIMER.search(text)
    if match and match.start() >= min(80, len(text) // 2):
        text = text[: match.start()].rstrip()
        rules.append("clectw_standard_disclaimer")
    elif match:
        # Very short documents may be nothing but the footer; keep the body
        # before it if there is one, otherwise drop the footer.
        head = text[: match.start()].strip()
        if len(head) >= 40:
            text = head
            rules.append("clectw_standard_disclaimer")

    # Blog / X-post footers are concatenated into the last line.  Truncate
    # from the first footer marker when it appears in the trailing section.
    positions = [text.find(marker) for marker in FOOTER_MARKERS]
    positions = [pos for pos in positions if pos >= 0]
    if positions:
        pos = min(positions)
        tail = text[pos:]
        if pos >= len(text) * 0.5 or len(tail) <= 400:
            text = text[:pos].rstrip(" \t，,。；;：:")
            rules.append("podcast_social_footer")
    if FOOTER_LINE.search(text):
        text = FOOTER_LINE.sub("", text)
        rules.append("podcast_social_footer")
    return Cleaned(text=text, rules=list(dict.fromkeys(rules)), before=before)


def book_identity(name: str) -> tuple[str, tuple[int, ...], str] | None:
    """Identify versioned books by normalised title + language/script.

    Returns ``(group, version_tuple, language)`` or ``None``.  The grouping is
    intentionally conservative: only known CLEC book families with an
    explicit version number are collapsed, so unrelated lecture notes are not
    merged into one work.
    """
    value = normalize_title(name or "")
    lower = value.lower()
    family = ""
    base = ""
    if "wisdom-of-clec" in lower or "wisdom of clec" in lower or "clec寶典" in lower or "clec宝典" in lower:
        family, base = "clec-wisdom", "clec-wisdom"
    elif "wealth-bible" in lower or "wealth bible" in lower or "理財聖經" in lower or "理财圣经" in lower:
        family, base = "wealth-bible", "wealth-bible"
    if not family:
        return None
    match = VERSION_RE.search(value)
    if not match:
        return None
    try:
        version = tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None
    language = "zh"
    if re.search(r"(?i)(?:^|[_\-\s])en(?:[_\-\s.]|$)|英文", value):
        language = "en"
    elif re.search(r"(?i)(?:^|[_\-\s])sc(?:[_\-\s.]|$)|简", value):
        language = "sc"
    elif re.search(r"(?i)(?:^|[_\-\s])zh(?:[_\-\s.]|$)", value):
        language = "zh"
    return f"{base}-{language}", version, language



# ---------------------------------------------------------------------------
# Generic web chrome
# ---------------------------------------------------------------------------

def clean_capture_header(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    match = CAPTURE_HEADER.match(text)
    if match:
        text = text[match.end():]
        rules.append("web_capture_header")
    return Cleaned(text=text, rules=rules, before=before)


def clean_drive_document(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    title = ""
    match = DRIVE_HEADER.match(text)
    if match:
        title = normalize_title(match.group("title"))
        text = text[match.end():]
        rules.append("drive_capture_header")
    else:
        # Some captures use a variant header; remove any residual label lines.
        variant = re.match(
            r"\A(?:来源|來源)\s*:\s*https?://[^\n\r]*\r?\n"
            r"(?:(?:标题|文件类型|文件類型|提取方法)\s*:[^\n\r]*\r?\n)+"
            r"(?:\r?\n)?",
            text,
        )
        if variant:
            text = text[variant.end():]
            rules.append("drive_capture_header_variant")
    text, extra = _remove_source_citation(text)
    rules.extend(extra)
    standard = remove_standard_footer(text)
    text = standard.text
    rules.extend(standard.rules)
    text = normalize_whitespace(text)
    text = normalize_whitespace(strip_web_noise(text))
    return Cleaned(text=text, rules=rules, before=before, meta={"title": title})


def _remove_source_citation(text: str) -> tuple[str, list[str]]:
    rules: list[str] = []
    if SOURCE_CITATION.search(text):
        text = SOURCE_CITATION.sub("", text)
        rules.append("source_citation_line")
    return text, rules


def remove_clectw_footer(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    # Keep only the part before the related-episodes block.  The transcript
    # disclaimer is site chrome and is not part of the editorial summary.
    marker_positions = [
        pos for pos in (
            text.find("※ 逐字稿由影片語音"),
            text.find("▶ 相關集數"),
        ) if pos >= 0
    ]
    if marker_positions:
        text = text[: min(marker_positions)]
        rules.append("clectw_episode_footer")
    text = TRANSCRIPT_DISCLAIMER.sub("", text)
    text = RELATED_EPISODES.sub("", text)
    return Cleaned(text=text, rules=rules, before=before)


def remove_blog_footer(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    positions = [
        pos for pos in (
            text.rfind("\n全部文章\n"),
            text.rfind("\n郵件清單\n"),
            text.rfind("\n你可能會喜歡\n"),
        ) if pos >= 0
    ]
    if positions:
        text = text[: min(positions)]
        rules.append("blog_related_footer")
    if MAILING_BLOCK.search(text):
        text = MAILING_BLOCK.sub("", text)
        rules.append("publisher_mailing_footer")
    return Cleaned(text=text, rules=rules, before=before)


def remove_chapter_pager(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    if CHAPTER_PAGER.search(text):
        text = CHAPTER_PAGER.sub("", text)
        rules.append("book_pagination_footer")
    return Cleaned(text=text, rules=rules, before=before)


def remove_generic_pager_lines(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    if GENERIC_PAGER.search(text):
        text = GENERIC_PAGER.sub("", text)
        rules.append("generic_pager")
    return Cleaned(text=text, rules=rules, before=before)


# ---------------------------------------------------------------------------
# clectw pages: split editorial summary from the embedded transcript
# ---------------------------------------------------------------------------

def parse_clectw_header(text: str) -> dict:
    """Extract title/category/date/duration from a clectw episode header.

    The capture header must be removed *before* scanning: otherwise the
    ``采集时间: 2026-11-06`` fallback is mistaken for the episode date and
    propagated to every Drive transcript of the same work.
    """
    text = repair_text(text)
    text, _ = _strip_capture(text)
    lines = [line.strip() for line in text.splitlines()]
    category = ""
    title = ""
    date_value = None
    duration_sec = None
    header_line = ""
    header_index = -1
    skip_prefixes = (
        "来源", "來源", "采集时间", "採集時間", "播完",
        "标题", "標題", "文件类型", "文件類型",
    )
    for index, line in enumerate(lines[:10]):
        if line.startswith(skip_prefixes):
            continue
        if "收藏" in line and len(line) < 80:
            header_line = line
            header_index = index
            break
    if header_index >= 0:
        category = header_line.split()[0] if header_line.split() else ""
        date_value = normalize_date(header_line)
        duration_sec = parse_duration(header_line)
        for line in lines[header_index + 1:header_index + 4]:
            if line and not line.startswith(("📰", "本集講義", "來源")):
                title = normalize_title(line)
                break
    else:
        for line in lines[:8]:
            if line.startswith(skip_prefixes):
                continue
            if len(line) >= 4:
                title = normalize_title(line)
                break
    if not date_value:
        for line in lines[:8]:
            if line.startswith(skip_prefixes):
                continue
            date_value = normalize_date(line)
            if date_value:
                break
    return {
        "title": title,
        "category": category,
        "date": date_value,
        "duration_sec": duration_sec,
        "header_line": header_line,
    }


def split_clectw_page(text: str) -> tuple[str, str]:
    """Return ``(summary_part, transcript_part)`` for a clectw page.

    clectw episode pages contain an AI-written weekly summary followed by the
    full auto transcript.  We only keep the summary for ``kind=summary``; the
    transcript has its own source directory and, when available, a Drive SRT
    with timestamps.
    """
    text = repair_text(text)
    summary_end = -1
    for marker in ("※ 本週報由 AI", "※ 本週報由ＡＩ", "※ 逐字稿由影片語音"):
        pos = text.find(marker)
        if pos >= 0 and (summary_end < 0 or pos < summary_end):
            summary_end = pos
    if summary_end >= 0:
        summary = text[:summary_end]
        transcript = text[summary_end:]
    else:
        summary, transcript = text, ""
    return summary, transcript


def clean_clectw_summary(text: str) -> ClectwPage:
    before = text
    header = parse_clectw_header(text)
    text, capture_rules = _strip_capture(text)
    text = PLAYER_CONTROL.sub("", text)
    summary, _ = split_clectw_page(text)
    summary = RESOURCE_BLOCK.sub("", summary)
    summary = WEEKLY_DISCLAIMER.sub("", summary)
    summary = TRANSCRIPT_DISCLAIMER.sub("", summary)
    summary = RELATED_EPISODES.sub("", summary)

    lines = summary.splitlines()
    cleaned_lines: list[str] = []
    title_seen = 0
    header_line = header.get("header_line", "")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if stripped == header_line or ("收藏" in stripped and len(stripped) < 80 and not cleaned_lines):
            continue
        if header["title"] and normalize_title(stripped) == header["title"] and title_seen == 0:
            title_seen = 1
            continue
        if stripped.startswith(("📰 本集週報", "📰本集週報")):
            continue
        if stripped.startswith("本集講義／教材"):
            continue
        if stripped.startswith("※ 本週報由 AI"):
            continue
        cleaned_lines.append(line)
    summary = normalize_whitespace(strip_web_noise("\n".join(cleaned_lines)))
    footer = remove_standard_footer(summary)
    summary = normalize_whitespace(footer.text)

    rules = list(capture_rules)
    rules.extend(footer.rules)
    if PLAYER_CONTROL.search(before):
        rules.append("episode_player_control")
    if RESOURCE_BLOCK.search(before):
        rules.append("episode_resource_links")
    if SUMMARY_MARKER_FOUND(before):
        rules.append("weekly_ai_disclaimer")
    if "▶ 相關集數" in before:
        rules.append("clectw_episode_footer")
    if "本集講義／教材" in before:
        rules.append("episode_resource_links")
    return ClectwPage(
        summary=summary,
        title=header["title"],
        category=header["category"],
        date=header["date"],
        origin_url="",
        rules=list(dict.fromkeys(rules)),
    )


def SUMMARY_MARKER_FOUND(text: str) -> bool:
    return "※ 本週報由 AI" in text or "※ 本週報由ＡＩ" in text


def _strip_capture(text: str) -> tuple[str, list[str]]:
    match = CAPTURE_HEADER.match(text)
    if match:
        return text[match.end():], ["web_capture_header"]
    return text, []


def clean_clectw_transcript(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    text, capture_rules = _strip_capture(repair_text(text))
    rules.extend(capture_rules)
    text = PLAYER_CONTROL.sub("", text)
    if "播完自動接下一集" in before:
        rules.append("episode_player_control")
    footer = remove_clectw_footer(text)
    text = footer.text
    rules.extend(footer.rules)
    standard = remove_standard_footer(text)
    text = standard.text
    rules.extend(standard.rules)
    return Cleaned(text=normalize_whitespace(strip_web_noise(text)), rules=list(dict.fromkeys(rules)), before=before)


# ---------------------------------------------------------------------------
# clec168
# ---------------------------------------------------------------------------

def clean_clec168_blog(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    text, capture_rules = _strip_capture(repair_text(text))
    rules.extend(capture_rules)

    # Blog captures contain a truncated excerpt first, then a 來源: link, then
    # the full body.  Dropping the excerpt removes an accidental duplicate.
    source_match = re.search(r"(?m)^\s*(?:來源|来源)\s*[：:]\s*https?://", text)
    if source_match:
        text = text[source_match.end():]
        newline = text.find("\n")
        text = text[newline + 1:] if newline >= 0 else ""
        rules.append("blog_excerpt_before_source")

    footer = remove_blog_footer(text)
    text = footer.text
    rules.extend(footer.rules)
    text = WRITTEN_DATE.sub("", text)
    if "撰文日期" in before:
        rules.append("written_date_line")
    text = re.sub(r"(?m)^\s*\d{4}/\d{1,2}/\d{1,2}\s*$", "", text)
    text = BLOG_MORE.sub("", text)
    text = re.sub(r"(?:來源|来源)\s*[：:]\s*", "", text)
    standard = remove_standard_footer(text)
    text = standard.text
    rules.extend(standard.rules)
    return Cleaned(text=normalize_whitespace(strip_web_noise(text)), rules=list(dict.fromkeys(rules)), before=before)


def clean_clec168_doc(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    text, capture_rules = _strip_capture(repair_text(text))
    rules.extend(capture_rules)
    pager = remove_chapter_pager(text)
    text = pager.text
    rules.extend(pager.rules)
    text, citation = _remove_source_citation(text)
    rules.extend(citation)
    text = PHOTO_CREDIT.sub("", text)
    text = re.sub(r"(?m)^\s*(?:上一頁|上一页|下一頁|下一页)\s*$\n?", "", text)
    standard = remove_standard_footer(text)
    text = standard.text
    rules.extend(standard.rules)
    return Cleaned(text=normalize_whitespace(strip_web_noise(text)), rules=list(dict.fromkeys(rules)), before=before)


def clean_clec168_presentation(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    text, capture_rules = _strip_capture(repair_text(text))
    rules.extend(capture_rules)
    if "文件正文" in text:
        body_start = text.find("文件正文")
        text = text[body_start + len("文件正文"):]
        rules.append("presentation_metadata_block")
    text = PRESENTATION_META.sub("", text)
    text = re.sub(r"(?m)^\s*\d{5}\s+\d{4}年\d{1,2}月\d{1,2}日\s*$", "", text)
    text = re.sub(r"(?m)^\s*\d{5}\s+\d{4}/\d{1,2}/\d{1,2}\s*$", "", text)
    text = SOURCE_CITATION.sub("", text)
    standard = remove_standard_footer(text)
    text = standard.text
    rules.extend(standard.rules)
    return Cleaned(text=normalize_whitespace(strip_web_noise(text)), rules=list(dict.fromkeys(rules)), before=before)


def clean_clec168_html_title(raw_title: str) -> str:
    value = re.sub(r"\s*[|｜]\s*CLEC\s*投資理財\s*$", "", raw_title or "")
    return normalize_title(value)


# ---------------------------------------------------------------------------
# cfnavi
# ---------------------------------------------------------------------------

def clean_cfnavi(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    text = repair_text(text)
    header = re.match(r"\A\s*(?:来源|來源)\s*:\s*https?://[^\n\r]*\r?\n(?:\r?\n)?", text)
    if header:
        text = text[header.end():]
        rules.append("web_capture_header")
    nav = CFNAVI_NAV.search(text)
    if nav:
        text = text[:nav.start()]
        rules.append("cfnavi_navigation_footer")
    lines = text.splitlines()
    label = ""
    title = ""
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not label and CFNAVI_LABEL.match(stripped):
            label = stripped.strip("[]")
            rules.append("cfnavi_category_label")
            continue
        if not title:
            title = normalize_title(stripped)
            continue
        if title and ("pic.twitter.com" in stripped or "pic.x.com" in stripped):
            rules.append("cfnavi_social_card")
            continue
        if title and stripped.startswith("[") and "]" in stripped and len(stripped) < 120:
            remainder = stripped.split("]", 1)[1].strip()
            if remainder:
                title = normalize_title(remainder)
            continue
        cleaned.append(line)
    text = "\n".join(cleaned)
    text = re.sub(r"(?m)^\s*#.*$", "", text)  # tag clouds
    text = re.sub(r"\s*pic\.(?:twitter|x)\.com/\S+", "", text)
    standard = remove_standard_footer(text)
    text = standard.text
    rules.extend(standard.rules)
    return Cleaned(
        text=normalize_whitespace(strip_web_noise(text)),
        rules=list(dict.fromkeys(rules)),
        before=before,
        meta={"title": title, "label": label},
    )


# ---------------------------------------------------------------------------
# Markdown / local documents
# ---------------------------------------------------------------------------

def clean_markdown(text: str) -> Cleaned:
    before = text
    rules: list[str] = []
    text = repair_text(text)
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4:]
            rules.append("yaml_frontmatter")
    text = re.sub(r"(?m)^\s*import\s+.*$", "", text)
    text = re.sub(r"(?m)^\s*export\s+.*$", "", text)
    if HTML_COMMENT.search(text):
        text = HTML_COMMENT.sub("", text)
        rules.append("html_comment")
    if IMAGE_LINK.search(text):
        text = IMAGE_LINK.sub(r"\1", text)
        rules.append("markdown_image")
    if MARKDOWN_LINK.search(text):
        text = MARKDOWN_LINK.sub(r"\1", text)
        rules.append("markdown_link")
    if WIKI_LINK.search(text):
        text = WIKI_LINK.sub(r"\1", text)
        rules.append("wiki_link")
    text = FOOTNOTE_DEF.sub("", text)
    text = FOOTNOTE_MARK.sub("", text)
    text = re.sub(r"(?m)^\s*---+\s*$", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = strip_web_noise(text)
    return Cleaned(text=normalize_whitespace(text), rules=list(dict.fromkeys(rules)), before=before)


def clean_local_document(text: str) -> Cleaned:
    before = text
    text = repair_text(text)
    text = text.replace("\x0c", "\n")
    text = re.sub(r"(?m)^\s*---+\s*$", "", text)
    text = normalize_whitespace(text)
    text = normalize_whitespace(strip_web_noise(text))
    return Cleaned(text=text, rules=["local_pdf_text"], before=before)


def clean_spreadsheet(text: str, max_rows: int = 6, scenario: str = "") -> Cleaned:
    """Compact an .xlsx text dump while preserving scenario semantics.

    The six spreadsheets in ``text/local`` contain 7,713 individual cells.  We
    keep the sheet name, scenario parameter (30% vs 50%), column names and a
    handful of key summary rows.  Cell coordinates (``B11:``), local absolute
    paths and formula dumps are removed so the numbers remain searchable
    without flooding the corpus with cell fragments.
    """
    before = text
    lines = repair_text(text).splitlines()
    sheets: list[tuple[str, list[str]]] = []
    sheet = ""
    rows: list[str] = []

    def flush() -> None:
        nonlocal rows
        if sheet or rows:
            sheets.append((sheet, rows))
        rows = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("--- xl/worksheets/"):
            flush()
            sheet = stripped.strip("- ").strip()
            continue
        if not CELL_COORD.match(stripped):
            continue
        cleaned = CELL_COORD.sub("", stripped)
        cleaned = CELL_COORD_ANY.sub("", cleaned)
        cleaned = LOCAL_PATH.sub("", cleaned)
        cleaned = FORMULA_NOTE.sub("", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        cells = [cell.strip() for cell in cleaned.split("\t") if cell.strip()]
        if cells:
            rows.append(" | ".join(cells))
    flush()

    header_keywords = (
        "年份", "日期", "資產", "资产", "配置", "回報", "回报", "現金", "现金",
        "QQQ", "QLD", "開銷", "开销", "負債", "负债", "淨資產", "净资产",
        "適足率", "适足率", "Beta", "金額", "金额", "比例", "資料", "资料",
    )
    parts: list[str] = []
    if scenario:
        parts.append(f"情景參數：{scenario}")
    for sheet_name, row_list in sheets:
        if not row_list:
            continue
        sheet_bits: list[str] = []
        if sheet_name:
            sheet_bits.append(f"工作表：{sheet_name}")
        caption = [
            row for row in row_list
            if ("狀況" in row or "资产模拟" in row or "資產模擬" in row
                or "個人資料" in row or "个人资料" in row or "時間採" in row)
        ][:5]
        if caption:
            sheet_bits.append("情景說明：" + "；".join(dict.fromkeys(caption))[:600])
        header_rows = sorted(
            row_list,
            key=lambda row: sum(
                1 for keyword in header_keywords if keyword.lower() in row.lower()
            ),
            reverse=True,
        )[:2]
        headers = [
            row for row in header_rows
            if sum(1 for keyword in header_keywords if keyword.lower() in row.lower()) >= 2
        ]
        if headers:
            sheet_bits.append("表頭：" + "；".join(headers)[:500])
        data_rows = [
            row for row in row_list
            if re.search(r"狀況|状况", row)
            and len(re.findall(r"-?\d+(?:\.\d+)?", row)) >= 2
            and row not in headers
        ][:max_rows]
        if not data_rows:
            data_rows = [
                row for row in row_list
                if len(re.findall(r"-?\d+(?:\.\d+)?", row)) >= 2 and row not in headers
            ][:max_rows]
        if data_rows:
            sheet_bits.append("關鍵資料列：" + "；".join(data_rows)[:900])
        if sheet_bits:
            parts.append("\n".join(sheet_bits))

    if not parts:
        # Fall back to the first non-empty lines if the dump has no cell grid.
        parts = [
            CELL_COORD.sub("", line.strip())
            for line in lines
            if line.strip()
        ][: max_rows + 1]
    text = "\n\n".join(parts)
    text = normalize_whitespace(text)
    return Cleaned(text=text, rules=["spreadsheet_compacted"], before=before)


def is_listing_page(text: str) -> bool:
    stripped = normalize_whitespace(text)
    if len(stripped) >= 700:
        return False
    lines = [line for line in stripped.splitlines() if line.strip()]
    if not lines:
        return True
    listing_markers = (
        "全部文章", "更多文章", "全部影片", "更多影片", "共 2,156 集",
        "來自我們的最新新聞和更新", "投資簡報", "文件中心",
    )
    marker_hits = sum(1 for line in lines if any(marker in line for marker in listing_markers))
    link_like = sum(1 for line in lines if re.match(r"^\s*(?:\[[^\]]+\]|https?://)", line))
    return marker_hits >= 1 or link_like >= max(2, len(lines) // 3)


def split_lines_for_chunking(text: str) -> Iterable[str]:
    for line in normalize_whitespace(text).splitlines():
        if line.strip():
            yield line
