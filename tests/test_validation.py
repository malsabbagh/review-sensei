import tempfile
import unicodedata
import unittest
from pathlib import Path

from review_sensei.errors import ReviewInputError
from review_sensei.validation import (
    DEFAULT_REVIEW_LIMITS,
    ReviewLimits,
    decode_git_c_quoted_path,
    read_bounded_utf8,
    validate_repository_path,
)


class ValidationTests(unittest.TestCase):
    def test_limits_are_frozen_and_downward_only(self):
        profile = ReviewLimits(max_diff_bytes=128, max_comments=1)
        self.assertEqual(profile.max_diff_bytes, 128)
        self.assertEqual(profile.max_comments, 1)
        with self.assertRaises(ReviewInputError):
            ReviewLimits(max_diff_bytes=DEFAULT_REVIEW_LIMITS.max_diff_bytes + 1)
        with self.assertRaises(ReviewInputError):
            ReviewLimits(max_diff_bytes=True)
        with self.assertRaises((AttributeError, TypeError)):
            profile.max_comments = 2

    def test_canonical_paths_reject_ambiguous_forms(self):
        invalid = (
            "",
            ".",
            "..",
            "docs/./guide.md",
            "docs/../private.md",
            "docs//guide.md",
            "docs/guide.md/",
            "/etc/passwd",
            "C:/repo/file.py",
            "C:repo/file.py",
            "\\\\server\\share\\file.py",
            "docs\\guide.md",
            " docs/guide.md",
            "docs/guide.md ",
            "docs/\x00guide.md",
            "docs/\u200bguide.md",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                validate_repository_path(value)
        self.assertEqual(
            validate_repository_path("docs:guide/file.md"), "docs:guide/file.md"
        )

    def test_paths_must_be_nfc_and_segments_have_byte_bounds(self):
        decomposed = "cafe\u0301.md"
        self.assertNotEqual(decomposed, unicodedata.normalize("NFC", decomposed))
        with self.assertRaises(ReviewInputError):
            validate_repository_path(decomposed)
        self.assertEqual(validate_repository_path("café.md"), "café.md")
        with self.assertRaises(ReviewInputError):
            validate_repository_path("x" * 256)
        with self.assertRaises(ReviewInputError):
            validate_repository_path("x" * 4_097)

    def test_pattern_mode_allows_globs_but_not_traversal(self):
        self.assertEqual(
            validate_repository_path("docs/**/*.md", pattern=True), "docs/**/*.md"
        )
        self.assertEqual(validate_repository_path("*", pattern=True), "*")
        with self.assertRaises(ReviewInputError):
            validate_repository_path("docs/../*.md", pattern=True)

    def test_non_pattern_paths_can_allow_literal_glob_characters(self):
        self.assertEqual(
            validate_repository_path(
                "src/foo[1].py",
                allow_glob_chars=True,
            ),
            "src/foo[1].py",
        )
        self.assertEqual(
            validate_repository_path(
                "docs/what*.md",
                allow_glob_chars=True,
            ),
            "docs/what*.md",
        )
        with self.assertRaises(ReviewInputError):
            validate_repository_path(
                "docs/../*.md",
                allow_glob_chars=True,
            )
        with self.assertRaises(ReviewInputError):
            validate_repository_path("src/foo[1].py")

    def test_git_c_quoted_paths_decode_bytes_strictly(self):
        self.assertEqual(
            decode_git_c_quoted_path('"docs/caf\\303\\251.md"'),
            "docs/café.md",
        )
        self.assertEqual(
            decode_git_c_quoted_path('"docs/a\\"b.md"'),
            'docs/a"b.md',
        )
        for value in (
            '"docs/bad\\12.md"',
            '"docs/bad\\x41.md"',
            '"docs/bad\\400.md"',
            '"docs/bad\\303.md"',
            '"docs/bad\\377.md',
        ):
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                decode_git_c_quoted_path(value)

    def test_bounded_utf8_file_read_does_not_decode_or_buffer_over_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "diff.patch"
            path.write_bytes("é".encode("utf-8"))
            self.assertEqual(read_bounded_utf8(path, maximum=3), "é")
            with self.assertRaises(ReviewInputError):
                read_bounded_utf8(path, maximum=1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
