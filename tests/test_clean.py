"""Tests for web-template stripping, titles and dates."""

from etl import clean


CAPTURE = """来源: https://clectw.com/ep/ep-00001
采集时间: 2026-11-06 20:11:20 +0800

播完自動接下一集
長篇 00001 2017年12月28日 · 6:27 收藏
【00001】投資通訊 (1)2017 十二月
本集講義／教材（可下載）
📕 00001 專題 pdf
📰 本集週報
本週課程概述
這是摘要正文，應該保留。
本週課程重點
1. 重點一
※ 本週報由 AI 依影片逐字稿整理，僅供參考，實際內容以原影片為準。
這是逐字稿內文，不應進入 summary。
※ 逐字稿由影片語音自動轉錄，可能有錯字，僅供參考。
▶ 相關集數
1:12
長篇 00002 2017/12/29
下一集標題
"""


def test_capture_header_removed():
    result = clean.clean_capture_header(CAPTURE)
    assert result.text.startswith("播完自動接下一集")
    assert "web_capture_header" in result.rules
    assert "来源:" not in result.text


def test_clectw_summary_splits_embedded_transcript():
    page = clean.clean_clectw_summary(CAPTURE)
    assert page.title == "【00001】投資通訊 (1)2017 十二月"
    assert page.date == "2017-12-28"
    assert "這是摘要正文" in page.summary
    assert "這是逐字稿內文" not in page.summary
    assert "播放" not in page.summary
    assert "来源:" not in page.summary
    assert "▶ 相關集數" not in page.summary
    assert "本集講義／教材" not in page.summary
    assert "episode_resource_links" in page.rules
    assert "clectw_episode_footer" in page.rules


def test_blog_drops_excerpt_and_related_footer():
    raw = """来源: https://clec168.com/blog/fb-posts/fb-post-0001
采集时间: 2026-11-06 20:10:50 +0800

2016/05/20
這是截斷的摘要

來源： https://www.facebook.com/123/posts/456/
這是完整正文第一段。
這是完整正文第二段。
撰文日期: May 20, 2016
全部文章
作者
CLEC 投資理財
分類
FB 貼文
更多文章
相關文章標題
郵件清單
加入我們的社區
訂閱郵件清單，及時獲取最新消息和更新
電子郵件
"""
    result = clean.clean_clec168_blog(raw)
    assert "這是完整正文第一段" in result.text
    assert "這是截斷的摘要" not in result.text
    assert "全部文章" not in result.text
    assert "郵件清單" not in result.text
    assert "來源：" not in result.text
    assert "blog_excerpt_before_source" in result.rules


def test_drive_header_variant_and_source_citation():
    raw = """来源: https://drive.google.com/file/d/abc/view
标题: 00572【投資人的財富獨立宣言】日期：2026年7月4日簡報資料.pdf
提取方法: 原始 DOCX XML

來源：https://www.facebook.com/123/posts/456/
正文開始。
"""
    result = clean.clean_drive_document(raw)
    assert result.meta["title"].startswith("00572")
    assert "标题:" not in result.text
    assert "提取方法:" not in result.text
    assert "來源：" not in result.text
    assert result.text.startswith("正文開始")


def test_normalize_title_strips_channel_suffix_and_subscription_cta():
    title = (
        "【00123】投資理財 市場不貴！回報低長期風險最高。"
        "長期投資指數基金，風險低，就富有。 "
        "建議大家 訂閲頻道，才不會漏掉我們的直播通知。訂閲👍轉發！ "
        "02/20/2020 CLEC 投資理財教育頻道"
    )
    assert clean.normalize_title(title) == (
        "【00123】投資理財 市場不貴！回報低長期風險最高。"
        "長期投資指數基金，風險低，就富有。 02/20/2020"
    )

    cta = (
        "【00167】CLEC 讓更多錢為你工作、Tesla Q3銷售新高、買賣原則、"
        "投資永遠不嫌晚 （請訂閱、分享、按 👍，開啟鈴鐺 🔔！感謝🙏）"
        "2020年10月9日 CLEC 投資頻道"
    )
    assert clean.normalize_title(cta) == (
        "【00167】CLEC 讓更多錢為你工作、Tesla Q3銷售新高、買賣原則、"
        "投資永遠不嫌晚 2020年10月9日"
    )

    quoted = (
        "00469(一） 別人的投資是別人的事 2024年6月1日 "
        "CLEC投資理財教育頻道‘"
    )
    assert clean.normalize_title(quoted) == (
        "00469(一） 別人的投資是別人的事 2024年6月1日"
    )


