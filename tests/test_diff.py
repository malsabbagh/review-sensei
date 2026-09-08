import unittest

from review_sensei.diff import analyze_diff, parse_changed_lines, parse_changed_paths
from review_sensei.errors import ReviewInputError
from review_sensei.validation import ReviewLimits


class DiffParserTests(unittest.TestCase):
    def test_returns_added_lines_by_repository_relative_path(self):
        diff = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,4 @@
 one
+two
 three
+four
"""

        self.assertEqual(
            parse_changed_lines(diff),
            {"src/app.py": frozenset({2, 4})},
        )

    def test_ignores_deletions_when_finding_new_file_lines(self):
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
-removed
 kept
+added
"""

        self.assertEqual(
            parse_changed_lines(diff),
            {"src/app.py": frozenset({2})},
        )

    def test_changed_paths_include_deletions_and_both_sides_of_renames(self):
        diff = """diff --git a/src/legacy.py b/src/legacy.py
deleted file mode 100644
--- a/src/legacy.py
+++ /dev/null
@@ -1 +0,0 @@
-legacy = True
diff --git a/docs/old.md b/guides/new.md
similarity index 100%
rename from docs/old.md
rename to guides/new.md
"""

        self.assertEqual(
            parse_changed_paths(diff),
            ("src/legacy.py", "docs/old.md", "guides/new.md"),
        )

    def test_changed_paths_support_plain_unified_diffs(self):
        deletion = """--- a/src/legacy.py
+++ /dev/null
@@ -1 +0,0 @@
-legacy = True
"""
        addition = """--- /dev/null
+++ b/src/new.py
@@ -0,0 +1 @@
+new = True
"""

        self.assertEqual(parse_changed_paths(deletion), ("src/legacy.py",))
        self.assertEqual(parse_changed_paths(addition), ("src/new.py",))

    def test_analysis_enforces_byte_line_file_and_hunk_limits(self):
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+new
"""
        for limits in (ReviewLimits(max_diff_bytes=4), ReviewLimits(max_diff_lines=2)):
            with self.subTest(limits=limits), self.assertRaises(ReviewInputError):
                analyze_diff(diff, limits=limits)
        two_files = diff + "\ndiff --git a/other.py b/other.py\n"
        with self.assertRaises(ReviewInputError):
            analyze_diff(two_files, limits=ReviewLimits(max_diff_files=1))
        two_hunks = diff.replace("+new\n", "+new\n@@ -4 +5 @@\n+later\n")
        with self.assertRaises(ReviewInputError):
            analyze_diff(two_hunks, limits=ReviewLimits(max_diff_hunks=1))

    def test_git_c_quoted_unicode_path_and_binary_header_are_supported(self):
        diff = (
            'diff --git "a/docs/caf\\303\\251.md" "b/docs/caf\\303\\251.md"\n'
            "new file mode 100644\n"
            "--- /dev/null\n"
            '+++ "b/docs/caf\\303\\251.md"\n'
            "@@ -0,0 +1 @@\n"
            "+ok\n"
        )
        self.assertEqual(parse_changed_paths(diff), ("docs/café.md",))
        self.assertEqual(parse_changed_lines(diff)["docs/café.md"], frozenset({1}))

        binary = "diff --git a/assets/logo.bin b/assets/logo.bin\nBinary files differ\n"
        self.assertEqual(parse_changed_paths(binary), ("assets/logo.bin",))

    def test_unquoted_space_containing_git_paths_match_real_git_headers(self):
        diff = """diff --git a/file with spaces.txt b/file with spaces.txt
--- a/file with spaces.txt
+++ b/file with spaces.txt
@@ -1 +1,2 @@
 keep
+changed
"""
        self.assertEqual(parse_changed_paths(diff), ("file with spaces.txt",))
        self.assertEqual(
            parse_changed_lines(diff),
            {"file with spaces.txt": frozenset({2})},
        )

        binary = "diff --git a/assets/logo with space.bin b/assets/logo with space.bin\nBinary files differ\n"
        self.assertEqual(parse_changed_paths(binary), ("assets/logo with space.bin",))

    def test_literal_glob_characters_in_git_paths_are_supported(self):
        diff = """diff --git a/src/foo[1].py b/src/foo[1].py
--- a/src/foo[1].py
+++ b/src/foo[1].py
@@ -1 +1,2 @@
 keep
