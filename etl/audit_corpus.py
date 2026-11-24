"""Independent quality audit for the CLEC corpus.

This script does not trust the build report.  It reads every line of
``dist/corpus.jsonl``, independently verifies the corpus hard thresholds and
the new P0/P1 invariants, and writes ``dist/etl-audit.md``.  All audit samples
are selected from the final ``corpus.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import schema

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config/source_policy.json"
DEFAULT_BLOCKED = ("ep-00176", "ep-00260", "ep-00295")
BLOCKED_DRIVE_SLOTS = {
    "00269": "ep-00176",
    "00221": "ep-00260",
    "00300": "ep-00295",
}
# Content markers taken from the blocked pages themselves.  Episode titles and
# one-line descriptions also appear in the legitimate "CLEC 精選輯目錄"
# catalogue, so they are metadata rather than leaked content.  The marker below
# appears in exactly the three blocked clectw pages; the repeated "出品人" is
# the hallucinated body of the 00300 Drive SRT.
CONTENT_FINGERPRINTS = {
    "ep-00176": ("這一集的內容整理還在進行中",),
    "ep-00260": ("這一集的內容整理還在進行中",),
    "ep-00295": ("這一集的內容整理還在進行中", "出品人出品人出品人"),
}
CAPTURE_DATE = "2026-11-06"
TEMPLATE_PATTERNS = {
    "web_capture_header": re.compile(
        r"(?m)^\s*(?:来源|來源)\s*:\s*https?://(?:www\.)?"
        r"(?:clec168|clectw|cfnavi|drive\.google|docs\.google)"
    ),
    "capture_timestamp": re.compile(r"(?m)^\s*(?:采集时间|採集時間)\s*:\s*\d{4}-"),
    "player_control": re.compile(r"(?m)^\s*播完自動接下一集\s*$"),
    "transcript_disclaimer": re.compile(r"※\s*逐字稿由影片語音"),
    "related_episodes": re.compile(r"▶\s*相關集數"),
    "related_articles": re.compile(r"(?m)^\s*(?:全部文章|更多文章|全部影片|更多影片)\s*$"),
    "mailing_list": re.compile(r"(?m)^\s*郵件清單\s*$"),
    "pager": re.compile(r"(?m)^\s*(?:上一頁|上一页|下一頁|下一页)\s*$"),
    "ai_page_disclaimer": re.compile(r"本頁內容由 AI"),
    "resource_downloads": re.compile(r"(?m)^\s*本集講義／教材（可下載）"),
    "author_category_labels": re.compile(r"(?m)^\s*(?:作者|分類)\s*$"),
    "presentation_metadata": re.compile(
        r"(?m)^\s*(?:原始檔名|原始\s*PDF|相關資料|上一篇簡報|下一篇簡報)\s*[：:]?"
    ),
    "drive_header_labels": re.compile(
        r"(?m)^\s*(?:标题|文件类型|文件類型|提取方法)\s*[：:]"
    ),
    "favorite_chrome": re.compile(r"(?m)^\s*收藏\s*$"),
    "video_intro": re.compile(r"無法播放此影片|影片資訊\s*\d{5}"),
    "clectw_standard_disclaimer": re.compile(
        r"免責宣告.{0,100}?投資有風險\s*[，,]\s*入市需謹慎", re.S
    ),
    "facebook_follow": re.compile(r"Follow\s*我們的\s*Facebook"),
    "cloudflare_email": re.compile(r"\[email protected\]"),
    "clec_channel_links": re.compile(r"CLEC\s*投資理財頻道重要資訊"),
    "podcast_social_footer": re.compile(
        r"在Apple Podcast上聆聽|在Spotify Podcast上聆聽|Spotify Podcast"
    ),
    "html_entity": re.compile(r"&amp;"),
}
THRESHOLD_TEMPLATE = 0.005
THRESHOLD_COMPLEMENT = 2000
THRESHOLD_LENGTH = 0.95

# Residual title branding / subscription CTA.  A title is flagged when the
# channel brand is effectively at the end (only a short noise fragment may
# follow and it must not contain a date).  Semantic mentions such as
# "CLEC 投資理財頻道的成功分享 2025-02-02" are not suffixes and must not be
# reported as template pollution.
TITLE_CHANNEL_TOKEN = re.compile(
    r"(?i)(?:[|｜]\s*)?(?:CLEC|CLE|CRAC)?\s*"
    r"(?:投資理財(?:教育)?頻道|投資(?:理財|教育)?頻道|理財投資頻道|"
    r"投理財頻道|投資裡材頻道|投在理財頻道)"
)
TITLE_DATE = re.compile(
    r"\d{4}\s*[年/.\-]\s*\d{1,2}"
    r"|\d{1,2}\s*[/⁄.]\s*\d{1,2}\s*[/⁄.]\s*\d{2,4}"
)
TITLE_CTA = re.compile(
    r"漏掉我們的直播通知"
    r"|(?:訂[閲閱]|订阅)\s*👍"
    r"|(?:開啟|打開|打开)\s*鈴鐺"
    r"|(?:請訂閱|請訂閲|請订阅)\s*[、,，]?\s*(?:分享|轉發|转发|按)"
)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_jsonl(path: Path) -> list[dict]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_policy_blocked() -> tuple[str, ...]:
    policy = load_json(CONFIG_PATH)
    blocked = (
        (policy.get("sources") or {})
        .get("quarantine", {})
        .get("blocked_works")
    )
    return tuple(blocked or DEFAULT_BLOCKED)


def template_hits(rows: list[dict]) -> tuple[list[dict], Counter]:
    hits: list[dict] = []
    counter: Counter = Counter()
    for row in rows:
        haystack = "\n".join(
            str(row.get(key, "")) for key in ("title", "heading", "body")
        )
        matched = [name for name, pattern in TEMPLATE_PATTERNS.items() if pattern.search(haystack)]
        if matched:
            counter.update(matched)
            hits.append(
                {
                    "doc_id": row.get("doc_id", ""),
                    "work_id": row.get("work_id", ""),
                    "source_id": row.get("source_id", ""),
                    "patterns": matched,
                    "excerpt": haystack[:240],
                }
            )
    return hits, counter


def other_kinds_mismatches(rows: list[dict]) -> list[dict]:
    """Rows whose ``other_kinds`` disagrees with the final corpus itself.

    The expected set is derived independently from every surviving passage:
    the union of ``other_kinds`` and the row's own kind must equal all kinds
    that actually exist for that ``work_id``, and a row may never list its
    own kind as complementary.
    """
    actual_kinds: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        actual_kinds[str(row.get("work_id", ""))].add(str(row.get("kind", "")))
    mismatches: list[dict] = []
    for row in rows:
        work_id = str(row.get("work_id", ""))
        kind = str(row.get("kind", ""))
        others = row.get("other_kinds")
        if not isinstance(others, list):
            others = []
        expected = actual_kinds[work_id] - {kind}
        if kind in others or set(others) != expected:
            mismatches.append(
                {
                    "doc_id": row.get("doc_id", ""),
                    "work_id": work_id,
                    "kind": kind,
                    "other_kinds": list(others),
                    "expected": sorted(expected),
                }
            )
    return mismatches


def title_template_hits(rows: list[dict]) -> list[dict]:
    """Find channel-suffix or subscription-CTA residue in final titles."""
    hits: list[dict] = []
    for row in rows:
        title = str(row.get("title", "") or "")
        patterns: list[str] = []
        matches = list(TITLE_CHANNEL_TOKEN.finditer(title))
        if matches:
            match = matches[-1]
            prefix = title[:match.start()].strip(" \t\u3000|｜-—–•,，;；:：")
            tail = title[match.end():].strip(" \t\u3000")
            if len(prefix) >= 2 and len(tail) <= 32 and not TITLE_DATE.search(tail):
                patterns.append("channel_suffix")
        if TITLE_CTA.search(title):
            patterns.append("subscription_cta")
        if patterns:
            hits.append(
                {
                    "doc_id": row.get("doc_id", ""),
                    "work_id": row.get("work_id", ""),
                    "title": title[:240],
                    "patterns": patterns,
                }
            )
    return hits


def blocked_hits(rows: list[dict], blocked_works: tuple[str, ...]) -> list[dict]:
    hits: list[dict] = []
    for row in rows:
        text = "\n".join(
            str(row.get(key, "")) for key in ("work_id", "source_id", "doc_id", "title", "body")
        ).lower()
        for blocked in blocked_works:
            if blocked in text:
                hits.append({"doc_id": row.get("doc_id", ""), "blocked": blocked})
    return hits


def blocked_slot_hits(rows: list[dict]) -> list[dict]:
    hits: list[dict] = []
    for row in rows:
        source_id = str(row.get("source_id", ""))
        work_id = str(row.get("work_id", ""))
        for slot, blocked in BLOCKED_DRIVE_SLOTS.items():
            if source_id.startswith(f"drive_srt_{slot}") or work_id == blocked:
                hits.append(
                    {
                        "doc_id": row.get("doc_id", ""),
                        "slot": slot,
                        "work_id": work_id,
                    }
                )
    return hits


def fingerprint_hits(rows: list[dict]) -> list[dict]:
    def compact(value: str) -> str:
        return re.sub(r"[\s\W_]+", "", value or "").lower()

    hits: list[dict] = []
    for row in rows:
        haystack = compact(
            "\n".join(
                str(row.get(key, ""))
                for key in ("work_id", "source_id", "title", "heading", "body")
            )
        )
        for blocked, phrases in CONTENT_FINGERPRINTS.items():
            for phrase in phrases:
                needle = compact(phrase)
                if needle and needle in haystack:
                    hits.append(
                        {
                            "doc_id": row.get("doc_id", ""),
                            "blocked": blocked,
                            "fingerprint": phrase,
                        }
                    )
                    break
    return hits


def drive_duplicate_hits(rows: list[dict]) -> list[dict]:
    hits = []
    for row in rows:
        source_id = str(row.get("source_id", ""))
        if source_id.startswith(("drive_txt_", "drive_md_")):
            hits.append({"doc_id": row.get("doc_id", ""), "source_id": source_id})
    # A work must not have two Drive transcript sources.
    by_work: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if str(row.get("source_id", "")).startswith("drive_srt_"):
            by_work[row["work_id"]].append(row["source_id"])
    for work_id, source_ids in by_work.items():
        if len(set(source_ids)) > 1:
            hits.append({"work_id": work_id, "source_ids": sorted(set(source_ids))})
    return hits


def clec168_video_hits(rows: list[dict]) -> list[dict]:
    hits = []
    for row in rows:
        source_id = str(row.get("source_id", ""))
        haystack = "\n".join(str(row.get(key, "")) for key in ("title", "body"))
        if source_id.startswith("c168_video_") or re.search(r"無法播放此影片|影片資訊\s*\d{5}", haystack):
            hits.append({"doc_id": row.get("doc_id", ""), "source_id": source_id})
    return hits


def complement_works(rows: list[dict]) -> tuple[int, int]:
    kinds: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        kinds[row.get("work_id", "")].add(row.get("kind", ""))
    with_both = sum(
        1 for values in kinds.values()
        if "transcript" in values and "summary" in values
    )
    with_many = sum(1 for values in kinds.values() if len(values) > 1)
    return with_both, with_many


def pseudo_year_works(rows: list[dict]) -> list[str]:
    works = {str(row.get("work_id", "")) for row in rows}
    return sorted(work for work in works if re.fullmatch(r"ep-02\d{3}", work))


def capture_date_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("date") == CAPTURE_DATE]


def duplicate_time_ranges(rows: list[dict]) -> list[dict]:
    counter: Counter = Counter()
    example: dict[tuple, str] = {}
    for row in rows:
        start, end = row.get("start_sec"), row.get("end_sec")
        if start is None or end is None:
            continue
        key = (row.get("source_id", ""), start, end)
        counter[key] += 1
        example.setdefault(key, row.get("doc_id", ""))
    return [
        {"source_id": key[0], "start_sec": key[1], "end_sec": key[2],
         "count": count, "doc_id": example[key]}
        for key, count in counter.items() if count > 1
    ]


def overlapping_time_ranges(rows: list[dict]) -> list[dict]:
    by_source: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for row in rows:
        start, end = row.get("start_sec"), row.get("end_sec")
        if start is None or end is None:
            continue
        by_source[str(row.get("source_id", ""))].append((start, end, str(row.get("doc_id", ""))))
    hits: list[dict] = []
    for source_id, ranges in by_source.items():
        ranges.sort()
        last_end = -1
        last_doc = ""
        for start, end, doc_id in ranges:
            if start < last_end:
                hits.append(
                    {
                        "source_id": source_id,
                        "start_sec": start,
                        "previous_end": last_end,
                        "doc_id": doc_id,
                        "previous_doc": last_doc,
                    }
                )
            if end > last_end:
                last_end = end
                last_doc = doc_id
    return hits


def cross_work_identical_bodies(rows: list[dict]) -> list[str]:
    bodies: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        bodies[row.get("body", "")].add(str(row.get("work_id", "")))
    return [body for body, works in bodies.items() if len(works) > 1]


def manifest_metrics(path: Path) -> dict:
    entries = load_jsonl(path)
    zero = [
        entry for entry in entries
        if int(entry.get("text_chars", 0)) >= 100
        and int(entry.get("passage_count", 0)) == 0
    ]
    return {
        "entries": entries,
        "documents": len(entries),
        "zero": zero,
        "exists": path.exists(),
    }


def format_table(rows: list[list[str]], headers: list[str]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def build_report(
    corpus_path: Path,
    meta_path: Path,
    out_path: Path,
    stats_path: Path,
    samples_path: Path,
    docs_path: Path,
) -> tuple[str, bool]:
    rows = list(schema.iter_corpus(corpus_path))
    meta = load_json(meta_path)
    stats = load_json(stats_path)
    samples = load_json(samples_path)
    metrics = schema.audit_rows(rows)
    total = len(rows)
    blocked_works = load_policy_blocked()

    hits, pattern_counter = template_hits(rows)
    template_rate = len(hits) / total if total else 0.0
    other_kinds_bad = other_kinds_mismatches(rows)
    title_hits = title_template_hits(rows)
    blocked = blocked_hits(rows, blocked_works)
    slot_hits = blocked_slot_hits(rows)
    fp_hits = fingerprint_hits(rows)
    drive_dupes = drive_duplicate_hits(rows)
    video_hits = clec168_video_hits(rows)
    with_both, with_many = complement_works(rows)
    quality4 = [row for row in rows if row.get("quality") == 4]
    license_full = all(row.get("license_mode") == "full" for row in rows)
    podcast_body = [
        row for row in rows
        if str(row.get("source_id", "")).startswith("podcast_meta_")
        or str(row.get("source_id", "")).startswith("podcast_rss_")
    ]
    pseudo_years = pseudo_year_works(rows)
    capture_dates = capture_date_rows(rows)
    duplicate_ranges = duplicate_time_ranges(rows)
    overlaps = overlapping_time_ranges(rows)
    cross_work_bodies = cross_work_identical_bodies(rows)
    manifest = manifest_metrics(docs_path)
    zero_passages = manifest["zero"]

    actual_works = len({row.get("work_id", "") for row in rows})
    actual_sources = len({row.get("source_id", "") for row in rows})
    meta_counts = meta.get("counts", {}) or {}
    meta_passages_ok = meta_counts.get("passages") == total
    meta_works_ok = meta_counts.get("works") == actual_works
    meta_sources_ok = meta_counts.get("sources") == actual_sources

    known_gaps = "\n".join(meta.get("known_gaps", []))
    known_gaps_ok = all(blocked_id in known_gaps for blocked_id in blocked_works)

    checks = [
        ("Schema 全部通过（doc_id/ordinal/日期/UTF-8）", not metrics["errors"], f"{len(metrics['errors'])} errors"),
        ("other_kinds 逐段互补集合准确（差异 = 0）", not other_kinds_bad, f"{len(other_kinds_bad)} mismatches"),
        ("标题频道后缀 / 订阅 CTA 残留 = 0", not title_hits, f"{len(title_hits)} rows"),
        ("段落长度 100–900 字比例 ≥ 95%", metrics["length_ok_ratio"] >= THRESHOLD_LENGTH, f"{metrics['length_ok_ratio']:.2%}"),
        ("网页模板命中率 < 0.5%（含制式页尾）", template_rate < THRESHOLD_TEMPLATE, f"{template_rate:.4%} ({len(hits)}/{total})"),
        ("隔离区 ep-00176/00260/00295 泄露 = 0", not blocked, f"{len(blocked)} hits"),
        ("隔离区真实 Drive 槽位 00269/00221/00300 = 0", not slot_hits, f"{len(slot_hits)} hits"),
        ("隔离区内容指纹泄露 = 0", not fp_hits, f"{len(fp_hits)} hits"),
        ("known_gaps 准确记录隔离作品", known_gaps_ok, "yes" if known_gaps_ok else "no"),
        ("Drive 同组 TXT/SRT/MD 重复 = 0", not drive_dupes, f"{len(drive_dupes)} hits"),
        ("transcript + summary 互补作品 ≥ 2,000", with_both >= THRESHOLD_COMPLEMENT, f"{with_both}"),
        ("clec168 纯视频介绍页入正文 = 0", not video_hits, f"{len(video_hits)} hits"),
        ("sources/podcast 正文 = 0", not podcast_body, f"{len(podcast_body)} hits"),
        ("license_mode 全量 = full", license_full, "yes" if license_full else "no"),
        ("quality=4 真实样例 ≥ 10", len(quality4) >= 10, f"{len(quality4)}"),
        ("docs_with_zero_passages == 0", manifest["exists"] and not zero_passages, f"{len(zero_passages)} / {manifest['documents']} docs"),
        ("ep-02xxx 伪年份作品 = 0", not pseudo_years, f"{len(pseudo_years)}"),
        ("date == 采集日 2026-11-06 = 0", not capture_dates, f"{len(capture_dates)} rows"),
        ("同源 (start_sec, end_sec) 重复 = 0", not duplicate_ranges, f"{len(duplicate_ranges)} keys"),
        ("同源时间戳重叠 = 0", not overlaps, f"{len(overlaps)} overlaps"),
        ("跨 work 完全相同 body = 0", not cross_work_bodies, f"{len(cross_work_bodies)} bodies"),
        ("meta passages 与 corpus 一致", meta_passages_ok, f"meta={meta_counts.get('passages')} corpus={total}"),
        ("meta works 与 corpus 一致", meta_works_ok, f"meta={meta_counts.get('works')} corpus={actual_works}"),
        ("meta sources 与 corpus 一致", meta_sources_ok, f"meta={meta_counts.get('sources')} corpus={actual_sources}"),
    ]
    passed = all(ok for _, ok, _ in checks)

    summary_rows = [
        [name, "PASS" if ok else "FAIL", detail]
        for name, ok, detail in checks
    ]

    source_rows = []
    labels = stats.get("source_labels", {})
    for key, value in stats.get("sources", {}).items():
        source_rows.append([
            labels.get(key, key),
            value.get("files", 0),
            f"{value.get('raw_chars', 0):,}",
            value.get("docs_before_dedup", 0),
            value.get("docs_after_dedup", 0),
            value.get("skipped", 0),
            value.get("passages", 0),
            f"{value.get('passage_chars', 0):,}",
        ])
    totals = [
        "合计",
        sum(row[1] for row in source_rows),
        f"{sum(int(str(row[2]).replace(',', '')) for row in source_rows):,}",
        sum(row[3] for row in source_rows),
        sum(row[4] for row in source_rows),
        sum(row[5] for row in source_rows),
        sum(row[6] for row in source_rows),
        f"{sum(int(str(row[7]).replace(',', '')) for row in source_rows):,}",
    ]
    source_rows.append(totals)

    reasons_by_id = {
        sample.get("doc_id"): sample.get("reasons", [])
        for sample in samples.get("suspect_examples", [])
    }
    suspect_sections = []
    for index, row in enumerate(quality4[:10], 1):
        reasons = reasons_by_id.get(row.get("doc_id"), [])
        suspect_sections.append(
            f"### 样例 {index}：`{row.get('doc_id', '')}`（最终 corpus.jsonl）\n\n"
            f"- work_id：`{row.get('work_id', '')}`\n"
            f"- source_id：`{row.get('source_id', '')}`\n"
            f"- 触发原因：`{'; '.join(reasons) if reasons else 'quality=4'}`\n\n"
            f"```text\n{str(row.get('body', ''))[:620]}\n```\n"
        )
    if not suspect_sections:
        suspect_sections.append("_未找到 quality=4 样例。_\n")

    hit_sections = "## 模板残留命中（最终 corpus.jsonl）\n\n"
    if hits:
        hit_sections += format_table(
            [[h["doc_id"], ", ".join(h["patterns"]), h["excerpt"][:120].replace("\n", " ")] for h in hits[:20]],
            ["doc_id", "patterns", "excerpt"],
        ) + "\n"
    else:
        hit_sections += "_全量零命中。_\n"

    template_examples = samples.get("template_examples", [])[:10]
    template_sections = []
    for index, example in enumerate(template_examples, 1):
        template_sections.append(
            f"### 样例 {index}：{example.get('source_path', '')}\n\n"
            f"- 规则：`{', '.join(example.get('rules', []))}`\n\n"
            f"**清洗前（原始抓取）**\n\n```text\n{example.get('before', '')}\n```\n\n"
            f"**清洗后（入正文）**\n\n```text\n{example.get('after', '')}\n```\n"
        )
    if not template_sections:
        template_sections.append("_未找到可展示的模板剥离样例。_\n")

    error_sections = ""
    if metrics["errors"]:
        error_sections = "## Schema 错误\n\n" + "\n".join(f"- {item}" for item in metrics["errors"][:30]) + "\n"

    manifest_zero_section = ""
    if zero_passages:
        manifest_zero_section = "## 零段落文档\n\n" + format_table(
            [[entry.get("source_id", ""), entry.get("work_id", ""), entry.get("text_chars", 0)]
             for entry in zero_passages[:20]],
            ["source_id", "work_id", "text_chars"],
        ) + "\n"

    report = f"""# CLEC 语料 ETL 独立质量审计

