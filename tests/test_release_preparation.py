from __future__ import annotations

import copy
import hashlib
import json
import os
import re
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
    import prepare_release as PREP
finally:
    sys.path.pop(0)

CURRENT = PREP.versions.project_version(ROOT)
PARTS = PREP.docs.version_tuple("v" + CURRENT)
VERSION = ".".join(str(value) for value in (*PARTS[:2], PARTS[2] + 1))
DATE = "2026-10-08"
SHA = "a" * 40


class PreparationRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.fixture_dir.name) / "source"
        shutil.copytree(
            ROOT,
            cls.fixture,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "node_modules",
                "build",
                "dist",
                ".project-ai",
                "__pycache__",
                "*.egg-info",
            ),
        )
        # Release-source CI also runs these tests after preparation has consumed
        # Unreleased. This synthetic fixture always has reviewed next-release notes.
        changelog = cls.fixture / "CHANGELOG.md"
        prefix, _, rest = PREP.changelog_parts(changelog.read_text())
        changelog.write_text(
            prefix + "\n\n- Synthetic reviewed release notes.\n\n" + rest
        )

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        shutil.copytree(self.fixture, self.root)
        self.run_git("init", "-q")
        # Background Git maintenance can recreate .git entries while the
        # temporary fixture is being removed, especially with docs snapshots.
        self.run_git("config", "maintenance.auto", "false")
        self.run_git("config", "gc.auto", "0")
        # Model production's Linux checkout even when ROOT is checked out with
        # Windows CRLF. The explicit filter test still rejects changed blobs.
        self.run_git("config", "core.autocrlf", "false")
        for relative in PREP.ALLOWED_PATHS:
            path = self.root / relative
            if path.is_file():
                path.write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
                )
        self.run_git("add", ".")
        self.run_git("commit", "-qm", "Reviewed source")
        self.base = self.run_git("rev-parse", "HEAD")

    def run_git(self, *args):
        return subprocess.check_output(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    def generated(self):
        return PREP.generate(self.root, VERSION, DATE, self.base)

    def commit_generated(self):
        self.run_git("add", ".")
        PREP.validate_index(self.root)
        self.run_git("commit", "-qm", "Generated preparation")
        return self.run_git("rev-parse", "HEAD")

    def test_renderer_aligns_every_package_and_docs_without_changing_historical_or_provider_facts(
        self,
    ):
        old_site = json.loads(
            (self.root / "docs/site/data/site-manifest.json").read_text()
        )
        installation = self.root / "docs/installation.md"
        historical = installation.read_text().split("```text", 1)[1]
        changelog = (self.root / "CHANGELOG.md").read_text()
        _, _, history = PREP.changelog_parts(changelog)
        before = {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        receipt = self.generated()
        PREP.versions.validate_release_tag("v" + VERSION, self.root)
        new_site = json.loads(
            (self.root / "docs/site/data/site-manifest.json").read_text()
        )
        self.assertEqual(new_site["providers"], old_site["providers"])
        self.assertEqual(new_site["release_facts"]["version"], VERSION)
        self.assertEqual(new_site["last_updated"], DATE)
        self.assertIn(history, (self.root / "CHANGELOG.md").read_text())
        self.assertEqual(installation.read_text().split("```text", 1)[1], historical)
        self.assertIn("@reviewsensei/cli@" + VERSION, installation.read_text())
        changed = {
            name
            for name, value in before.items()
            if (self.root / name).read_bytes() != value
        }
        self.assertTrue(changed.issubset(PREP.ALLOWED_PATHS))
        self.assertEqual(set(receipt["files"]), PREP.PAYLOAD_PATHS)
        self.assertEqual(PREP.validate_payload(self.root), receipt)

    def test_receipt_binds_actual_parent_and_allowlisted_commit(self):
        self.generated()
        sha = self.commit_generated()
        self.assertEqual(PREP.verify_commit(self.root, sha)["base_sha"], self.base)
        self.run_git("commit", "--allow-empty", "-qm", "Different source")
        with self.assertRaisesRegex(ValueError, "exact reviewed parent"):
            PREP.verify_commit(self.root, self.run_git("rev-parse", "HEAD"))

    def test_native_newline_defaults_generate_identical_lf_payload(self):
        write_text = Path.write_text

        def windows_write_text(path, text, *args, **kwargs):
            if kwargs.get("newline") is None:
                kwargs["newline"] = "\r\n"
            return write_text(path, text, *args, **kwargs)

        with patch.object(Path, "write_text", new=windows_write_text):
            receipt = self.generated()
        for relative in PREP.ALLOWED_PATHS:
            with self.subTest(path=relative):
                self.assertNotIn(b"\r\n", (self.root / relative).read_bytes())
        sha = self.commit_generated()
        self.run_git("checkout-index", "--force", "--all")
        self.assertEqual(PREP.verify_commit(self.root, sha), receipt)

    def test_same_generated_intent_is_deterministic_and_refreshed_reviewed_notes_are_admitted(
        self,
    ):
        receipt = self.generated()
        self.assertEqual(PREP.generate(self.root, VERSION, DATE, self.base), receipt)
        with self.assertRaisesRegex(ValueError, "intent differs"):
            PREP.generate(self.root, VERSION, "2026-10-09", self.base)
        sha = self.commit_generated()
        path = self.root / "CHANGELOG.md"
        path.write_text(
            path.read_text().replace(
                "## Unreleased\n", "## Unreleased\n\n- Additional reviewed note.\n", 1
            )
        )
        self.run_git("add", "CHANGELOG.md")
        self.run_git("commit", "-qm", "Reviewed pending note")
        new_base = self.run_git("rev-parse", "HEAD")
        self.assertNotEqual(new_base, sha)
        refreshed = PREP.generate(self.root, VERSION, DATE, new_base)
        self.assertEqual(refreshed["base_sha"], new_base)
        self.assertIn(
            "Additional reviewed note.",
            PREP.released_notes(path.read_text(), VERSION, DATE),
        )
        self.assertEqual(
            PREP.verify_commit(self.root, self.commit_generated())["base_sha"], new_base
        )

    def test_changed_payload_outside_allowlist_and_git_filters_are_rejected(self):
        self.generated()
        path = self.root / "src/review_sensei/cli.py"
        path.write_text(path.read_text() + "\n# Unreviewed source\n")
        self.run_git("add", ".")
        with self.assertRaisesRegex(ValueError, "non-generated"):
            PREP.validate_index(self.root)
        self.run_git("reset", "--", "src/review_sensei/cli.py")
        attributes = self.root / ".git/info/attributes"
        attributes.write_text("README.md text eol=crlf\n")
        readme = self.root / "README.md"
        readme.write_bytes(readme.read_bytes().replace(b"\n", b"\r\n"))
        self.run_git("add", "README.md")
        with self.assertRaisesRegex(ValueError, "storage filters"):
            PREP.validate_index(self.root)

    def test_symlink_missing_marker_tampering_and_downgrade_are_rejected(self):
        path = self.root / "README.md"
        original = path.read_bytes()
        path.unlink()
        path.symlink_to(self.root / "CHANGELOG.md")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.generated()
        path.unlink()
        path.write_bytes(original)
        path.write_text(
            path.read_text().replace(
                "release-installation:package-npm-1:start",
                "release-installation:other:start",
            )
        )
        with self.assertRaisesRegex(ValueError, "unknown current installation"):
            self.generated()
        path.write_bytes(original)
        self.generated()
        path.write_text(path.read_text() + "\nTampered\n")
        with self.assertRaisesRegex(ValueError, "payload bytes"):
            PREP.validate_payload(self.root)
        with self.assertRaisesRegex(ValueError, "decrease"):
            PREP.generate(self.root, CURRENT, DATE, self.base)

    def test_empty_notes_and_invalid_inputs_fail_before_writing(self):
        for version, date, sha in (
            ("v0.6.17", DATE, self.base),
            (VERSION, "2026-02-30", self.base),
            (VERSION, DATE, "main"),
        ):
            with (
                self.subTest(version=version, date=date, sha=sha),
                self.assertRaises(ValueError),
            ):
                PREP.generate(self.root, version, date, sha)
        self.assertEqual(self.run_git("status", "--porcelain"), "")
        path = self.root / "CHANGELOG.md"
        prefix, _, rest = PREP.changelog_parts(path.read_text())
        path.write_text(prefix + "\n\n" + rest)
        with self.assertRaisesRegex(ValueError, "previous release tag"):
            self.generated()

    def automatic_fixture(self, subject="Preserve complete findings (#232)"):
        path = self.root / "CHANGELOG.md"
        prefix, _, rest = PREP.changelog_parts(path.read_text())
        path.write_text(prefix + "\n\n" + rest)
        self.run_git("add", "CHANGELOG.md")
        self.run_git("commit", "-qm", "Previous released source")
        previous = self.run_git("rev-parse", "HEAD")
        self.run_git(
            "-c",
            "tag.gpgsign=false",
            "tag",
            "-a",
            "v" + CURRENT,
            "-m",
            "Previous release",
        )
        tag_object = self.run_git("rev-parse", "refs/tags/v" + CURRENT)
        self.run_git("commit", "--allow-empty", "-qm", subject)
        self.base = self.run_git("rev-parse", "HEAD")
        return tag_object, previous

    def test_empty_unreleased_generates_exact_history_and_provenance_with_no_provider(
        self,
    ):
        tag_object, previous = self.automatic_fixture(
            "Fix [label](url) <script> `code` (#232)"
        )
        receipt = self.generated()
        notes = PREP.released_notes(
            (self.root / "CHANGELOG.md").read_text(), VERSION, DATE
        )
        self.assertIn(f"`{tag_object}`", notes)
        for sha in (previous, self.base):
            self.assertIn(f"/commit/{sha}", notes)
        self.assertIn(r"Fix \[label\]\(url\) &lt;script&gt; \`code\` \(\#232\)", notes)
        self.assertNotIn("<script>", notes)
        self.assertEqual(
            receipt["notes_sha256"], hashlib.sha256(notes.encode()).hexdigest()
        )
        self.assertEqual(self.generated(), receipt)
        self.assertEqual(
            PREP.verify_commit(self.root, self.commit_generated()), receipt
        )

    def test_automatic_notes_reject_empty_or_ambiguous_previous_range_before_writing(
        self,
    ):
        tag_object, previous = self.automatic_fixture()
        original = (self.root / "CHANGELOG.md").read_bytes()
        self.run_git("tag", "-d", "v" + CURRENT)
        self.run_git("-c", "tag.gpgsign=false", "tag", "v" + CURRENT, previous)
        with self.assertRaisesRegex(ValueError, "annotated previous"):
            self.generated()
        self.assertEqual((self.root / "CHANGELOG.md").read_bytes(), original)
        self.run_git("update-ref", "refs/tags/v" + CURRENT, tag_object)
        with self.assertRaisesRegex(ValueError, "changes after"):
            PREP.history_notes(self.root, CURRENT, previous)
        with (
            patch.object(PREP, "MAX_NOTE_COMMITS", 0),
            self.assertRaisesRegex(ValueError, "bounded main history"),
        ):
            self.generated()
        self.assertEqual((self.root / "CHANGELOG.md").read_bytes(), original)

    def test_history_uses_first_parent_merge_titles_without_repeating_branch_commits(
        self,
    ):
        _, previous = self.automatic_fixture()
        self.run_git("switch", "-qc", "feature")
        self.run_git("commit", "--allow-empty", "-qm", "Internal branch detail")
        self.run_git("switch", "--detach", self.base)
        self.run_git("merge", "--no-ff", "-qm", "Reviewed feature (#234)", "feature")
        self.base = self.run_git("rev-parse", "HEAD")
        notes, _, start = PREP.history_notes(self.root, CURRENT, self.base)
        self.assertEqual(start, previous)
        self.assertIn("Reviewed feature", notes)
        self.assertNotIn("Internal branch detail", notes)
        self.assertLess(
            notes.index("Preserve complete"), notes.index("Reviewed feature")
        )

    def test_previous_tag_on_a_side_branch_is_not_a_release_cutoff(self):
        _, previous = self.automatic_fixture()
        self.run_git("switch", "-qc", "unmerged", previous)
        self.run_git("commit", "--allow-empty", "-qm", "Unmerged source")
        self.run_git("tag", "-d", "v" + CURRENT)
        self.run_git(
            "-c", "tag.gpgsign=false", "tag", "-a", "v" + CURRENT, "-m", "Side tag"
        )
        self.run_git("switch", "--detach", self.base)
        with self.assertRaisesRegex(ValueError, "bounded main history"):
            self.generated()
        self.assertEqual(self.run_git("status", "--porcelain"), "")

    def test_oversized_or_control_character_subjects_and_notes_stop_before_generation(
        self,
    ):
        self.automatic_fixture()
        for subject in ("x" * 2049, "Unsafe\tcontrol"):
            with self.subTest(subject_size=len(subject)):
                self.run_git("commit", "--allow-empty", "-qm", subject)
                self.base = self.run_git("rev-parse", "HEAD")
                with self.assertRaisesRegex(ValueError, "single-line commit subjects"):
                    self.generated()
                self.assertEqual(self.run_git("status", "--porcelain"), "")
                self.run_git("reset", "--hard", "HEAD~1")
        self.base = self.run_git("rev-parse", "HEAD")
        with (
            patch.object(PREP, "MAX_NOTE_BYTES", 10),
            self.assertRaisesRegex(ValueError, "bounded history allowance"),
        ):
            self.generated()
        self.assertEqual(self.run_git("status", "--porcelain"), "")

    def test_forged_note_and_receipt_digest_cannot_replace_exact_generated_history(
        self,
    ):
        self.automatic_fixture()
        receipt = self.generated()
        path = self.root / "CHANGELOG.md"
        path.write_text(
            path.read_text().replace(
                "Preserve complete findings", "Invented release claim"
            )
        )
        notes = PREP.released_notes(path.read_text(), VERSION, DATE)
        receipt["notes_sha256"] = hashlib.sha256(notes.encode()).hexdigest()
        receipt["files"] = PREP.file_hashes(self.root)
        (self.root / PREP.RECEIPT).write_text(json.dumps(receipt))
        self.run_git("add", ".")
        self.run_git("commit", "-qm", "Forged generated notes")
        with self.assertRaisesRegex(ValueError, "exact source history"):
            PREP.verify_commit(self.root, self.run_git("rev-parse", "HEAD"))

    def test_existing_authored_notes_do_not_require_a_previous_tag(self):
        with patch.object(
            PREP, "history_notes", side_effect=AssertionError("unexpected history")
        ):
            self.generated()
            PREP.qualify_notes_origin(self.root, VERSION, self.base)

    def test_verified_previous_tag_required_and_rechecked_before_direct_main_push(self):
        identity = self.automatic_fixture()
        remote_main = self.remote_fixture()
        with (
            patch.object(PREP, "unused"),
            patch.object(PREP.docs, "tag_identity", return_value=identity) as tags,
        ):
            sha = PREP.prepare(self.root, VERSION, DATE, self.base)
        self.assertEqual(tags.call_count, 2)
        self.assertEqual(remote_main(), sha)
        self.run_git("fetch", "origin", "refs/heads/main")
        self.run_git("checkout", "--detach", sha)
        PREP.verify_commit(self.root, sha)
        self.assertIn(
            "Preserve complete findings", (self.root / "CHANGELOG.md").read_text()
        )

    def test_previous_tag_race_withholds_push_and_preserves_reviewed_main(self):
        identity = self.automatic_fixture()
        remote_main = self.remote_fixture()
        with (
            patch.object(PREP, "unused"),
            patch.object(
                PREP.docs,
                "tag_identity",
                side_effect=[identity, ("c" * 40, identity[1])],
            ),
            self.assertRaisesRegex(ValueError, "tag identity changed"),
        ):
            PREP.prepare(self.root, VERSION, DATE, self.base)
        self.assertEqual(remote_main(), self.base)
        self.assertEqual(self.run_git("status", "--porcelain"), "")

    def remote_fixture(self):
        remote = Path(self.temporary.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self.run_git("remote", "add", "origin", str(remote))
        self.run_git("push", "-q", "origin", "HEAD:refs/heads/main")
        real_git = PREP.git

        def local_git(root, *args):
            if args == ("remote", "get-url", "origin"):
                return "https://github.com/" + PREP.docs.REPOSITORY
            return real_git(root, *args)

        def remote_main():
            return subprocess.check_output(
                ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
                text=True,
            ).strip()

        self.enterContext(patch.object(PREP, "git", side_effect=local_git))
        self.enterContext(patch.object(PREP, "remote_main", side_effect=remote_main))
        self.enterContext(patch.object(PREP, "workflow_context"))
        self.enterContext(
            patch.object(
                PREP.bot,
                "check",
                return_value={
                    "name": "release-fixture[bot]",
                    "email": "fixture@example.invalid",
                },
            )
        )
        self.enterContext(patch.object(PREP, "ci_once", return_value=ci_run()))
        return remote_main

    def test_real_git_nonforce_prepare_and_same_intent_retry_do_not_duplicate_commits(
        self,
    ):
        remote_main = self.remote_fixture()
        with patch.object(PREP, "unused"):
            sha = PREP.prepare(self.root, VERSION, DATE, self.base)
            self.assertEqual(remote_main(), sha)
            self.assertNotEqual(sha, self.base)
            self.assertEqual(PREP.prepare(self.root, VERSION, DATE, sha), sha)
            self.assertEqual(PREP.prepare(self.root, VERSION, DATE, self.base), sha)
            with self.assertRaisesRegex(ValueError, "intent differs"):
                PREP.prepare(self.root, VERSION, "2026-10-09", sha)
        self.assertEqual(remote_main(), sha)
        self.assertEqual(self.run_git("status", "--porcelain"), "")

    def test_real_git_concurrent_main_advance_is_preserved_without_release_push(self):
        remote_main = self.remote_fixture()
        calls = []

        def unused(_version):
            calls.append(True)
            if len(calls) == 2:
                self.run_git(
                    "commit", "--allow-empty", "-qm", "Concurrent reviewed source"
                )
                self.run_git("push", "-q", "origin", "HEAD:refs/heads/main")

        with (
            patch.object(PREP, "unused", side_effect=unused),
            self.assertRaisesRegex(ValueError, "advanced before push"),
        ):
            PREP.prepare(self.root, VERSION, DATE, self.base)
        self.assertEqual(remote_main(), self.run_git("rev-parse", "HEAD"))
        # A previously prepared release can already have a tracked receipt.
        # Losing the main race must preserve every original checkout byte.
        self.assertEqual(self.run_git("status", "--porcelain"), "")


def ci_run(identity=42, status="completed", conclusion="success", attempt=1):
    return {
        "id": identity,
        "head_sha": SHA,
        "head_branch": "main",
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "repository": {"full_name": PREP.docs.REPOSITORY},
        "head_repository": {"full_name": PREP.docs.REPOSITORY},
        "pull_requests": [],
    }


def ci_jobs(attempt=1):
    return [
        {
            "name": name,
            "run_attempt": attempt,
            "status": "completed",
            "conclusion": "success",
        }
        for name in PREP.CI_JOBS
    ]


class PreparationQualificationTests(unittest.TestCase):
    def qualify(self, runs, jobs, fresh=None):
        def pages(path, key):
            return runs if key == "workflow_runs" else jobs

        with (
            patch.object(PREP.docs, "api_pages", side_effect=pages),
            patch.object(
                PREP.docs, "api", return_value=fresh or (runs[-1] if runs else {})
            ),
        ):
            return PREP.ci_once(SHA)

    def test_exact_ci_all_jobs_and_failed_only_retry_acceptance(self):
        self.assertEqual(self.qualify([ci_run()], ci_jobs())["id"], 42)
        old = ci_jobs()
        old[0]["conclusion"] = "failure"
        new = {**old[0], "run_attempt": 2, "conclusion": "success"}
        self.assertEqual(
            self.qualify([ci_run(attempt=2)], [*old, new])["run_attempt"], 2
        )

    def test_latest_run_and_each_latest_attempt_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "CI failed"):
            self.qualify([ci_run(), ci_run(43, conclusion="failure")], ci_jobs())
        for change in ("missing", "skipped", "failure", "duplicate", "invalid-attempt"):
            jobs = ci_jobs()
            if change == "missing":
                jobs.pop()
            elif change == "duplicate":
                jobs.append(copy.deepcopy(jobs[0]))
            elif change == "invalid-attempt":
                jobs[0]["run_attempt"] = 2
            else:
                jobs[0]["conclusion"] = change
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.qualify([ci_run()], jobs)
        self.assertIsNone(self.qualify([], []))
        self.assertIsNone(
            self.qualify([ci_run(status="in_progress", conclusion=None)], [])
        )

    def test_wrong_run_identity_and_racing_ci_retry_are_rejected(self):
        for field, value in (
            ("head_sha", "b" * 40),
            ("event", "pull_request"),
            ("head_branch", "feature"),
            ("path", ".github/workflows/other.yml"),
            ("pull_requests", [{"number": 1}]),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "exact main push"),
            ):
                self.qualify([{**ci_run(), field: value}], ci_jobs())
        with self.assertRaisesRegex(ValueError, "advanced"):
            self.qualify([ci_run()], ci_jobs(), ci_run(status="in_progress", attempt=2))

    def test_ci_wait_stops_on_main_advance_or_terminal_failure(self):
        with (
            patch.object(PREP, "remote_main", return_value="b" * 40),
            patch.object(PREP, "ci_once") as ci,
            self.assertRaisesRegex(ValueError, "main advanced"),
        ):
            PREP.wait_ci(ROOT, SHA, 1, None, None)
        ci.assert_not_called()
        with (
            patch.object(PREP, "remote_main", return_value=SHA),
            patch.object(PREP, "ci_once", side_effect=ValueError("CI failed")),
            self.assertRaisesRegex(ValueError, "CI failed"),
        ):
            PREP.wait_ci(ROOT, SHA, 1, None, None)

    def test_github_absence_requires_actual_404_not_auth_error(self):
        for status in (200, 401, 403, 404, 429, 500):
            response = subprocess.CompletedProcess(
                [], 0 if status == 200 else 1, f"HTTP/2.0 {status}\n\n{{}}", ""
            )
            with (
                self.subTest(status=status),
                patch.object(PREP.subprocess, "run", return_value=response),
            ):
                if status == 404:
                    PREP.absent_github("git/ref/tags/v" + VERSION)
                else:
                    with self.assertRaises(ValueError):
                        PREP.absent_github("git/ref/tags/v" + VERSION)

    def test_dispatch_context_does_not_allow_forks_tags_pushes_or_other_rerunners(self):
        env = {
            "GITHUB_REPOSITORY": PREP.docs.REPOSITORY,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTOR": "malsabbagh",
            "GITHUB_TRIGGERING_ACTOR": "malsabbagh",
        }
        with patch.dict(os.environ, env):
            PREP.workflow_context()
        for key, value in (
            ("GITHUB_REPOSITORY", "fork/repo"),
            ("GITHUB_REF", "refs/tags/v0.6.17"),
            ("GITHUB_EVENT_NAME", "push"),
            ("GITHUB_TRIGGERING_ACTOR", "other"),
        ):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {**env, key: value}),
                self.assertRaises(ValueError),
            ):
                PREP.workflow_context()

    def test_tag_guard_binds_signed_object_receipt_ci_and_main_ancestry(self):
        with (
            patch.object(PREP.docs, "tag_identity", return_value=("b" * 40, SHA)),
            patch.object(PREP, "git", side_effect=[SHA, "b" * 40]),
            patch.object(PREP, "verify_commit", return_value={"tag": "v" + VERSION}),
            patch.object(PREP.docs, "api", return_value={"status": "identical"}),
            patch.object(PREP, "ci_once", return_value=ci_run()),
            patch.dict(os.environ, {"GITHUB_SHA": SHA}),
        ):
            PREP.qualify_tag(ROOT, "v" + VERSION)
        with (
            patch.object(PREP.docs, "tag_identity", return_value=("b" * 40, SHA)),
            patch.object(PREP, "git", return_value="c" * 40),
            self.assertRaisesRegex(ValueError, "identities differ"),
        ):
            PREP.qualify_tag(ROOT, "v" + VERSION)


