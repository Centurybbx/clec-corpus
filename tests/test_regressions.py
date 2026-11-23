"""Regression tests for the Stage-1 P0/P1 fixes."""

from __future__ import annotations

import json
from pathlib import Path

from etl import audit_corpus, build_corpus, chunk, clean, hallucination, schema


def _row(**overrides):
    row = {
        "doc_id": "p_ep-00001_tr_ab12cd34_0000",
        "work_id": "ep-00001",
        "source_id": "drive_srt_00001",
        "ordinal": 0,
        "kind": "transcript",
        "quality": 2,
        "date": "2024-06-15",
        "start_sec": 0,
        "end_sec": 30,
        "title": "測試標題",
        "heading": "",
        "body": "這是一段合法的正文。" * 20,
        "origin_url": "https://clectw.com/ep/ep-00001",
        "other_kinds": ["summary"],
        "license_mode": "full",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# P0-1: years are not episode numbers
# ---------------------------------------------------------------------------

def test_infer_work_id_rejects_year_prefixes():
    stems = {"ep-00001", "short-00001"}
    assert build_corpus.infer_work_id(
        "2016.07.02[投資哲學(10)] - 為何堅持長期持有的投資如此難!",
        stem_set=stems,
        fallback="drive-doc-x",
    ) == "drive-doc-x"
    assert build_corpus.infer_work_id(
        "2024.01.09第九十七章【 如何判斷基金 和 區分好壞】",
        stem_set=stems,
        fallback="drive-doc-x",
    ) == "drive-doc-x"
    assert build_corpus.infer_work_id(
        "2024 簡報組合",
        stem_set=stems,
        fallback="drive-doc-x",
    ) == "drive-doc-x"


def test_infer_work_id_uses_real_episode_number_next_to_year():
    stems = {"ep-00514", "ep-00001"}
    assert build_corpus.infer_work_id(
        "2025 (00514)組合",
        stem_set=stems,
        fallback="drive-doc-x",
    ) == "ep-00514"
    assert build_corpus.infer_work_id(
        "【00001】投資通訊 (1)2017 十二月",
        stem_set=stems,
        fallback="drive-doc-x",
    ) == "ep-00001"


def test_infer_work_id_explicit_post_and_episode_context():
    assert build_corpus.infer_work_id(
        "0025貼文", stem_set=set(), fallback="drive-doc-x"
    ) == "post-00025"
    assert build_corpus.infer_work_id(
        "【0025】年度回顧", stem_set=set(), fallback="drive-doc-x"
    ) == "ep-00025"


def test_stem_candidates_support_split_episode_suffixes():
    stems = {"ep-00480a", "ep-00480b", "ep-00481"}
    assert build_corpus._stem_candidates(stems, "00480") == ["ep-00480a", "ep-00480b"]
    assert build_corpus._stem_candidates(stems, "00480a") == ["ep-00480a"]


# ---------------------------------------------------------------------------
# P0-2: timestamped Markdown + short sections
# ---------------------------------------------------------------------------

def test_timestamped_markdown_is_parsed_as_cues():
    text = """# 語音筆記：00694

## [0:00:00,000 – 0:00:08,000]
大家好,這裡是CRAC投資理財頻道

## [0:00:08,000 – 0:00:19,000]
今天我們的課程就是2026年投資第一堂課價值10億元的投資講座
"""
    cues = chunk.parse_cue_markdown(text)
    assert len(cues) == 2
    assert cues[0].start == 0
    assert cues[0].end == 8
    assert cues[1].start == 8
    passages = chunk.chunk_srt(cues)
    assert passages
    assert "CRAC" in passages[0].body
    assert all(passage.start_sec is not None for passage in passages)


def test_short_document_under_100_chars_is_kept():
    text = "學員問答精選。" + "這是短篇摘要內容，" * 8
    assert 40 <= len(text) < 100
    passages = chunk.chunk_prose(text)
    assert len(passages) == 1
    assert passages[0].body == text


def test_short_section_is_not_silently_dropped():
    text = "長文內容。" * 180 + "\n## 短節\n" + "短節內容。" * 10
    passages = chunk.chunk_prose(text)
    assert any("短節內容" in passage.body for passage in passages)


def test_short_fragment_under_20_chars_is_dropped():
    assert chunk.chunk_prose("太短。") == []


# ---------------------------------------------------------------------------
# P1-2: SRT timestamp interpolation / no duplicated ranges
# ---------------------------------------------------------------------------

def test_overlong_single_cue_gets_interpolated_unique_ranges():
    cue = chunk.Cue(start=0.0, end=1000.0, text="沒有標點的很長句子" * 200)
    passages = chunk.chunk_srt([cue])
    assert len(passages) > 1
    ranges = [(p.start_sec, p.end_sec) for p in passages]
    assert len(ranges) == len(set(ranges))
    assert ranges[0][0] == 0
    assert ranges[-1][1] >= 990
    for left, right in zip(passages, passages[1:]):
        assert left.end_sec <= right.start_sec


def test_srt_chunking_has_no_duplicate_or_overlapping_ranges():
    cues = [
        chunk.Cue(start=index * 2.0, end=index * 2.0 + 2.0, text="這是一句沒有標點的逐字稿內容")
        for index in range(600)
    ]
    passages = chunk.chunk_srt(cues)
    ranges = [(p.start_sec, p.end_sec) for p in passages]
    assert len(ranges) == len(set(ranges))
    for left, right in zip(passages, passages[1:]):
        assert left.end_sec <= right.start_sec


# ---------------------------------------------------------------------------
# P0-3: capture date must never become the work date
# ---------------------------------------------------------------------------

def test_clectw_header_ignores_capture_date():
    raw = """来源: https://clectw.com/ep/ep-00123
采集时间: 2026-11-06 20:11:20 +0800

播完自動接下一集
長篇 00123 2020 7月10日 · 6:27 收藏
【00123】測試集標題
本週課程概述
這是一段摘要正文。
"""
    header = clean.parse_clectw_header(raw)
    assert header["date"] == "2020-07-10"
    assert header["duration_sec"] == 387
    page = clean.clean_clectw_summary(raw)
    assert page.date == "2020-07-10"


def test_normalize_date_without_year_character():
    assert clean.normalize_date("2020 7月10日") == "2020-07-10"
    assert clean.normalize_date("採集時間: 2026-11-06 20:11") == "2026-11-06"


# ---------------------------------------------------------------------------
# P1-3: standard page footers
# ---------------------------------------------------------------------------

def test_clectw_standard_disclaimer_is_stripped():
    body = "這是正常摘要內容，討論資產配置與長期投資。" * 4
    raw = body + "\n免責宣告：本篇內容僅為經驗分享，非專業投資建議。投資有風險，入市需謹慎。"
    result = clean.clean_clectw_transcript(raw)
    assert "免責宣告" not in result.text
    assert "投資有風險" not in result.text
    assert "這是正常摘要內容" in result.text


def test_mid_document_risk_sentence_is_not_stripped():
    raw = (
        "第一章討論風險。投資有風險，入市需謹慎，這是每個投資人都該記住的原則。"
        "接著我們繼續討論資產配置與再平衡的實際做法。"
    )
    result = clean.clean_drive_document(raw)
    assert "投資有風險" in result.text


def test_podcast_and_social_footer_is_stripped():
    raw = (
        "這是正文最後一段，討論長期投資。" * 4
        + "\n撰稿：James 。在Apple Podcast上聆聽：在Spotify Podcast上聆聽："
        "Follow 我們的 Facebook(Wealthin50)：有問題可以寫Email 給我！"
        "[email protected] 。CLEC 投資理財頻道重要資訊連結與最新公告"
    )
    result = clean.clean_clec168_blog(raw)
    assert "在Apple Podcast" not in result.text
    assert "Follow 我們的 Facebook" not in result.text
    assert "[email protected]" not in result.text
    assert "CLEC 投資理財頻道重要資訊" not in result.text
    assert "這是正文最後一段" in result.text


# ---------------------------------------------------------------------------
# P1-4: spreadsheets and book versions
# ---------------------------------------------------------------------------

def test_spreadsheet_keeps_scenario_and_removes_cell_coordinates():
    raw = """--- xl/worksheets/sheet1.xml ---
B11: 資產模擬結果總表-1
B13: 狀況一上漲年槓桿基金轉的部分轉轉30%
B31: 狀況八、一開始質押借款再投資；上漲年槓桿基金轉的部分轉轉30%
--- xl/worksheets/sheet2.xml ---
A9: 資產配置\tB9: 原始QQQ金額\tC9: 原始QLD 金額\tD9: 原始現金額
A10: 狀況一\tB10: 600000 [公式: '狀況一'!$P$8]\tC10: 200000\tD10: 200000
A11: 狀況二\tB11: 500000\tC11: 250000\tD11: 250000
A14: 模擬輸出資料\tB14: 最終淨資產
A15: 狀況一\tB15: 4097452.468\tC15: 4236121.907
路徑：/Users/century/secret/file.xlsx
"""
    thirty = clean.clean_spreadsheet(raw, scenario="30%")
    fifty = clean.clean_spreadsheet(raw, scenario="50%")
    assert "B11:" not in thirty.text
    assert "C12:" not in thirty.text
    assert "/Users/" not in thirty.text
    assert "30%" in thirty.text
    assert "50%" in fifty.text
    assert thirty.text != fifty.text
    assert "資產模擬結果總表" in thirty.text
    assert "最終淨資產" in thirty.text


def test_book_identity_and_versions():
    assert clean.book_identity("The-Wisdom-of-CLEC_EN_V1.14.2.pdf")[:2] == (
        "clec-wisdom-en", (1, 14, 2)
    )
    assert clean.book_identity("CLEC宝典_SC_V1.5.1.pdf.txt")[:2] == (
        "clec-wisdom-sc", (1, 5, 1)
    )
    assert clean.book_identity("CLEC寶典_ZH_V1.5.1.pdf")[:2] == (
        "clec-wisdom-zh", (1, 5, 1)
    )
    assert clean.book_identity("CLEC 精選輯目錄(更新至20260704)") is None


# ---------------------------------------------------------------------------
# P1-5: hallucination gate and synonyms
# ---------------------------------------------------------------------------

def test_english_word_fragment_does_not_trigger_repeat_signal():
    text = (
        "The energy sector is important. We discuss energy policy and energy "
        "prices. Energy companies invest in energy infrastructure while energy "
        "demand grows. This energy transition changes energy markets."
    )
    result = hallucination.check_transcript(text)
    assert not result.suspect


def test_normal_colloquial_repetition_is_not_suspect():
    text = (
        "這個這個這個問題我們先看一下，然後那個那個那個數字再確認。"
        "市場短期波動很正常，投資人應該專注長期現金流與資產配置。"
        "退休規劃需要準備足夠現金水位，並且避免頻繁交易與追高殺低。"
    )
    result = hallucination.check_transcript(text)
    assert not result.suspect


def test_high_specificity_hallucination_phrase_in_middle_is_caught():
    text = (
        "現在我們來看長期投資的正確做法，重點是持續買進並持有。"
        "請不吝點贊訂閱轉發打賞支持明鏡與點點欄目"
        "接下來回答學員關於資產配置與退休提領的問題。"
    )
    result = hallucination.check_transcript(text)
    assert result.suspect
    assert any("isolated_hallucination_phrase" in reason for reason in result.reasons)


def test_synonyms_are_high_confidence_and_within_budget():
    groups = json.loads(
        (Path(__file__).resolve().parent.parent / "config/synonyms.json").read_text(
            encoding="utf-8"
        )
    )
    assert 30 <= len(groups) <= 60
    flat = {item.lower() for group in groups for item in group}
    banned_pairs = [
        ("劳退", "劳保"),
        ("增贷", "转贷"),
        ("短债", "sgov"),
        ("短债", "boxx"),
        ("两倍杠杆", "qld"),
        ("年金", "终身俸"),
        ("质押", "抵押"),
        ("健保", "health insurance"),
    ]
    for left, right in banned_pairs:
        assert not any(left in group and right in group for group in groups), (left, right)
    assert any("crac" in group and "clec" in group for group in groups)
    assert any("债卷" in group and "债券" in group for group in groups)
    assert "勞退" not in flat


def test_blocked_works_are_read_from_source_policy():
    policy = json.loads(
        (Path(__file__).resolve().parent.parent / "config/source_policy.json").read_text(
            encoding="utf-8"
        )
    )
    expected = set(policy["sources"]["quarantine"]["blocked_works"])
    assert set(build_corpus.BLOCKED_WORKS) == expected
    assert set(audit_corpus.load_policy_blocked()) == expected


# ---------------------------------------------------------------------------
# Passage-level exact dedup / audit helpers
# ---------------------------------------------------------------------------

def test_cross_work_exact_body_is_deduplicated_and_ordinals_renumbered():
    body = "完全相同的一段正文內容。" * 10
    passages = [
        _row(doc_id="a0", work_id="w-a", source_id="a", ordinal=0, body=body),
        _row(doc_id="a1", work_id="w-a", source_id="a", ordinal=1, body=body + "甲"),
        _row(doc_id="b0", work_id="w-b", source_id="b", ordinal=0, body=body),
    ]
    kept = build_corpus.deduplicate_cross_work_passages(
        passages, {"a": 70, "b": 50}
    )
    assert len(kept) == 2
    assert {row["work_id"] for row in kept} == {"w-a"}
    assert [row["ordinal"] for row in kept if row["source_id"] == "a"] == [0, 1]
    assert all(schema.validate_passage(row) == [] for row in kept)


def test_audit_helpers_detect_pseudo_years_dates_and_duplicate_ranges():
    rows = [
        _row(doc_id="a", work_id="ep-02016", source_id="s1", start_sec=0, end_sec=10),
        _row(doc_id="b", work_id="ep-02016", source_id="s1", start_sec=0, end_sec=10),
        _row(doc_id="c", work_id="ep-00001", source_id="s2", date="2026-11-06", start_sec=20, end_sec=30),
    ]
    assert audit_corpus.pseudo_year_works(rows) == ["ep-02016"]
    assert len(audit_corpus.capture_date_rows(rows)) == 1
    assert len(audit_corpus.duplicate_time_ranges(rows)) == 1
    contiguous = [
        _row(doc_id="x", work_id="ep-00002", source_id="s3", start_sec=0, end_sec=10),
        _row(doc_id="y", work_id="ep-00002", source_id="s3", start_sec=10, end_sec=20),
    ]
    assert len(audit_corpus.overlapping_time_ranges(contiguous)) == 0


def test_passage_other_kinds_are_derived_from_final_passages():
    passages = [
        _row(doc_id="summary-0", work_id="ep-00123", kind="summary", other_kinds=[]),
        _row(doc_id="summary-1", work_id="ep-00123", kind="summary", other_kinds=[]),
        _row(doc_id="transcript-0", work_id="ep-00123", kind="transcript", other_kinds=[]),
        _row(doc_id="solo-0", work_id="solo-work", kind="doc", other_kinds=[]),
    ]
    build_corpus.assign_passage_other_kinds(passages)
    by_id = {row["doc_id"]: row for row in passages}
    assert by_id["summary-0"]["other_kinds"] == ["transcript"]
    assert by_id["summary-1"]["other_kinds"] == ["transcript"]
    assert by_id["transcript-0"]["other_kinds"] == ["summary"]
    assert by_id["solo-0"]["other_kinds"] == []


def test_audit_detects_other_kinds_mismatches_and_title_residue():
    good = [
        _row(doc_id="s", work_id="ep-00123", kind="summary", other_kinds=["transcript"]),
        _row(doc_id="t", work_id="ep-00123", kind="transcript", other_kinds=["summary"]),
    ]
    assert audit_corpus.other_kinds_mismatches(good) == []

    bad = [
        _row(doc_id="s", work_id="ep-00123", kind="summary", other_kinds=["summary"]),
        _row(doc_id="t", work_id="ep-00123", kind="transcript", other_kinds=["transcript"]),
    ]
    mismatches = audit_corpus.other_kinds_mismatches(bad)
    assert len(mismatches) == 2
    assert {item["expected"][0] for item in mismatches} == {"summary", "transcript"}

    titles = [
        _row(doc_id="a", title="正常標題"),
        _row(
            doc_id="b",
            title="【00123】市場不貴 02/20/2020 CLEC 投資理財教育頻道",
        ),
        _row(
            doc_id="c",
            title="【00123】市場不貴 建議大家 訂閲頻道，才不會漏掉我們的直播通知。",
        ),
    ]
    hits = audit_corpus.title_template_hits(titles)
    assert {hit["doc_id"] for hit in hits} == {"b", "c"}
    assert {tuple(hit["patterns"]) for hit in hits} == {
        ("channel_suffix",),
        ("subscription_cta",),
    }


def test_audit_ignores_semantic_channel_mentions_in_titles():
    rows = [
        _row(
            doc_id="semantic",
            title="CLEC 投資理財頻道的成功分享 2025年2月2日",
        )
    ]
    assert audit_corpus.title_template_hits(rows) == []


# ---------------------------------------------------------------------------
# End-to-end: low-confidence Drive SRT keeps both works
# ---------------------------------------------------------------------------

def _make_collection(root: Path) -> None:
    (root / "web/clectw/text/ep").mkdir(parents=True, exist_ok=True)
    (root / "web/clectw/transcripts").mkdir(parents=True, exist_ok=True)
    (root / "sources/drive/archive-transcripts").mkdir(parents=True, exist_ok=True)


def test_low_confidence_drive_srt_keeps_drive_and_clectw(tmp_path):
    root = tmp_path / "collection"
    _make_collection(root)

    summary = """来源: https://clectw.com/ep/short-00001
采集时间: 2026-11-06 20:11:20 +0800

播完自動接下一集
短篇 00001 2024年12月31日 · 5:40 收藏
【00001】短篇測試標題
📰 本集週報
本週課程概述
""" + "這是摘要內容，討論長期投資與資產配置。" * 10 + """
※ 本週報由 AI 依影片逐字稿整理，僅供參考。
"""
    (root / "web/clectw/text/ep/short-00001.txt").write_text(summary, encoding="utf-8")
    (root / "web/clectw/transcripts/short-00001.txt").write_text(
        "以下是關於指數基金的深入說明與長期投資策略。" * 30, encoding="utf-8"
    )

    (root / "sources/drive/archive-transcripts/00070-03.md").write_text(
        "# 語音筆記：00001 短篇 有字幕 價值一億元的投資講座 2024年12月31日\n",
        encoding="utf-8",
    )
    blocks = []
    for index in range(30):
        start = index * 2
        blocks.append(
            f"{index + 1}\n"
            f"00:{start // 60:02d}:{start % 60:02d},000 --> "
            f"00:{(start + 2) // 60:02d}:{(start + 2) % 60:02d},000\n"
            "今天天氣很好我們去公園散步聊聊生活與旅行規劃以及晚餐吃什麼"
        )
    (root / "sources/drive/archive-transcripts/00070-01.srt").write_text(
        "\n\n".join(blocks), encoding="utf-8"
    )

    out = tmp_path / "dist"
    build_corpus.build(root, out)
    rows = list(schema.iter_corpus(out / "corpus.jsonl"))
    works = {row["work_id"] for row in rows}
    source_ids = {row["source_id"] for row in rows}
    assert "drive-00070" in works
    assert "short-00001" in works
    assert "clectw_tr_short-00001" in source_ids
    assert "drive_srt_00070" in source_ids
    manifest = [
        json.loads(line)
        for line in (out / "doc-manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert not [
        entry for entry in manifest
        if entry["text_chars"] >= 100 and entry["passage_count"] == 0
    ]


def test_newest_book_version_wins_end_to_end(tmp_path):
    root = tmp_path / "collection"
    _make_collection(root)
    local_dir = root / "text/local/public/教學資料/CLEC 寶典(The-Wisdom-of-CLEC)"
    local_dir.mkdir(parents=True)
    (local_dir / "CLEC寶典_ZH_V1.5.1.pdf.txt").write_text(
        "這是舊版內容，討論資產配置。" * 20, encoding="utf-8"
    )
    (root / "sources/drive/text").mkdir(parents=True)
    (root / "sources/drive/text/book.txt").write_text(
        "来源: https://drive.google.com/file/d/abc/view\n"
        "标题: CLEC寶典_ZH_V1.14.2.pdf\n"
        "文件类型: application/pdf\n\n"
        + "這是新版內容，討論資產配置與再平衡。" * 20,
        encoding="utf-8",
    )

    out = tmp_path / "dist"
    build_corpus.build(root, out)
    rows = list(schema.iter_corpus(out / "corpus.jsonl"))
    assert {row["work_id"] for row in rows} == {"book-clec-wisdom-zh"}
    body = "\n".join(row["body"] for row in rows)
    assert "新版內容" in body
    assert "舊版內容" not in body
    stats = json.loads((out / "build-stats.json").read_text(encoding="utf-8"))
    assert stats["book_versions_removed"] == 1