- 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
- corpus：`{corpus_path}`
- meta：`{meta_path}`
- doc manifest：`{docs_path}`
- 总段落数：{total:,}
- 结论：**{'PASS' if passed else 'FAIL'}**

> 审计样例全部取自最终 `corpus.jsonl`；模板命中、quality=4 样例、时间戳与去重检查均直接扫描最终产物。

## 硬阈值验收

{format_table(summary_rows, ['验收项', '结果', '实测值'])}

## 清洗前后规模对比

{format_table(source_rows, ['来源', '文件数', '原始字符', '去重前文档', '去重后文档', '跳过', '段落数', '正文字符'])}

- 去重：{stats.get('dedup', {}).get('before', 'n/a')} → {stats.get('dedup', {}).get('after', 'n/a')} 个文档；同作品同类型合并 {stats.get('dedup', {}).get('same_work_merges', 'n/a')}，5-gram 近重复合并 {stats.get('dedup', {}).get('near_duplicate_merges', 'n/a')}。
- 多版本书籍淘汰：{stats.get('book_versions_removed', 'n/a')} 个旧版本。
- 跨 work 完全重复段落删除：{stats.get('cross_work_exact_passages_removed', 'n/a')}。
- 隔离区跳过：{stats.get('skipped_blocked', 'n/a')} 个文件/文档。
- 同一 work_id 同时含 transcript 与 summary：**{with_both:,}**；多形态互补作品：{with_many:,}。
- 文档清单：{manifest['documents']:,} 个；零段落文档：{len(zero_passages)}。

