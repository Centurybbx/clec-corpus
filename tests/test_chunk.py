"""Tests for natural-semantic chunking and timestamp extraction."""

from etl import chunk


def _prose(sentence: str, count: int) -> str:
    return "".join(sentence for _ in range(count))


def test_prose_chunks_respect_sentence_boundaries_and_length():
    sentence = "這是一句用於測試切段的完整句子，長度大約三十個字。"
    text = _prose(sentence, 60)
    passages = chunk.chunk_prose(text)
    assert passages
    assert all(100 <= len(p.body) <= 900 for p in passages)
    assert all(p.body.endswith(("。", "！", "？", "；", "…")) for p in passages)
    assert sum(len(p.body) for p in passages) == len(text)


def test_prose_headings_propagate():
    text = (
        "## 第一節\n" + "第一節的內容。" * 30
        + "\n## 第二節\n" + "第二節的內容。" * 30
    )
    passages = chunk.chunk_prose(text)
    headings = {p.heading for p in passages}
    assert "第一節" in headings
    assert "第二節" in headings
    first = next(p for p in passages if p.heading == "第一節")
    second = next(p for p in passages if p.heading == "第二節")
    assert "第一節的內容" in first.body
    assert "第二節的內容" in second.body


def test_short_fragment_is_dropped():
    assert chunk.chunk_prose("太短。") == []


def test_long_run_on_sentence_is_capped():
    text = "沒有標點的很長句子" * 200
    passages = chunk.chunk_prose(text)
    assert passages
    assert all(len(p.body) <= 900 for p in passages)


def test_srt_timestamps_are_monotonic_and_present():
    blocks = []
    for index in range(80):
        start = index * 3
        blocks.append(
            f"{index + 1}\n"
            f"00:{start // 60:02d}:{start % 60:02d},000 --> "
            f"00:{(start + 2) // 60:02d}:{(start + 2) % 60:02d},500\n"
            f"這是第{index}句字幕內容。"
        )
    cues = chunk.parse_srt("\n\n".join(blocks))
    assert len(cues) == 80
    passages = chunk.chunk_srt(cues)
    assert passages
    for passage in passages:
        assert passage.start_sec is not None
        assert passage.end_sec is not None
        assert passage.start_sec <= passage.end_sec
    for left, right in zip(passages, passages[1:]):
        assert left.end_sec <= right.start_sec


def test_srt_merges_to_target_size():
    text = "這是一句沒有標點的逐字稿內容"
    blocks = []
    for index in range(120):
        start = index * 2
        blocks.append(
            f"{index + 1}\n"
            f"00:{start // 60:02d}:{start % 60:02d},000 --> "
            f"00:{(start + 2) // 60:02d}:{(start + 2) % 60:02d},000\n"
            f"{text}"
        )
    passages = chunk.chunk_srt(chunk.parse_srt("\n\n".join(blocks)))
    assert passages
    assert all(100 <= len(p.body) <= 900 for p in passages)
    assert any(400 <= len(p.body) <= 600 for p in passages)
