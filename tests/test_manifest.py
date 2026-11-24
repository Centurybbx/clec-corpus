"""Tests for the corpus release manifest generator."""

import hashlib
import json
import re

from etl import publish_manifest

META = {
    "schema_version": 2,
    "pack_name": "clec",
    "pack_version": "2026-11-24",
    "built_at": "2026-11-24T04:47:32Z",
    "counts": {
        "works": 2817,
        "sources": 5359,
        "passages": 50646,
        "by_kind": {"doc": 11698, "transcript": 27955, "qa": 419, "summary": 10574},
    },
    "known_gaps": [
        "ep-00176 / ep-00260 / ep-00295 因严重语音转写幻觉已隔离剔除，请回看原片。",
        "自动逐字稿与网页摘要均未经逐字人工校对，关键数字与观点请回溯原始出处。",
    ],
    "disclaimer": "本知识库为非官方学习检索工具，不构成投资建议。",
}


def _write_inputs(tmp_path, *, payload=b"clec corpus archive\n" * 1000, **meta_overrides):
    archive = tmp_path / "corpus.tar.zst"
    archive.write_bytes(payload)
    meta = dict(META)
    meta.update(meta_overrides)
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return archive, meta_path, payload


def _run(tmp_path, archive, meta_path, *extra):
    out = tmp_path / "manifest.json"
    code = publish_manifest.main(
        [
            "--archive",
            str(archive),
            "--meta",
            str(meta_path),
            "--tag",
            "corpus-2026-11-24",
            "--repo",
            "Centurybbx/clec-corpus",
            "--out",
            str(out),
            *extra,
        ]
    )
    return code, out


def test_manifest_defaults_hash_size_and_release_url(tmp_path):
    archive, meta_path, payload = _write_inputs(tmp_path)

    code, out = _run(tmp_path, archive, meta_path)

    assert code == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest == {
        "schema_version": 2,
        "pack_name": "clec",
        "pack_version": "2026-11-24",
        "built_at": "2026-11-24T04:47:32Z",
        "archive_url": (
            "https://github.com/Centurybbx/clec-corpus/releases/download/"
            "corpus-2026-11-24/corpus.tar.zst"
        ),
        "mirrors": [],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "counts": META["counts"],
        "known_gaps": META["known_gaps"],
    }
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])
    assert list(manifest) == [
        "schema_version",
        "pack_name",
        "pack_version",
        "built_at",
        "archive_url",
        "mirrors",
        "sha256",
        "size_bytes",
        "counts",
        "known_gaps",
    ]


def test_mirrors_and_archive_url_override(tmp_path):
    archive, meta_path, payload = _write_inputs(tmp_path)

    code, out = _run(
        tmp_path,
        archive,
        meta_path,
        "--mirror",
        "https://mirror.example.com/clec/corpus.tar.zst",
        "--mirror",
        "https://backup.example.org/clec-corpus.tar.zst",
        "--archive-url",
        "https://cdn.example.net/corpus.tar.zst",
    )

    assert code == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["archive_url"] == "https://cdn.example.net/corpus.tar.zst"
    assert manifest["mirrors"] == [
        "https://mirror.example.com/clec/corpus.tar.zst",
        "https://backup.example.org/clec-corpus.tar.zst",
    ]
    assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["size_bytes"] == len(payload)


def test_missing_archive_exits_nonzero(tmp_path, capsys):
    archive, meta_path, _payload = _write_inputs(tmp_path)
    archive.unlink()

    code, out = _run(tmp_path, archive, meta_path)

    assert code == 1
    assert not out.exists()
    stderr = capsys.readouterr().err
    assert "archive not found" in stderr
    assert str(archive) in stderr


def test_missing_meta_exits_nonzero(tmp_path, capsys):
    archive, meta_path, _payload = _write_inputs(tmp_path)
    meta_path.unlink()

    code, out = _run(tmp_path, archive, meta_path)

    assert code == 1
    assert not out.exists()
    assert "meta file not found" in capsys.readouterr().err


def test_unsupported_schema_version_exits_nonzero(tmp_path, capsys):
    archive, meta_path, _payload = _write_inputs(tmp_path, schema_version=1)

    code, out = _run(tmp_path, archive, meta_path)

    assert code == 1
    assert not out.exists()
    stderr = capsys.readouterr().err
    assert "schema_version" in stderr
    assert "expected 2" in stderr


def test_invalid_meta_json_exits_nonzero(tmp_path, capsys):
    archive, meta_path, _payload = _write_inputs(tmp_path)
    meta_path.write_text("{not json", encoding="utf-8")

    code, out = _run(tmp_path, archive, meta_path)

    assert code == 1
    assert not out.exists()
    assert "not valid JSON" in capsys.readouterr().err
