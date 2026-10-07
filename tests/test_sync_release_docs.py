from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    SPEC = importlib.util.spec_from_file_location(
        "sync_release_docs", ROOT / "scripts/sync_release_docs.py"
    )
    assert SPEC and SPEC.loader
    SYNC = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(SYNC)
finally:
    sys.path.pop(0)


def make_bundle(bundle: Path, tag: str = "v0.6.17", attempt: int = 1):
    selection = {
        "schema_version": "1.0",
        "repository": SYNC.docs.REPOSITORY,
        "tag": tag,
        "version": tag[1:],
        "source_sha": "a" * 40,
        "tag_object_sha": "b" * 40,
        "release_run_id": 42,
        "release_run_attempt": attempt,
        "artifact_id": 123,
    }
    site = bundle / "site"
    (site / "data").mkdir(parents=True)
    (site / "index.html").write_text(f"<main>{tag}</main>\n", encoding="utf-8")
    provenance = {
        key: value
        for key, value in selection.items()
        if key not in {"artifact_id", "release_run_attempt"}
    }
    provenance["build_run_attempt"] = attempt
    (site / "data/release-provenance.json").write_text(
        json.dumps(provenance), encoding="utf-8"
    )
    (site / "data/site-manifest.json").write_text(
        json.dumps({"release_facts": {"tag": tag, "version": tag[1:]}}),
        encoding="utf-8",
    )
    (bundle / "files.json").write_text(
        json.dumps(SYNC.docs.file_inventory(site)), encoding="utf-8"
    )
    return selection


class ReleaseDocsSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "main"
        self.root.mkdir()
        (self.root / "docs/releases").mkdir(parents=True)
        (self.root / "docs/releases/.gitattributes").write_text(
            SYNC.STORAGE_POLICY, encoding="utf-8"
        )
        (self.root / "pyproject.toml").write_text("future development is preserved\n")
        self.bundle = Path(self.temporary.name) / "bundle"
        self.selection = make_bundle(self.bundle)

    def test_complete_snapshot_and_index_only_and_same_cutoff_is_idempotent(self):
        self.assertTrue(SYNC.prepare(self.root, self.bundle, self.selection))
        snapshot = self.root / SYNC.SNAPSHOT
        SYNC.docs.verify_bundle(snapshot, self.selection)
        text = (self.root / SYNC.INDEX).read_text()
        self.assertIn("pip install review-sensei==0.6.17", text)
        self.assertIn(f"/tree/{self.selection['source_sha']}/examples", text)
        before = SYNC.docs.file_inventory(self.root)
        retry = Path(self.temporary.name) / "retry"
        selection = make_bundle(retry, attempt=2)
        selection["artifact_id"] = 456
        self.assertFalse(SYNC.prepare(self.root, retry, selection))
        self.assertEqual(SYNC.docs.file_inventory(self.root), before)
        self.assertEqual(
            (self.root / "pyproject.toml").read_text(),
            "future development is preserved\n",
        )

    def test_newer_snapshot_replaces_old_but_older_and_changed_identity_fail(self):
        SYNC.prepare(self.root, self.bundle, self.selection)
        newer = Path(self.temporary.name) / "newer"
        selection = make_bundle(newer, "v0.6.18")
        self.assertTrue(SYNC.prepare(self.root, newer, selection))
        with self.assertRaisesRegex(ValueError, "newer"):
            SYNC.prepare(self.root, self.bundle, self.selection)
        changed = dict(selection, source_sha="c" * 40)
        provenance = newer / "site/data/release-provenance.json"
        data = json.loads(provenance.read_text())
        data["source_sha"] = changed["source_sha"]
        provenance.write_text(json.dumps(data))
        (newer / "files.json").write_text(
            json.dumps(SYNC.docs.file_inventory(newer / "site"))
        )
        with self.assertRaisesRegex(ValueError, "immutable"):
            SYNC.prepare(self.root, newer, changed)

    def test_tampered_input_and_stored_snapshot_or_index_are_rejected(self):
        SYNC.prepare(self.root, self.bundle, self.selection)
        site = self.root / SYNC.SNAPSHOT / "site/index.html"
        original = site.read_text()
        site.write_text("modified")
        with self.assertRaisesRegex(ValueError, "inventory"):
            SYNC.prepare(self.root, self.bundle, self.selection)
        site.write_text(original)
        index = self.root / SYNC.INDEX
        index.write_text("modified")
        with self.assertRaisesRegex(ValueError, "index was modified"):
            SYNC.prepare(self.root, self.bundle, self.selection)
        (self.bundle / "site/index.html").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "inventory"):
            SYNC.prepare(self.root, self.bundle, self.selection)

    def test_symlink_parent_and_partial_snapshot_are_rejected(self):
        elsewhere = Path(self.temporary.name) / "outside"
        elsewhere.mkdir()
        shutil.rmtree(self.root / "docs")
        (self.root / "docs").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            SYNC.prepare(self.root, self.bundle, self.selection)
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.root / "docs").unlink()
        (self.root / "docs/releases").mkdir(parents=True)
        (self.root / "docs/releases/.gitattributes").write_text(
            SYNC.STORAGE_POLICY, encoding="utf-8"
        )
        (self.root / SYNC.INDEX).write_text("orphan")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            SYNC.prepare(self.root, self.bundle, self.selection)

    def test_same_cutoff_rejects_edits_even_if_the_local_inventory_was_recomputed(self):
        SYNC.prepare(self.root, self.bundle, self.selection)
        snapshot = self.root / SYNC.SNAPSHOT
        (snapshot / "site/index.html").write_text("changed and rehashed\n")
        (snapshot / "files.json").write_text(
            json.dumps(SYNC.docs.file_inventory(snapshot / "site"))
        )
        with self.assertRaisesRegex(ValueError, "differ from the verified artifact"):
            SYNC.prepare(self.root, self.bundle, self.selection)

    def test_rule_gate_uses_only_dedicated_app_contract(self):
        with (
            patch.dict(
                SYNC.os.environ,
                {"RELEASE_DOCS_APP_ID": "123", "RELEASE_DOCS_APP_SLUG": "docs-bot"},
            ),
            patch.object(
                SYNC.bot,
                "check",
                return_value={"name": "docs-bot[bot]", "email": "docs@example.test"},
            ) as check,
        ):
            self.assertEqual(SYNC.check_main_rules()["name"], "docs-bot[bot]")
            check.assert_called_once_with("123", "docs-bot")

    def test_verified_and_rehashed_stored_attribute_overrides_are_rejected(self):
        SYNC.prepare(self.root, self.bundle, self.selection)
        for directory in (self.bundle, self.root / SYNC.SNAPSHOT):
            with self.subTest(directory=directory):
                override = directory / "site/.gitattributes"
                override.write_text("*.html text eol=lf\n")
                (directory / "files.json").write_text(
                    json.dumps(SYNC.docs.file_inventory(directory / "site"))
                )
                with self.assertRaisesRegex(ValueError, "storage attributes"):
                    SYNC.prepare(self.root, self.bundle, self.selection)
                override.unlink()
                (directory / "files.json").write_text(
                    json.dumps(SYNC.docs.file_inventory(directory / "site"))
                )

    def test_readiness_cli_does_not_qualify_a_release_or_write_git(self):
        with (
            patch.dict(SYNC.os.environ, {"GH_TOKEN": "test-token"}),
            patch.object(sys, "argv", ["sync_release_docs.py", "--check-bot"]),
            patch.object(
                SYNC, "check_main_rules", return_value={"name": "docs-bot[bot]"}
            ),
            patch.object(SYNC, "sync") as sync,
            patch.object(SYNC, "recheck") as recheck,
            patch.object(SYNC, "git") as git,
        ):
            self.assertEqual(SYNC.main(), 0)
            sync.assert_not_called()
            recheck.assert_not_called()
            git.assert_not_called()


class ReleaseDocsMainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.origin = self.directory / "origin.git"
        self.seed = self.directory / "seed"
        subprocess.run(
            ["git", "init", "--bare", "-q", "--initial-branch=main", str(self.origin)],
            check=True,
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.seed)],
            check=True,
            capture_output=True,
        )
        SYNC.git(self.seed, "config", "user.name", "Docs Test")
        SYNC.git(self.seed, "config", "user.email", "docs@example.test")
        (self.seed / "docs/releases").mkdir(parents=True)
        (self.seed / "docs/releases/.gitattributes").write_text(
            SYNC.STORAGE_POLICY, encoding="utf-8"
        )
        SYNC.git(self.seed, "add", "docs/releases/.gitattributes")
        self.advance_main("before")
        self.root = self.directory / "controller"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.root)], check=True
        )
        self.bundle = self.directory / "bundle"
        self.selection = make_bundle(self.bundle)
        for name in ("recheck", "check_main_rules"):
            mocked = patch.object(SYNC, name)
            started = mocked.start()
            if name == "check_main_rules":
                started.return_value = {
                    "name": "docs-bot[bot]",
                    "email": "docs@example.test",
                }
            self.addCleanup(mocked.stop)

    def advance_main(self, content):
        (self.seed / "README.md").write_text(content + "\n")
        SYNC.git(self.seed, "add", "README.md")
        SYNC.git(self.seed, "commit", "-qm", content)
        SYNC.git(self.seed, "push", "origin", "HEAD:main")

    def remote_file(self, path):
        return SYNC.git(self.origin, "show", f"main:{path}")

    def test_real_non_force_push_uses_fresh_main_and_retry_is_a_noop(self):
        self.advance_main("new main work")
        self.assertTrue(SYNC.sync(self.root, self.bundle, self.selection))
        self.assertEqual(self.remote_file("README.md"), "new main work")
        self.assertIn("v0.6.17", self.remote_file(SYNC.INDEX))
        before = SYNC.git(self.origin, "rev-parse", "main")
        self.assertFalse(SYNC.sync(self.root, self.bundle, self.selection))
        self.assertEqual(SYNC.git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(
            len(
                SYNC.git(self.root, "worktree", "list", "--porcelain").split(
                    "worktree "
                )
            )
            - 1,
            1,
        )

    def test_snapshot_bytes_survive_checkout_with_git_line_ending_conversion_enabled(
        self,
    ):
        SYNC.git(self.root, "config", "core.autocrlf", "true")
        self.test_real_non_force_push_uses_fresh_main_and_retry_is_a_noop()

    def test_verified_crlf_artifact_is_committed_byte_for_byte_and_retry_is_a_noop(
        self,
    ):
        html = self.bundle / "site/index.html"
        content = b"<main>v0.6.17</main> \r\n"
        html.write_bytes(content)
        (self.bundle / "files.json").write_text(
            json.dumps(SYNC.docs.file_inventory(self.bundle / "site")), encoding="utf-8"
        )
        self.assertTrue(SYNC.sync(self.root, self.bundle, self.selection))
        committed = subprocess.check_output(
            [
                "git",
                "-C",
                str(self.origin),
                "show",
                f"main:{SYNC.SNAPSHOT}/site/index.html",
            ]
        )
        self.assertEqual(committed, content)
        self.assertFalse(SYNC.sync(self.root, self.bundle, self.selection))

    def test_actual_main_advance_during_push_is_reintegrated_with_no_force(self):
        real_run = subprocess.run
        pushes = []

        def run(args, **kwargs):
            if "push" in args and str(self.seed) not in args:
                pushes.append(args)
                if len(pushes) == 1:
                    self.advance_main("concurrent main work")
            return real_run(args, **kwargs)

        with patch.object(SYNC.subprocess, "run", side_effect=run):
            self.assertTrue(SYNC.sync(self.root, self.bundle, self.selection))
        self.assertEqual(len(pushes), 2)
        self.assertTrue(
            all(
                not any(
                    arg.startswith("--force") or arg.startswith("+") for arg in args
                )
                for args in pushes
            )
        )
        self.assertEqual(self.remote_file("README.md"), "concurrent main work")

    def test_rejected_push_on_unchanged_main_is_terminal_without_retry(self):
        real_run = subprocess.run
        pushes = []

        def run(args, **kwargs):
            if "push" in args:
                pushes.append(args)
                return subprocess.CompletedProcess(
                    args, 1, "", "GH013 repository rule violation"
                )
            return real_run(args, **kwargs)

        with patch.object(SYNC.subprocess, "run", side_effect=run):
            before = SYNC.git(self.origin, "rev-parse", "main")
            with self.assertRaisesRegex(ValueError, "No other bypass attempted"):
                SYNC.sync(self.root, self.bundle, self.selection)
            self.assertEqual(len(pushes), 1)
            self.assertEqual(SYNC.git(self.origin, "rev-parse", "main"), before)

    def test_final_requalification_failure_prevents_push(self):
        before = SYNC.git(self.origin, "rev-parse", "main")
        with patch.object(
            SYNC, "recheck", side_effect=[None, ValueError("newer successful release")]
        ):
            with self.assertRaisesRegex(ValueError, "newer successful release"):
                SYNC.sync(self.root, self.bundle, self.selection)
        self.assertEqual(SYNC.git(self.origin, "rev-parse", "main"), before)

    def test_non_generated_staged_change_is_rejected(self):
        (self.root / "runtime.py").write_text("bad change\n")
        SYNC.git(self.root, "add", "runtime.py")
        with self.assertRaisesRegex(ValueError, "non-generated"):
            SYNC.validate_staged_paths(self.root)

    def test_ignored_snapshot_payload_cannot_be_partially_committed(self):
        (self.bundle / "site/.gitignore").write_text("index.html\n")
        (self.bundle / "files.json").write_text(
            json.dumps(SYNC.docs.file_inventory(self.bundle / "site"))
        )
        before = SYNC.git(self.origin, "rev-parse", "main")
        with self.assertRaisesRegex(ValueError, "omitted part"):
            SYNC.sync(self.root, self.bundle, self.selection)
        self.assertEqual(SYNC.git(self.origin, "rev-parse", "main"), before)

    def test_git_filter_changed_payload_is_rejected_before_commit(self):
        # info/attributes has higher precedence than tracked storage policy.
        # Simulate a runner override and retain the original verified CRLF data.
        attributes = self.root / ".git/info/attributes"
        attributes.write_text(f"{SYNC.SNAPSHOT}/site/index.html text eol=lf\n")
        (self.bundle / "site/index.html").write_bytes(b"<main>v0.6.17</main>\r\n")
        (self.bundle / "files.json").write_text(
            json.dumps(SYNC.docs.file_inventory(self.bundle / "site"))
        )
        before = SYNC.git(self.origin, "rev-parse", "main")
        with self.assertRaisesRegex(ValueError, "changed verified"):
            SYNC.sync(self.root, self.bundle, self.selection)
        self.assertEqual(SYNC.git(self.origin, "rev-parse", "main"), before)

    def test_three_concurrent_advances_stop_with_no_docs_push(self):
        real_run = subprocess.run
        count = 0

        def run(args, **kwargs):
            nonlocal count
            if "push" in args and str(self.seed) not in args:
                count += 1
                self.advance_main(f"advance {count}")
            return real_run(args, **kwargs)

        with patch.object(SYNC.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "three docs integration attempts"):
                SYNC.sync(self.root, self.bundle, self.selection)
        self.assertEqual(count, 3)
        self.assertEqual(self.remote_file("README.md"), "advance 3")
        self.assertNotIn(
            SYNC.INDEX,
            SYNC.git(self.origin, "ls-tree", "-r", "--name-only", "main"),
        )


if __name__ == "__main__":
    unittest.main()