class PreparationWorkflowTests(unittest.TestCase):
    def test_preparation_is_manual_owner_main_and_uses_only_existing_narrow_token(self):
        text = (ROOT / ".github/workflows/prepare-release.yml").read_text()
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("  push:", text)
        self.assertNotIn("workflow_run:", text)
        self.assertIn("github.triggering_actor == 'malsabbagh'", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("environment: release-docs-main", text)
        self.assertIn("needs: qualify-base", text)
        self.assertIn("needs: prepare", text)
        self.assertIn("group: release-docs-main", text)
        self.assertNotRegex(
            text, re.compile(r"^\s+contents:\s+write\s*$", re.MULTILINE)
        )
        self.assertIn("permission-contents: write", text)
        for forbidden in (
            "permission-workflows",
            "permission-administration",
            "git tag",
            "npm publish",
            "gh release",
        ):
            self.assertNotIn(forbidden, text)
        for key in ("RELEASE_VERSION", "RELEASE_DATE", "SOURCE_SHA", "PREPARED_SHA"):
            self.assertIn(f'"${key}"', text)

    def test_all_release_build_lanes_depend_on_prepared_source_gate(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        for job in ("docs-build", "build", "native-build"):
            body = re.split(
                r"\n  [a-z][a-z0-9-]*:\n", text.split(f"  {job}:\n", 1)[1], maxsplit=1
            )[0]
            self.assertIn("needs: qualify-source", body)
        self.assertIn('prepare_release.py qualify-tag --tag "$GITHUB_REF_NAME"', text)
        self.assertNotIn("ref: ${{ github.ref }}", text)
        self.assertGreaterEqual(text.count("ref: ${{ github.sha }}"), 6)
