"""Frozen original context comes from bounded local Git objects, not checkout files."""

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from review_sensei.api_work import FrozenApiConfiguration
from review_sensei.errors import ReviewInputError


class QuarantineSourceTests(unittest.TestCase):
    def git(self, root, *args):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    *args,
                ],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    def fixture(self, root):
        self.git(root, "init")
        (root / ".reviewsensei.yml").write_text("schema: 1\napi_request_limit: 512\n")
        (root / "docs").mkdir()
        (root / "docs/architecture.md").write_text("# Original trusted architecture\n")
        (root / "file.py").write_text("old\n")
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "base")
        base = self.git(root, "rev-parse", "HEAD")
        (root / "file.py").write_text("new\n")
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "head")
        head = self.git(root, "rev-parse", "HEAD")
        return (
            base,
            head,
            FrozenApiConfiguration(
                base,
                hashlib.sha256((root / ".reviewsensei.yml").read_bytes()).hexdigest(),
                512,
            ),
        )

    def test_original_tree_and_diff_ignore_dirty_current_context_without_fetch_or_checkout(
        self,
    ):
        from review_sensei_github.quarantine_source import frozen_publication_source

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, head, configuration = self.fixture(root)
            (root / "docs/architecture.md").write_text(
                "Unrelated current user changes.\n"
            )
            (root / ".reviewsensei.yml").write_text(
                "Unrelated current configuration.\n"
            )
            before = self.git(root, "status", "--porcelain")
            with frozen_publication_source(
                root, configuration, base_sha=base, head_sha=head
            ) as source:
                self.assertEqual(
                    (source.root / "docs/architecture.md").read_text(),
                    "# Original trusted architecture\n",
                )
                self.assertIn("+new", source.diff)
                self.assertEqual(source.policy_commit, base)
                self.assertEqual(
                    source.diff_sha256, hashlib.sha256(source.diff.encode()).hexdigest()
                )
            self.assertFalse(source.root.exists())
            self.assertEqual(self.git(root, "status", "--porcelain"), before)
            self.assertEqual(
                (root / ".reviewsensei.yml").read_text(),
                "Unrelated current configuration.\n",
            )

    def test_missing_objects_wrong_configuration_and_symlink_sources_refuse(self):
        from review_sensei_github.quarantine_source import frozen_publication_source

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, head, configuration = self.fixture(root)
            cases = [
                (configuration, "0" * 40),
                (FrozenApiConfiguration(base, "0" * 64, 512), head),
            ]
            for config, selected_head in cases:
                with (
                    self.subTest(head=selected_head),
                    self.assertRaises(ReviewInputError),
                ):
                    with frozen_publication_source(
                        root, config, base_sha=base, head_sha=selected_head
                    ):
                        pass
            (root / "AGENTS.md").symlink_to("docs/architecture.md")
            self.git(root, "add", "AGENTS.md")
            self.git(root, "commit", "-m", "unsafe context link")
            policy = self.git(root, "rev-parse", "HEAD")
            with self.assertRaises(ReviewInputError):
                with frozen_publication_source(
                    root,
                    FrozenApiConfiguration(policy, configuration.digest, 512),
                    base_sha=base,
                    head_sha=head,
                ):
                    pass

    def test_repository_diff_driver_cannot_execute_during_original_context_read(self):
        from review_sensei_github.quarantine_source import frozen_publication_source

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, head, configuration = self.fixture(root)
            marker = root / "external-driver-ran"
            self.git(root, "config", "diff.untrusted.command", f"touch {marker}")
            (root / ".gitattributes").write_text("*.py diff=untrusted\n")
            with frozen_publication_source(
                root, configuration, base_sha=base, head_sha=head
            ) as source:
                self.assertIn("+new", source.diff)
            self.assertFalse(marker.exists())
