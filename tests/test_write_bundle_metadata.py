#!/usr/bin/env python3
"""Tests for npm bundle metadata writing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.write_bundle_metadata import (
    BundleMetadataError,
    normalize_release_version,
    write_bundle_metadata,
)


class WriteBundleMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.bundle_dir = Path(self.tempdir.name)

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


if __name__ == "__main__":
    unittest.main()
