"""Tests for the version-2 corpus contract and end-to-end assembly."""

import json
from pathlib import Path

from etl import build_corpus, schema


def _valid_row(**overrides):
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


def test_valid_passage_has_no_errors():
    assert schema.validate_passage(_valid_row()) == []


def test_invalid_dates_and_empty_fields_are_rejected():
    assert "date_invalid" in schema.validate_passage(_valid_row(date="2024-02-30"))
    assert "date_invalid" in schema.validate_passage(_valid_row(date=""))
    assert "date_invalid" in schema.validate_passage(_valid_row(date="2024-6-5"))
    assert "body_missing_or_empty" in schema.validate_passage(_valid_row(body=""))
    assert "kind_invalid" in schema.validate_passage(_valid_row(kind="other"))
    assert "quality_invalid" in schema.validate_passage(_valid_row(quality=5))


def test_audit_detects_duplicate_doc_ids_and_ordinal_gaps():
    rows = [
        _valid_row(doc_id="same", source_id="s1", ordinal=0),
        _valid_row(doc_id="same", source_id="s1", ordinal=1),
        _valid_row(doc_id="other", source_id="s2", ordinal=2),
    ]
    metrics = schema.audit_rows(rows)
    assert metrics["duplicate_doc_ids"] == ["same"]
    assert metrics["ordinal_error_sources"] == ["s2"]
    assert metrics["errors"]


def test_length_ratio_metric():
    rows = [_valid_row(doc_id=f"p{i}", source_id=f"s{i}", ordinal=0, body="字" * 500) for i in range(20)]
    rows.append(_valid_row(doc_id="short", source_id="short", ordinal=0, body="短" * 50))
    metrics = schema.audit_rows(rows)
    assert metrics["length_ok"] == 20
    assert 0.95 < metrics["length_ok_ratio"] < 1.0


def test_build_passages_assigns_contiguous_ordinals_and_contract_fields():
    doc = {
        "work_id": "ep-00001",
        "source_id": "clectw_tr_ep-00001",
        "title": "測試標題",
        "kind": "transcript",
        "quality": 2,
        "date": "2024-06-15",
        "text": "這是一句正常逐字稿內容。" * 80,
        "heading": "",
        "start_sec": None,
        "end_sec": None,
        "cues": None,
        "origin_url": "https://clectw.com/ep/ep-00001",
        "other_kinds": ["summary"],
        "license_mode": "full",
    }
    passages, suspects = build_corpus.build_passages([doc])
    assert passages
    assert [row["ordinal"] for row in passages] == list(range(len(passages)))
    assert all(schema.validate_passage(row) == [] for row in passages)
    assert all(row["license_mode"] == "full" for row in passages)
    assert all(item["doc_id"].startswith("p_") for item in suspects)


def test_end_to_end_synthetic_build(tmp_path):
    root = tmp_path / "collection"
    (root / "web/clectw/text/ep").mkdir(parents=True)
    (root / "web/clectw/transcripts").mkdir(parents=True)
    (root / "sources/drive/text").mkdir(parents=True)
    (root / "sources/drive/archive-transcripts").mkdir(parents=True)

    summary = """来源: https://clectw.com/ep/ep-00001
采集时间: 2026-11-06 20:11:20 +0800

播完自動接下一集
長篇 00001 2024年6月15日 · 6:27 收藏
【00001】測試標題
📰 本集週報
本週課程概述
這是摘要內容，討論長期投資與資產配置。""" + "摘要正文。" * 40 + """
※ 本週報由 AI 依影片逐字稿整理，僅供參考，實際內容以原影片為準。
逐字稿不應進入摘要。
※ 逐字稿由影片語音自動轉錄，可能有錯字，僅供參考。
▶ 相關集數
"""
    (root / "web/clectw/text/ep/ep-00001.txt").write_text(summary, encoding="utf-8")
    (root / "web/clectw/transcripts/ep-00001.txt").write_text(
        "這是逐字稿內容，討論長期投資。" * 40, encoding="utf-8"
    )

    drive = """来源: https://drive.google.com/file/d/abc/view
标题: 00572【投資人的財富獨立宣言】日期：2024年6月15日簡報資料.pdf
文件类型: application/pdf

這是 Drive 正文，討論資產配置與退休規劃。""" + "正文內容。" * 40
    (root / "sources/drive/text/doc.txt").write_text(drive, encoding="utf-8")

    blocks = []
    for index in range(60):
        start = index * 3
        blocks.append(
            f"{index + 1}\n"
            f"00:{start // 60:02d}:{start % 60:02d},000 --> "
            f"00:{(start + 2) // 60:02d}:{(start + 2) % 60:02d},000\n"
            "這是 SRT 逐字稿內容，討論投資。"
        )
    srt = "\n\n".join(blocks)
    (root / "sources/drive/archive-transcripts/00001-01.srt").write_text(srt, encoding="utf-8")
    (root / "sources/drive/archive-transcripts/00001-02.txt").write_text("重複 TXT 不應入庫", encoding="utf-8")
    (root / "sources/drive/archive-transcripts/00001-03.md").write_text("# 重複 MD 不應入庫", encoding="utf-8")

    out = tmp_path / "dist"
    build_corpus.build(root, out)
    rows = list(schema.iter_corpus(out / "corpus.jsonl"))
    assert rows
    metrics = schema.audit_rows(rows)
    assert not metrics["errors"]
    assert metrics["length_ok_ratio"] >= 0.95
    source_ids = {row["source_id"] for row in rows}
    assert not any(source_id.startswith(("drive_txt_", "drive_md_")) for source_id in source_ids)
    kinds = {row["kind"] for row in rows if row["work_id"] == "ep-00001"}
    assert "transcript" in kinds and "summary" in kinds
