"""Generate the release manifest for a packed CLEC corpus.

Usage:
    python3 -m etl.publish_manifest \
        --archive dist/corpus.tar.zst \
        --meta dist/meta.json \
        --tag corpus-2026-11-24 \
        --repo Centurybbx/clec-corpus \
        --out dist/manifest.json

The archive is streamed through SHA-256, so multi-gigabyte packs never have to
be loaded into memory.  ``dist/meta.json`` must use ``schema_version`` 2; the
release manifest copies its pack identity and statistics verbatim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 2
DEFAULT_REPO = "Centurybbx/clec-corpus"
CHUNK_SIZE = 1024 * 1024
REQUIRED_META_FIELDS = ("pack_name", "pack_version", "built_at")


class ManifestError(Exception):
    """Raised when a release manifest cannot be generated."""


def sha256_and_size(path: Path) -> tuple[str, int]:
    """Stream *path* and return its lowercase SHA-256 hex digest and size."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def load_meta(path: Path) -> dict:
    """Read and validate a corpus ``meta.json`` document."""
    if not path.is_file():
        raise ManifestError(f"meta file not found: {path}")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ManifestError(f"meta file is not valid UTF-8: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"meta file is not valid JSON: {path}: {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"cannot read meta file: {path}: {exc}") from exc

    if not isinstance(meta, dict):
        raise ManifestError(f"meta file must contain a JSON object: {path}")

    version = meta.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported meta schema_version {version!r} in {path}; "
            f"expected {SCHEMA_VERSION}"
        )

    for field in REQUIRED_META_FIELDS:
        value = meta.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(f"meta file is missing a valid {field!r}: {path}")
    if not isinstance(meta.get("counts"), dict):
        raise ManifestError(f"meta file is missing a valid 'counts' object: {path}")
    if not isinstance(meta.get("known_gaps"), list):
        raise ManifestError(f"meta file is missing a valid 'known_gaps' list: {path}")
    return meta


def default_archive_url(repo: str, tag: str, archive_name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{archive_name}"


def build_manifest(
    *,
    archive: Path,
    meta: dict,
    tag: str,
    repo: str = DEFAULT_REPO,
    mirrors: list[str] | None = None,
    archive_url: str | None = None,
) -> dict:
    """Build the release manifest payload for *archive* and *meta*."""
    if not archive.is_file():
        raise ManifestError(f"archive not found: {archive}")
    if not tag.strip():
        raise ManifestError("release tag must not be empty")
    if not repo.strip():
        raise ManifestError("repository must not be empty")

    sha256, size_bytes = sha256_and_size(archive)
    return {
        "schema_version": SCHEMA_VERSION,
        "pack_name": meta["pack_name"],
        "pack_version": meta["pack_version"],
        "built_at": meta["built_at"],
        "archive_url": archive_url or default_archive_url(repo, tag, archive.name),
        "mirrors": list(mirrors or []),
        "sha256": sha256,
        "size_bytes": size_bytes,
        "counts": meta["counts"],
        "known_gaps": meta["known_gaps"],
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, required=True, help="packed corpus archive")
    parser.add_argument("--meta", type=Path, required=True, help="dist/meta.json")
    parser.add_argument("--tag", required=True, help="GitHub Release tag")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository (owner/name)")
    parser.add_argument(
        "--mirror",
        action="append",
        default=[],
        dest="mirrors",
        metavar="URL",
        help="extra download mirror (repeatable)",
    )
    parser.add_argument("--archive-url", default=None, help="override the archive download URL")
    parser.add_argument("--out", type=Path, required=True, help="output manifest path")
    args = parser.parse_args(argv)

    try:
        meta = load_meta(args.meta)
        manifest = build_manifest(
            archive=args.archive,
            meta=meta,
            tag=args.tag,
            repo=args.repo,
            mirrors=args.mirrors,
            archive_url=args.archive_url,
        )
        write_manifest(args.out, manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "manifest": str(args.out),
                "archive": str(args.archive),
                "sha256": manifest["sha256"],
                "size_bytes": manifest["size_bytes"],
                "archive_url": manifest["archive_url"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
