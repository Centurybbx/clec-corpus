"""Tests for the Whisper hallucination gate."""

from etl import hallucination


def test_repeated_phrase_is_suspect():
    result = hallucination.check_transcript("我看你很想念你" * 20)
    assert result.suspect
    assert result.max_repeat_count >= 4
    assert any("repeat=" in reason for reason in result.reasons)


def test_normal_transcript_is_not_suspect():
    text = (
        "今天我們來聊長期投資與資產配置。市場短期波動無法預測，"
        "投資人應該專注於現金流、風險控制與長期複利。"
        "退休規劃需要準備足夠的現金水位，並且避免頻繁交易。"
    )
    result = hallucination.check_transcript(text)
    assert not result.suspect


def test_known_subtitle_credit_is_suspect():
    text = "這是正常的段落內容，討論 QQQ 與納斯達克100的長期投資。" + "字幕由Amara.org社区提供"
    result = hallucination.check_transcript(text)
    assert result.suspect
    assert any("isolated_hallucination_phrase" in reason for reason in result.reasons)


def test_compression_ratio_on_repetition():
    result = hallucination.check_transcript("重複重複重複重複重複" * 80)
    assert result.compression_ratio > 2.4
    assert result.suspect


def test_compression_ratio_function_is_stable():
    assert hallucination.compression_ratio("") == 0.0
    assert hallucination.compression_ratio("正常內容") > 0
