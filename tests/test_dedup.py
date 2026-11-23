"""Tests for same-kind deduplication and cross-kind alignment."""

from etl import dedup


def _doc(source_id, text, *, kind="doc", work_id=None, priority=50, source_type="drive_text"):
    return {
        "source_id": source_id,
        "work_id": work_id or source_id,
        "kind": kind,
        "text": text,
        "priority": priority,
        "source_type": source_type,
        "title": source_id,
    }


def test_exact_duplicate_same_kind_is_merged():
    text = "這是一段用於去重測試的完整文章內容。" * 20
    docs = [_doc("a", text, priority=10), _doc("b", text, priority=20)]
    result = dedup.deduplicate_documents(docs)
    assert len(result.kept) == 1
    assert result.kept[0]["source_id"] == "b"
    assert result.removed[0]["duplicate_reason"] in {"same_work_kind", "exact_text", "near_duplicate_5gram"}


def test_near_duplicate_80_percent_containment_is_merged():
    base = "金融投資需要長期持有並且避免頻繁交易。" * 30
    near = base + "額外補充的一小段內容。"
    result = dedup.deduplicate_documents(
        [
            _doc("long", near, priority=20),
            _doc("short", base, priority=10),
        ]
    )
    assert len(result.kept) == 1
    assert result.kept[0]["source_id"] == "long"
    assert result.removed[0]["duplicate_reason"] == "near_duplicate_5gram"
    assert result.removed[0]["duplicate_containment"] >= 0.8


def test_short_excerpt_inside_long_article_is_not_swallowed():
    long_text = "完整文章內容。" * 500
    excerpt = "完整文章內容。" * 40
    result = dedup.deduplicate_documents(
        [_doc("long", long_text), _doc("excerpt", excerpt)]
    )
    assert len(result.kept) == 2


def test_different_kinds_are_never_merged():
    text = "同一段內容。" * 100
    result = dedup.deduplicate_documents(
        [
            _doc("tr", text, kind="transcript"),
            _doc("sum", text, kind="summary"),
        ]
    )
    assert len(result.kept) == 2


def test_drive_srt_beats_clectw_transcript_for_same_work():
    text = "同一集的逐字稿內容。" * 100
    result = dedup.deduplicate_documents(
        [
            _doc("clectw_tr_ep-00001", text, kind="transcript", work_id="ep-00001", priority=55, source_type="clectw_transcripts"),
            _doc("drive_srt_00001", text, kind="transcript", work_id="ep-00001", priority=100, source_type="drive_transcripts"),
        ]
    )
    assert len(result.kept) == 1
    assert result.kept[0]["source_id"] == "drive_srt_00001"


def test_annotation_writes_other_kinds():
    docs = [
        {"work_id": "w1", "kind": "transcript"},
        {"work_id": "w1", "kind": "summary"},
        {"work_id": "w1", "kind": "doc"},
        {"work_id": "w2", "kind": "doc"},
    ]
    annotated = dedup.annotate_other_kinds(docs)
    by_kind = {doc["kind"]: doc for doc in annotated if doc["work_id"] == "w1"}
    assert by_kind["transcript"]["other_kinds"] == ["doc", "summary"]
    assert by_kind["summary"]["other_kinds"] == ["doc", "transcript"]
    assert by_kind["doc"]["other_kinds"] == ["transcript", "summary"]
    assert next(doc for doc in annotated if doc["work_id"] == "w2")["other_kinds"] == []


def test_complement_counts():
    docs = [
        {"work_id": "w1", "kind": "transcript"},
        {"work_id": "w1", "kind": "summary"},
        {"work_id": "w2", "kind": "doc"},
    ]
    counts = dedup.complement_counts(docs)
    assert counts["works_with_transcript_and_summary"] == 1
    assert counts["works"] == 2