## 契约与分布

- 类型分布：{json.dumps(metrics['by_kind'], ensure_ascii=False)}
- 质量分布：{json.dumps(metrics['by_quality'], ensure_ascii=False)}
- 年份分布：{json.dumps(metrics['by_year'], ensure_ascii=False)}
- 段落长度 100–900 字：{metrics['length_ok']:,} / {total:,} = {metrics['length_ok_ratio']:.2%}
- `doc_id` 唯一数：{total - len(metrics['duplicate_doc_ids']) if not metrics['duplicate_doc_ids'] else 'FAIL'}
- 来源数：{metrics['sources']:,}
- `ep-02xxx` 伪年份作品：{len(pseudo_years)}
- `other_kinds` 逐段差异：{len(other_kinds_bad)}
- 标题频道后缀 / 订阅 CTA 残留：{len(title_hits)}
- `date == {CAPTURE_DATE}` 段落：{len(capture_dates)}
- 同源重复时间戳：{len(duplicate_ranges)}；同源重叠：{len(overlaps)}
- 跨 work 完全相同 body：{len(cross_work_bodies)}

{error_sections}{manifest_zero_section}
## 已知缺口

```text
{known_gaps}
```

## quality=4（suspect）真实样例（10 条，最终 corpus.jsonl）

{''.join(suspect_sections)}