+changed
"""
        self.assertEqual(parse_changed_paths(diff), ("src/foo[1].py",))
        self.assertEqual(
            parse_changed_lines(diff),
            {"src/foo[1].py": frozenset({2})},
        )

        binary = "diff --git a/docs/what*.md b/docs/what*.md\nBinary files differ\n"
        self.assertEqual(parse_changed_paths(binary), ("docs/what*.md",))

    def test_repeated_b_segments_resolve_same_path_text_and_binary_headers(self):
        header = "diff --git a/file b/with b/space.txt b/file b/with b/space.txt\n"
        text = (
            header
            + "--- a/file b/with b/space.txt\n"
            + "+++ b/file b/with b/space.txt\n"
            + "@@ -1 +1,2 @@\n keep\n+changed\n"
        )
        self.assertEqual(parse_changed_paths(text), ("file b/with b/space.txt",))
        self.assertEqual(
            parse_changed_lines(text),
            {"file b/with b/space.txt": frozenset({2})},
        )
        binary = header + "Binary files differ\n"
        self.assertEqual(parse_changed_paths(binary), ("file b/with b/space.txt",))

    def test_repeated_b_segments_resolve_distinct_rename_paths_from_metadata(self):
        diff = """diff --git a/old b/with b/old.txt b/new b/with b/new.txt
similarity index 100%
rename from old b/with b/old.txt
rename to new b/with b/new.txt
"""
        self.assertEqual(
            parse_changed_paths(diff),
            ("old b/with b/old.txt", "new b/with b/new.txt"),
        )

    def test_repeated_b_segments_fail_when_unresolved_or_conflicting(self):
        unresolved = (
            "diff --git a/old b/with b/old.txt b/new b/with b/new.txt\n"
            "Binary files differ\n"
        )
        conflicting = (
            "diff --git a/old b/with b/old.txt b/new b/with b/new.txt\n"
            "rename from unrelated\n"
            "rename to target\n"
        )
        for diff in (unresolved, conflicting):
            with self.subTest(diff=diff), self.assertRaises(ReviewInputError):
                parse_changed_paths(diff)

    def test_repeated_b_header_is_bounded_before_candidate_expansion(self):
        repeated = " b/segment" * 5_000
        diff = f"diff --git a/file{repeated} b/file{repeated}\nBinary files differ\n"
        self.assertLess(len(diff.encode("utf-8")), 1_048_576)
        with self.assertRaisesRegex(ReviewInputError, "Git header exceeds"):
            parse_changed_paths(diff)

    def test_maxish_repeated_b_same_path_header_resolves_in_bounded_form(self):
        path = "file" + (" b/segment" * 350)
        diff = f"diff --git a/{path} b/{path}\nBinary files differ\n"
        self.assertEqual(parse_changed_paths(diff), (path,))

    def test_hunks_close_only_after_exact_counts_and_require_file_markers(self):
        truncated = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 keep
"""
        before_next_hunk = truncated + "@@ -4 +4 @@\n+later\n"
        before_next_file = truncated + "diff --git a/other.py b/other.py\n"
        trailing = truncated + "trailing text\n"
        no_markers = """diff --git a/src/app.py b/src/app.py
@@ -1 +1 @@
 keep
"""
        for value in (
            truncated,
            before_next_hunk,
            before_next_file,
            trailing,
            no_markers,
        ):
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                analyze_diff(value)

    def test_zero_line_hunk_headers_cannot_use_zero_as_nonempty_start(self):
        diff = """--- a/src/app.py
+++ b/src/app.py
@@ -0,1 +1,1 @@
 old
"""
        with self.assertRaises(ReviewInputError):
            analyze_diff(diff)
        empty = """--- a/src/app.py
+++ b/src/app.py
@@ -1,0 +1,0 @@
"""
        with self.assertRaises(ReviewInputError):
            analyze_diff(empty)

    def test_hunk_numeric_overflow_is_sanitized(self):
        huge = "9" * 5_000
        diff = f"""--- a/src/app.py
+++ b/src/app.py
@@ -{huge},1 +1,1 @@
 old
"""
        with self.assertRaises(ReviewInputError):
            analyze_diff(diff)

    def test_malformed_paths_and_hunk_headers_fail_closed_without_echoing_input(self):
        marker = "TOP_SECRET_MARKER"
        bad = (
            f"diff --git a/../{marker} b/../{marker}\n",
            "--- a/file.py\n+++ a/file.py\n@@ nope @@\n+bad\n",
            'diff --git "a/file\\303" "b/file\\303"\n',
        )
        for diff in bad:
            with self.subTest(diff=diff), self.assertRaises(ReviewInputError) as raised:
                analyze_diff(diff)
            self.assertNotIn(marker, str(raised.exception))

    def test_unstructured_text_is_not_accepted_as_a_diff(self):
        with self.assertRaises(ReviewInputError):
            analyze_diff("not a unified diff")
