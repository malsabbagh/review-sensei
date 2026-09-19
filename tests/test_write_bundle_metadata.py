#!/usr/bin/env python3
"""Tests for npm bundle metadata writing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.write_bundle_metadata import (
    SHA256SUMS_FILENAME,
    BundleMetadataError,
    main,
    normalize_release_version,
    resolve_executed_source_sha,
    write_bundle_metadata,
)


class WriteBundleMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.bundle_dir = Path(self.tempdir.name)
        (self.bundle_dir / SHA256SUMS_FILENAME).write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_write_bundle_metadata_from_version(self) -> None:
        path = write_bundle_metadata(
            self.bundle_dir,
            version="0.5.0",
            source_sha="a" * 40,
        )
        metadata = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            metadata,
            {"version": "0.5.0", "source_sha": "a" * 40},
        )
        sums = (self.bundle_dir / SHA256SUMS_FILENAME).read_text(encoding="utf-8")
        self.assertIn("bundle-metadata.json", sums)

    def test_write_bundle_metadata_from_tag(self) -> None:
        path = write_bundle_metadata(
            self.bundle_dir,
            tag="v0.5.0",
            source_sha="b" * 40,
        )
        metadata = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], "0.5.0")

    def test_normalize_release_version_rejects_prerelease_tag(self) -> None:
        with self.assertRaisesRegex(BundleMetadataError, "must match vX.Y.Z"):
            normalize_release_version(tag="v0.5.0-pre")

    def test_write_bundle_metadata_rejects_invalid_source_sha(self) -> None:
        with self.assertRaisesRegex(BundleMetadataError, "canonical git commit"):
            write_bundle_metadata(
                self.bundle_dir,
                version="0.5.0",
                source_sha="not-a-sha",
            )

    def test_resolve_executed_source_sha_defaults_to_head(self) -> None:
        from unittest import mock

        head_sha = "a" * 40
        with (
            mock.patch(
                "scripts.write_bundle_metadata.resolve_git_head_sha",
                return_value=head_sha,
            ),
            mock.patch.dict("os.environ", {"GITHUB_SHA": head_sha}, clear=False),
        ):
            self.assertEqual(resolve_executed_source_sha(explicit=None), head_sha)

    def test_resolve_executed_source_sha_rejects_mismatch(self) -> None:
        from unittest import mock

        head_sha = "a" * 40
        with (
            mock.patch(
                "scripts.write_bundle_metadata.resolve_git_head_sha",
                return_value=head_sha,
            ),
            mock.patch.dict("os.environ", {"GITHUB_SHA": head_sha}, clear=False),
        ):
            with self.assertRaisesRegex(
                BundleMetadataError,
                "does not match the executed commit",
            ):
                resolve_executed_source_sha(explicit="b" * 40)

    def test_main_rejects_git_head_resolution_failure(self) -> None:
        import io
        from unittest import mock

        with mock.patch(
            "scripts.write_bundle_metadata.resolve_git_head_sha",
            side_effect=BundleMetadataError(
                "unable to resolve checkout HEAD for bundle metadata"
            ),
        ):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                exit_code = main(
                    [
                        "--bundle-dir",
                        str(self.bundle_dir),
                        "--version",
                        "0.5.0",
                    ]
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("unable to resolve checkout HEAD", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