## 模板正则统计

{format_table([[name, count] for name, count in pattern_counter.most_common()], ['模板规则', '残留命中数']) if pattern_counter else '_全量零命中。_'}

{hit_sections}
## 构建期模板剥离样例（补充，不作为审计依据）

{''.join(template_sections)}

## 复现命令

```bash
python3 -m etl.build_corpus --source-dir /Users/century/Vibing/clec/collection --out-dir dist
python3 -m etl.audit_corpus --corpus dist/corpus.jsonl --meta dist/meta.json --out dist/etl-audit.md
.venv/bin/pytest -q
```
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    return report, passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=Path("dist/corpus.jsonl"))
    parser.add_argument("--meta", type=Path, default=Path("dist/meta.json"))
    parser.add_argument("--out", type=Path, default=Path("dist/etl-audit.md"))
    parser.add_argument("--stats", type=Path, default=Path("dist/build-stats.json"))
    parser.add_argument("--samples", type=Path, default=Path("dist/audit-samples.json"))
    parser.add_argument("--docs", type=Path, default=Path("dist/doc-manifest.jsonl"))
    args = parser.parse_args(argv)
    if not args.corpus.exists():
        parser.error(f"corpus not found: {args.corpus}")
    report, passed = build_report(
        args.corpus, args.meta, args.out, args.stats, args.samples, args.docs
    )
    # Print the conclusion table for CI logs.
    print(report.split("## 清洗前后规模对比")[0])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