def test_normalize_title_keeps_semantic_channel_mention():
    title = (
        "多語音短篇從實踐中學習：CLEC 投資理財頻道指導下的投資經驗和收穫的"
        "真實案例分享 2025年2月13日 CLEC 投資理財頻道"
    )
    assert clean.normalize_title(title) == (
        "多語音短篇從實踐中學習：CLEC 投資理財頻道指導下的投資經驗和收穫的"
        "真實案例分享 2025年2月13日"
    )
    semantic = "短篇 一個可以穿越時空的投資頻道！ 2023年11月10日 CLEC投資理財頻道"
    assert clean.normalize_title(semantic) == (
        "短篇 一個可以穿越時空的投資頻道！ 2023年11月10日"
    )


def test_normalize_title_keeps_a_valid_title_when_stripping_would_empty_it():
    assert clean.normalize_title("CLEC 投資理財頻道") == "CLEC 投資理財頻道"
    assert clean.normalize_title(
        "CLEC 投資理財頻道重要資訊連結與最新公告"
    ) == "CLEC 投資理財頻道重要資訊連結與最新公告"
    assert clean.normalize_title("normal title") == "normal title"


def test_cfnavi_removes_capture_label_social_card_and_nav():
    raw = """来源: https://cfnavi.org/cat-clec/clec2026/cl-568/

[cl-568]
能改變自己的人是神
00568【能改變自己的人是神】日期：2026年6月6日 pic.twitter.com/abc — CLEC
課程重點
【1】市價買進
正文一。
正文二。
標籤
# 投資
搜尋
最新文章
[cl-583]
另一篇
"""
    result = clean.clean_cfnavi(raw)
    assert result.meta["title"] == "能改變自己的人是神"
    assert result.meta["label"] == "cl-568"
    assert "来源:" not in result.text
    assert "pic.twitter.com" not in result.text
    assert "最新文章" not in result.text
    assert "正文一" in result.text


def test_normalize_title_strips_site_suffix_and_audio_suffix():
    title = "【00001】投資通訊 (1)2017 十二月 — CLEC台灣 (128kbit_AAC)"
    assert clean.normalize_title(title) == "【00001】投資通訊 (1)2017 十二月"
    assert clean.normalize_title("標題 | CLEC投資理財") == "標題"
    assert clean.normalize_title("", fallback="fallback") == "fallback"


def test_strict_date_validation():
    assert clean.normalize_date("2024-6-5") == "2024-06-05"
    assert clean.normalize_date("2024年6月5日") == "2024-06-05"
    assert clean.normalize_date("May 20, 2016") == "2016-05-20"
    assert clean.normalize_date("2024-02-30") is None
    assert clean.normalize_date("") is None
    assert clean.normalize_date(None) is None
    assert clean.normalize_date("0000-00-00") is None


def test_markdown_cleaning_keeps_text_not_navigation():
    raw = """---
title: test
---
## 標題
請看[這篇文章](https://example.com/a?fbclid=1)。
![圖片](https://example.com/x.png)
[[維基連結]]
"""
    result = clean.clean_markdown(raw)
    assert "標題" in result.text
    assert "這篇文章" in result.text
    assert "https://example.com" not in result.text
    assert "維基連結" in result.text
