"""Origin URL resolution for podcast ASR transcripts and clec-md pages."""

from __future__ import annotations

import json

from etl import build_corpus, schema


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Podcast ASR
# ---------------------------------------------------------------------------

def test_valid_http_url_rejects_feed_filenames():
    assert build_corpus.valid_http_url("ximalaya-110339131.xml") == ""
    assert build_corpus.valid_http_url("") == ""
    assert build_corpus.valid_http_url(None) == ""
    assert build_corpus.valid_http_url("ftp://example.com/feed.xml") == ""
    assert (
        build_corpus.valid_http_url("https://www.ximalaya.com/sound/1023828791")
        == "https://www.ximalaya.com/sound/1023828791"
    )


def test_resolve_podcast_origin_prefers_page_link_over_audio():
    meta = {
        "source_guid": "xmly_track_1023828791",
        # This feed filename used to win because it is truthy but not a URL.
        "source": "ximalaya-110339131.xml",
        "media_url": "https://jt.ximalaya.com/audio.m4a",
    }
    podcast_meta = {
        "xmly_track_1023828791": {
            "link": "https://www.ximalaya.com/sound/1023828791",
            "enclosure": "https://jt.ximalaya.com/enclosure.m4a",
        }
    }
    assert build_corpus.resolve_podcast_origin(meta, podcast_meta) == (
        "https://www.ximalaya.com/sound/1023828791"
    )


def test_resolve_podcast_origin_falls_back_to_media_then_enclosure():
    meta = {
        "source_guid": "g",
        "source": "ximalaya-110339131.xml",
        "media_url": "https://cdn.example/audio.m4a",
    }
    podcast_meta = {
        "g": {"link": "", "enclosure": "https://cdn.example/enclosure.m4a"}
    }
    assert build_corpus.resolve_podcast_origin(meta, podcast_meta) == (
        "https://cdn.example/audio.m4a"
    )

    meta.pop("media_url")
    assert build_corpus.resolve_podcast_origin(meta, podcast_meta) == (
        "https://cdn.example/enclosure.m4a"
    )

    podcast_meta["g"]["enclosure"] = ""
    assert build_corpus.resolve_podcast_origin(meta, podcast_meta) == ""


def test_resolve_podcast_origin_accepts_valid_source_url():
    meta = {
        "source_guid": "g",
        "source": "https://example.com/episodes/1",
        "media_url": "https://cdn.example/audio.m4a",
    }
    assert build_corpus.resolve_podcast_origin(meta, {}) == (
        "https://example.com/episodes/1"
    )


# ---------------------------------------------------------------------------
# clec-md URL structure (Astro github-slugger + trailing slash)
# ---------------------------------------------------------------------------

def test_astro_slug_matches_live_clec_md_routes():
    assert build_corpus.astro_slug("聰明再平衡法") == "聰明再平衡法"
    assert build_corpus.astro_slug("回報率ROA-ROE-ROI") == "回報率roa-roe-roi"
    assert build_corpus.astro_slug("AI是一面鏡子") == "ai是一面鏡子"
    assert build_corpus.astro_slug("2021.03.02 FB所有文章彙總") == (
        "20210302-fb所有文章彙總"
    )
    assert build_corpus.astro_slug(
        "2022.01.16【James 緣起 與 今日 】- 含全部章節"
    ) == "20220116james-緣起-與-今日---含全部章節"


def test_clec_md_knowledge_urls_are_percent_encoded_and_trailing_slash():
    assert build_corpus.clec_md_knowledge_url(
        "zh", "asset-allocation", "聰明再平衡法"
    ) == (
        "https://fraserlai.github.io/clec-md/asset-allocation/"
        "%E8%81%B0%E6%98%8E%E5%86%8D%E5%B9%B3%E8%A1%A1%E6%B3%95/"
    )
    assert build_corpus.clec_md_knowledge_url(
        "en", "asset-allocation", "聰明再平衡法"
    ) == (
        "https://fraserlai.github.io/clec-md/en/asset-allocation/"
        "%E8%81%B0%E6%98%8E%E5%86%8D%E5%B9%B3%E8%A1%A1%E6%B3%95/"
    )
    assert build_corpus.clec_md_knowledge_url("en", "glossary", "QQQI") == (
        "https://fraserlai.github.io/clec-md/en/glossary/qqqi/"
    )


