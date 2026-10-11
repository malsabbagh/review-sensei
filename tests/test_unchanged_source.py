"""Closed unchanged-source citations. No network and no diff inference."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from review_sensei.errors import ReviewInputError
from review_sensei.schemas import validate_public_document
from review_sensei.unchanged_source import (
    KIND,
    UnchangedSourceCitation,
    bind_snapshot,
    git_blob_sha,
    parse_unchanged_source,
    snapshot_path_key,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/schemas/golden/unchanged-source-citation.json"
NEGATIVE = ROOT / "tests/fixtures/schemas/negative/unchanged-source-citation.json"

_BODY = b"def answer():\n    return 1\n"
_BASE = "a" * 40
_HEAD = "b" * 40


def _content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _document(
    body: bytes = _BODY,
    *,
    path: str = "src/app.py",
    mode: str = "100644",
    cited: bytes | None = None,
    range_start: int = 1,
    range_end: int | None = None,
) -> dict[str, object]:
    cited_bytes = body if cited is None else cited
    if range_end is None:
        range_end = body.count(b"\n") or 1
    blob = git_blob_sha(body)
    return {
        "kind": KIND,
        "repository": "acme/widgets",
        "pull_request": 253,
        "base_sha": _BASE,
        "head_sha": _HEAD,
        "path": path,
        "mode": mode,
        "base_blob_sha": blob,
        "head_blob_sha": blob,
        "range_start": range_start,
        "range_end": range_end,
        "content_sha256": _content_sha256(cited_bytes),
        "content_bytes": len(cited_bytes),
    }


def _snapshot(record: UnchangedSourceCitation, body: bytes) -> dict[str, bytes]:
    return {
        record.base_blob_sha: body,
        record.head_blob_sha: body,
        snapshot_path_key(record.base_sha, record.path): body,
        snapshot_path_key(record.head_sha, record.path): body,
    }


class UnchangedSourceTests(unittest.TestCase):
    def test_matching_regular_file_succeeds(self) -> None:
        document = _document()
        self.assertEqual(json.loads(GOLDEN.read_text(encoding="utf-8")), document)
        validate_public_document(document, "unchanged-source-citation")
        record = parse_unchanged_source(document)
        self.assertEqual(bind_snapshot(record, _snapshot(record, _BODY)), _BODY)
        executable = parse_unchanged_source(_document(mode="100755"))
        self.assertEqual(
            bind_snapshot(executable, _snapshot(executable, _BODY)), _BODY
        )

    def test_exact_range_matches_cited_bytes(self) -> None:
        body = b"alpha\nbeta\ngamma\n"
        cited = b"beta\n"
        record = parse_unchanged_source(
            _document(body, cited=cited, range_start=2, range_end=2)
        )
        self.assertEqual(bind_snapshot(record, _snapshot(record, body)), cited)

    def test_changed_blob_refuses(self) -> None:
        other = b"def answer():\n    return 2\n"
        document = _document()
        document["head_blob_sha"] = git_blob_sha(other)
        with self.assertRaises(ReviewInputError) as caught:
            parse_unchanged_source(document)
        self.assertIn("changed", str(caught.exception))

        record = parse_unchanged_source(document | {"head_blob_sha": document["base_blob_sha"]})
        blobs = _snapshot(record, _BODY)
        blobs[snapshot_path_key(record.head_sha, record.path)] = other
        with self.assertRaises(ReviewInputError) as caught:
            bind_snapshot(record, blobs)
        self.assertIn("differ", str(caught.exception))

    def test_missing_blob_refuses(self) -> None:
        record = parse_unchanged_source(_document())
        with self.assertRaises(ReviewInputError) as caught:
            bind_snapshot(record, {})
        self.assertIn("missing", str(caught.exception))
        path_only = {
            snapshot_path_key(record.base_sha, record.path): _BODY,
            snapshot_path_key(record.head_sha, record.path): _BODY,
        }
        with self.assertRaises(ReviewInputError) as caught:
            bind_snapshot(record, path_only)
        self.assertIn("missing", str(caught.exception))

    def test_truncated_range_refuses(self) -> None:
        short = _document()
        short["range_end"] = 10
        short["content_bytes"] = 5
        with self.assertRaises(ReviewInputError) as caught:
            parse_unchanged_source(short)
        self.assertIn("truncated", str(caught.exception))

        past_end = _document()
        past_end["range_end"] = 4
        record = parse_unchanged_source(past_end)
        with self.assertRaises(ReviewInputError) as caught:
            bind_snapshot(record, _snapshot(record, _BODY))
        self.assertIn("truncated", str(caught.exception))

    def test_symlink_and_submodule_modes_refuse(self) -> None:
        for mode in ("120000", "160000"):
            with self.subTest(mode=mode):
                document = _document()
                document["mode"] = mode
                with self.assertRaises(ReviewInputError) as caught:
                    parse_unchanged_source(document)
                self.assertIn("mode", str(caught.exception))
        negative = json.loads(NEGATIVE.read_text(encoding="utf-8"))
        with self.assertRaises(ReviewInputError):
            validate_public_document(negative, "unchanged-source-citation")
        with self.assertRaises(ReviewInputError):
            parse_unchanged_source(negative)

    def test_secret_paths_refuse(self) -> None:
        for path in (".env", "config/.env.local", "keys/id_rsa", "certs/server.pem", "id_ed25519"):
            with self.subTest(path=path):
                with self.assertRaises(ReviewInputError) as caught:
                    parse_unchanged_source(_document(path=path))
                self.assertIn("secret", str(caught.exception))

    def test_excluded_paths_refuse(self) -> None:
        for path in (
            "node_modules/left-pad/index.js",
            "vendor/lib.js",
            "pkg/node_modules/index.js",
            "dist/app.js",
            "generated/out.py",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ReviewInputError) as caught:
                    parse_unchanged_source(_document(path=path))
                self.assertIn("excluded", str(caught.exception))

    def test_foreign_sha_refuses(self) -> None:
        record = parse_unchanged_source(_document())
        foreign = b"not-the-cited-blob\n"
        blobs = _snapshot(record, foreign)
        blobs[record.base_blob_sha] = foreign
        with self.assertRaises(ReviewInputError) as caught:
            bind_snapshot(record, blobs)
        self.assertIn("foreign", str(caught.exception))

        mismatched = _document()
        mismatched["content_sha256"] = _content_sha256(b"other")
        parsed = parse_unchanged_source(mismatched)
        with self.assertRaises(ReviewInputError) as caught:
            bind_snapshot(parsed, _snapshot(parsed, _BODY))
        self.assertIn("does not match", str(caught.exception))

    def test_same_body_from_a_different_path_refuses(self) -> None:
        original = parse_unchanged_source(_document())
        blobs = _snapshot(original, _BODY)
        other = parse_unchanged_source(_document(path="src/other.py"))
        self.assertEqual(original.base_blob_sha, other.base_blob_sha)
        self.assertIn(original.base_blob_sha, blobs)
        with self.assertRaises(ReviewInputError) as caught:
            bind_snapshot(other, blobs)
        self.assertIn("path", str(caught.exception))
        self.assertNotIn("missing", str(caught.exception))

    def test_escaped_paths_refuse(self) -> None:
        for path in ("../secrets.py", "/etc/passwd", "src\\app.py", "https://example.com/a.py"):
            with self.subTest(path=path):
                with self.assertRaises(ReviewInputError):
                    parse_unchanged_source(_document(path=path))

    def test_extra_keys_floats_and_bools_refuse(self) -> None:
        extra = _document()
        extra["patch"] = ""
        with self.assertRaises(ReviewInputError):
            parse_unchanged_source(extra)
        with self.assertRaises(ReviewInputError):
            validate_public_document(extra, "unchanged-source-citation")
        for field, value in (
            ("pull_request", 1.0),
            ("pull_request", True),
            ("content_bytes", False),
            ("range_start", 1.5),
            ("mode", 100644),
        ):
            with self.subTest(field=field, value=value):
                document = _document()
                document[field] = value
                with self.assertRaises(ReviewInputError):
                    parse_unchanged_source(document)

    def test_accessors_are_not_read(self) -> None:
        class FetchingRecord(dict[str, object]):
            def __getitem__(self, key: str) -> object:
                raise AssertionError("record accessor was read")

        class FetchingBlobs(dict[str, bytes]):
            def __getitem__(self, key: str) -> bytes:
                raise AssertionError("snapshot accessor was read")

        with self.assertRaises(ReviewInputError):
            parse_unchanged_source(FetchingRecord(_document()))
        record = parse_unchanged_source(_document())
        with self.assertRaises(ReviewInputError):
            bind_snapshot(record, FetchingBlobs(_snapshot(record, _BODY)))

    def test_absent_diff_is_not_evidence(self) -> None:
        with self.assertRaises(ReviewInputError):
            parse_unchanged_source({"path": "src/app.py", "patch": ""})
        self.assertNotEqual(KIND, "diff-hunk")


if __name__ == "__main__":
    unittest.main()