def test_clec_md_post_url_keeps_nested_archive_folders():
    assert build_corpus.clec_md_post_url(
        "X及YouTube的貼文/0039貼文-99的人都把買進方式看得太重要了-日期2026年7月23日.md"
    ) == (
        "https://fraserlai.github.io/clec-md/posts/"
        "x%E5%8F%8Ayoutube%E7%9A%84%E8%B2%BC%E6%96%87/"
        "0039%E8%B2%BC%E6%96%87-99%E7%9A%84%E4%BA%BA%E9%83%BD%E6%8A%8A"
        "%E8%B2%B7%E9%80%B2%E6%96%B9%E5%BC%8F%E7%9C%8B%E5%BE%97%E5%A4%AA"
        "%E9%87%8D%E8%A6%81%E4%BA%86-%E6%97%A5%E6%9C%9F2026%E5%B9%B47"
        "%E6%9C%8823%E6%97%A5/"
    )
    assert build_corpus.clec_md_post_url(
        "FB Wealthyin50的貼文/2021.03.02 FB所有文章彙總/"
        "2019.05.22-你可以不用動.md"
    ) == (
        "https://fraserlai.github.io/clec-md/posts/"
        "fb-wealthyin50%E7%9A%84%E8%B2%BC%E6%96%87/"
        "20210302-fb%E6%89%80%E6%9C%89%E6%96%87%E7%AB%A0%E5%BD%99%E7%B8%BD/"
        "20190522-%E4%BD%A0%E5%8F%AF%E4%BB%A5%E4%B8%8D%E7%94%A8%E5%8B%95/"
    )


# ---------------------------------------------------------------------------
# End-to-end build
# ---------------------------------------------------------------------------

def test_build_fills_podcast_and_clec_md_origin_urls(tmp_path):
    root = tmp_path / "collection"
    (root / "web/clectw/text/ep").mkdir(parents=True)
    (root / "web/clectw/transcripts").mkdir(parents=True)

    _write(
        root / "sources/podcast/feed.xml",
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>test</title>
<item>
  <title>測試單集</title>
  <guid>pod-guid</guid>
  <link>https://www.ximalaya.com/sound/1023828791</link>
  <enclosure url="https://cdn.example/enclosure.m4a" type="audio/mp4"/>
</item>
</channel></rss>
""",
    )
    _write(
        root / "sources/podcast/episode-inventory.jsonl",
        json.dumps(
            {
                "feed": "feed.xml",
                "title": "測試單集",
                "published": "Tue, 13 Oct 2026 08:45:55 GMT",
                "guid": "pod-guid",
                "enclosure_url": "https://cdn.example/enclosure.m4a",
                "possible_matches": [],
                "status": "no_id_match_needs_review",
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    _write(
        root / "transcripts/podcast-asr/gap-1/metadata.json",
        json.dumps(
            {
                "id": "gap-1",
                "source": "ximalaya-110339131.xml",
                "source_guid": "pod-guid",
                "media_url": "https://jt.ximalaya.com/audio.m4a",
            },
            ensure_ascii=False,
        ),
    )
    _write(
        root / "transcripts/podcast-asr/gap-1/gap-1.txt",
        "這是播客逐字稿內容，討論長期投資、資產配置與退休現金流。" * 4,
    )

    knowledge_front = """---
title: '測試問答'
date: 2026-01-02
category: 'qa'
status: 'published'
---

"""
    _write(
        root / "sources/clec-md/knowledge/qa/測試問答.md",
        knowledge_front + "這是知識庫問答正文，討論資產配置與退休現金流。" * 6,
    )
    _write(
        root / "sources/clec-md/knowledge/en/qa/Test-QA.md",
        knowledge_front + "This is the English knowledge body about index funds. " * 6,
    )
    _write(
        root / "sources/clec-md/knowledge/asset-allocation/有來源.md",
        """---
title: '有來源'
date: 2026-01-03
category: 'asset-allocation'
sourceUrl: 'https://example.com/original-article'
status: 'published'
---

"""
        + "這是已標註來源的知識庫正文，討論再平衡與現金流。" * 5,
    )
    _write(
        root / "sources/clec-md/posts/X及YouTube的貼文/0001貼文-測試文章.md",
        """---
title: '0001貼文 測試文章'
date: 2026-07-01
source: X
status: published
---

"""
        + "這是貼文正文，討論長期持有指數基金與資產配置。" * 5,
    )

    out = tmp_path / "dist"
    build_corpus.build(root, out)
    rows = list(schema.iter_corpus(out / "corpus.jsonl"))
    by_source = {row["source_id"]: row for row in rows}

    assert by_source["podcast_asr_gap-1"]["origin_url"] == (
        "https://www.ximalaya.com/sound/1023828791"
    )
    assert by_source["cmd_kb_zh_qa_測試問答"]["origin_url"] == (
        "https://fraserlai.github.io/clec-md/qa/"
        "%E6%B8%AC%E8%A9%A6%E5%95%8F%E7%AD%94/"
    )
    assert by_source["cmd_kb_en_qa_test-qa"]["origin_url"] == (
        "https://fraserlai.github.io/clec-md/en/qa/test-qa/"
    )
    assert by_source["cmd_kb_zh_asset-allocation_有來源"]["origin_url"] == (
        "https://example.com/original-article"
    )
    assert by_source["cmd_post_0001貼文-測試文章"]["origin_url"] == (
        "https://fraserlai.github.io/clec-md/posts/"
        "x%E5%8F%8Ayoutube%E7%9A%84%E8%B2%BC%E6%96%87/"
        "0001%E8%B2%BC%E6%96%87-%E6%B8%AC%E8%A9%A6%E6%96%87%E7%AB%A0/"
    )
