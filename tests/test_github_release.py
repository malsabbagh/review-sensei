from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import github_release as RELEASE
finally:
    sys.path.pop(0)

CONTEXT = RELEASE.Context("v1.2.3", "a" * 40, "b" * 40, 123, 2, "- Reviewed change.")


class GitHubReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = {}
        for name in (
            "review_sensei-1.2.3.tar.gz",
            "review_sensei-1.2.3-py3-none-any.whl",
            RELEASE.SBOM,
        ):
            folder = self.root / ("release" if name == RELEASE.SBOM else "dist")
            folder.mkdir(exist_ok=True)
            path = folder / name
            path.write_bytes(name.encode())
            self.assets[name] = path
        checksums = self.root / "release/SHA256SUMS"
        checksums.write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n"
                for name, path in self.assets.items()
            )
        )
        self.assets[checksums.name] = checksums
        self.releases = []
        self.remote_assets = []
        self.writes = []
        self.commands = []
        self.lost_response = set()
        self.run = {
            "id": 123,
            "run_attempt": 2,
            "path": ".github/workflows/release.yml",
            "event": "push",
            "head_branch": CONTEXT.tag,
            "head_sha": CONTEXT.source,
            "repository": {"full_name": RELEASE.docs.REPOSITORY},
            "head_repository": {"full_name": RELEASE.docs.REPOSITORY},
            "status": "in_progress",
            "pull_requests": [],
        }
        self.jobs = [
            {
                "name": name,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
            }
            for name in sorted(RELEASE.BUILD_JOBS | RELEASE.PUBLISH_JOBS)
        ]
        self.artifacts = [
            {
                "name": RELEASE.ARTIFACT,
                "expired": False,
                "workflow_run": {"id": 123, "head_sha": CONTEXT.source},
            }
        ]
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(RELEASE.docs, "api", side_effect=self.api))
        stack.enter_context(
            patch.object(RELEASE.docs, "api_pages", side_effect=self.pages)
        )
        stack.enter_context(
            patch.object(
                RELEASE.docs,
                "tag_identity",
                return_value=(
                    CONTEXT.tag_object,
                    CONTEXT.source,
                ),
            )
        )
        stack.enter_context(patch.object(RELEASE, "write_api", side_effect=self.write))
        stack.enter_context(
            patch.object(RELEASE.subprocess, "run", side_effect=self.command)
        )

    def api(self, path):
        self.assertEqual(path, "actions/runs/123")
        return copy.deepcopy(self.run)

    def pages(self, path, key=None):
        if path == "releases":
            return copy.deepcopy(self.releases)
        if path == "actions/runs/123/jobs?filter=all":
            self.assertEqual(key, "jobs")
            return copy.deepcopy(self.jobs)
        if path == "actions/runs/123/artifacts":
            return copy.deepcopy(self.artifacts)
        self.assertEqual(path, "releases/42/assets")
        return copy.deepcopy(self.remote_assets)

    def write(self, path, method, payload):
        self.writes.append((path, method, payload))
        if method == "POST":
            self.assertEqual(path, "releases")
            self.releases.append({"id": 42, **payload})
        else:
            self.assertEqual((path, method), ("releases/42", "PATCH"))
            self.releases[0].update(payload)
        if method in self.lost_response:
            raise subprocess.CalledProcessError(1, ["synthetic lost response"])

    def command(self, args, **kwargs):
        self.commands.append(args)
        if args[:3] == ["gh", "release", "upload"]:
            path = Path(args[4])
            self.remote_assets.append(
                {
                    "name": path.name,
                    "state": "uploaded",
                    "size": path.stat().st_size,
                    "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
            if "upload" in self.lost_response:
                raise subprocess.CalledProcessError(1, ["synthetic lost response"])
        else:
            self.assertEqual(args[:3], ["gh", "attestation", "verify"])
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    def draft(self):
        RELEASE.manage(CONTEXT, self.assets, publish=False)

    def test_draft_has_reviewed_notes_and_four_assets_without_publishing(self):
        self.draft()
        self.assertEqual(len(self.remote_assets), 4)
        release = self.releases[0]
        self.assertTrue(release["draft"])
        self.assertEqual(release["target_commitish"], CONTEXT.source)
        self.assertIn(CONTEXT.notes, release["body"])
        self.assertIn(CONTEXT.tag_object, release["body"])
        self.assertIn("/actions/runs/123", release["body"])
        self.assertEqual([method for _, method, _ in self.writes], ["POST"])

    def test_publisher_approval_can_remain_pending_during_draft(self):
        for job in self.jobs:
            if job["name"] in RELEASE.PUBLISH_JOBS:
                job.update(status="waiting", conclusion=None)
        self.draft()
        self.assertTrue(self.releases[0]["draft"])

    def test_idempotent_draft_and_publication_with_exact_existing_assets(self):
        self.draft()
        self.draft()
        RELEASE.manage(CONTEXT, self.assets, publish=True)
        RELEASE.manage(CONTEXT, self.assets, publish=True)
        self.assertFalse(self.releases[0]["draft"])
        self.assertEqual([method for _, method, _ in self.writes], ["POST", "PATCH"])
        self.assertEqual(self.writes[-1][2], {"draft": False, "make_latest": "legacy"})
        self.assertEqual(len(self.commands), 4)

    def test_lost_create_upload_and_publish_responses_reconcile_exact_state(self):
        self.lost_response.update({"POST", "upload", "PATCH"})
        self.draft()
        RELEASE.manage(CONTEXT, self.assets, publish=True)
        self.assertFalse(self.releases[0]["draft"])
        self.assertEqual(len(self.remote_assets), 4)

    def test_failed_creation_does_not_claim_success(self):
        with patch.object(
            RELEASE, "write_api", side_effect=subprocess.CalledProcessError(1, [])
        ):
            with self.assertRaisesRegex(ValueError, "creation"):
                self.draft()
        self.assertEqual(self.remote_assets, [])

    def test_failed_upload_keeps_draft_for_resume(self):
        original = self.command

        def fail_one(args, **kwargs):
            if Path(args[4]).name == RELEASE.SBOM:
                raise subprocess.CalledProcessError(1, [])
            return original(args, **kwargs)

        with patch.object(RELEASE.subprocess, "run", side_effect=fail_one):
            with self.assertRaisesRegex(ValueError, "upload"):
                self.draft()
        self.assertTrue(self.releases[0]["draft"])
        before = len(self.commands)
        self.draft()
        self.assertEqual(len(self.commands) - before, 3)
        self.assertEqual(len(self.remote_assets), 4)

    def test_failed_publication_does_not_claim_success(self):
        self.draft()
        with patch.object(
            RELEASE, "write_api", side_effect=subprocess.CalledProcessError(1, [])
        ):
            with self.assertRaisesRegex(ValueError, "publication"):
                RELEASE.manage(CONTEXT, self.assets, publish=True)
        self.assertTrue(self.releases[0]["draft"])

    def test_each_failed_or_pending_prerequisite_holds_publication(self):
        self.draft()
        for job in self.jobs:
            with self.subTest(job=job["name"]):
                job["conclusion"] = "failure"
                with self.assertRaisesRegex(ValueError, "prerequisite"):
                    RELEASE.manage(CONTEXT, self.assets, publish=True)
                self.assertTrue(self.releases[0]["draft"])
                job["conclusion"] = "success"

    def test_latest_attempt_failure_does_not_reuse_older_success(self):
        self.jobs.append(
            {
                "name": "Build release documentation",
                "run_attempt": 2,
                "status": "completed",
                "conclusion": "failure",
            }
        )
        with self.assertRaisesRegex(ValueError, "prerequisite"):
            self.draft()
        self.assertEqual(self.writes, [])

    def test_duplicate_or_invalid_job_attempt_is_refused(self):
        for attempt in (1, None, 3):
            with self.subTest(attempt=attempt):
                self.jobs.append({**self.jobs[0], "run_attempt": attempt})
                with self.assertRaises(ValueError):
                    self.draft()
                self.jobs.pop()
        self.assertEqual(self.writes, [])

    def test_wrong_run_identity_and_artifact_are_refused_before_creation(self):
        for key, value in (
            ("head_sha", "c" * 40),
            ("head_branch", "v2.0.0"),
            ("event", "workflow_dispatch"),
            ("run_attempt", 3),
            ("status", "completed"),
        ):
            with self.subTest(key=key):
                old = self.run[key]
                self.run[key] = value
                with self.assertRaises(ValueError):
                    self.draft()
                self.run[key] = old
        for artifact in (
            {**self.artifacts[0], "expired": True},
            {
                **self.artifacts[0],
                "workflow_run": {"id": 124, "head_sha": CONTEXT.source},
            },
        ):
            with patch.object(
                RELEASE.docs,
                "api_pages",
                side_effect=lambda path, key=None: (
                    [artifact] if path.endswith("/artifacts") else self.pages(path, key)
                ),
            ):
                with self.assertRaises(ValueError):
                    self.draft()
        self.assertEqual(self.writes, [])

    def test_tag_moved_just_before_create_holds_all_writes(self):
        with patch.object(
            RELEASE.docs,
            "tag_identity",
            side_effect=[
                (CONTEXT.tag_object, CONTEXT.source),
                ("c" * 40, CONTEXT.source),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "tag identity"):
                self.draft()
        self.assertEqual(self.writes, [])

    def test_foreign_or_edited_release_is_never_overwritten(self):
        self.draft()
        writes = list(self.writes)
        for key, value in (
            ("body", "human-edited notes"),
            ("target_commitish", "main"),
            ("name", "foreign"),
            ("prerelease", True),
            ("draft", None),
        ):
            with self.subTest(key=key):
                old = self.releases[0][key]
                self.releases[0][key] = value
                with self.assertRaisesRegex(ValueError, "no overwrite"):
                    self.draft()
                self.releases[0][key] = old
        self.assertEqual(self.writes, writes)

    def test_foreign_asset_bytes_missing_digest_or_duplicate_are_refused(self):
        self.draft()
        for key, value in (
            ("digest", "sha256:" + "0" * 64),
            ("digest", None),
            ("size", 0),
            ("state", "starter"),
            ("name", "foreign"),
        ):
            with self.subTest(key=key):
                old = self.remote_assets[0][key]
                self.remote_assets[0][key] = value
                with self.assertRaises(ValueError):
                    RELEASE.manage(CONTEXT, self.assets, publish=True)
                self.remote_assets[0][key] = old
        self.remote_assets.append(copy.deepcopy(self.remote_assets[0]))
        with self.assertRaises(ValueError):
            self.draft()
        self.assertTrue(self.releases[0]["draft"])

    def test_duplicate_releases_are_refused(self):
        self.draft()
        self.releases.append(copy.deepcopy(self.releases[0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.draft()

    def test_finalizer_never_creates_or_repairs_a_missing_draft(self):
        with self.assertRaisesRegex(ValueError, "requires.*draft"):
            RELEASE.manage(CONTEXT, self.assets, publish=True)
        self.draft()
        self.remote_assets.pop()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            RELEASE.manage(CONTEXT, self.assets, publish=True)
        self.assertEqual(len(self.writes), 1)

    def test_published_release_is_never_repaired_or_adopted_without_publish_proofs(
        self,
    ):
        self.draft()
        self.releases[0]["draft"] = False
        self.jobs[-1]["conclusion"] = "failure"
        with self.assertRaises(ValueError):
            self.draft()
        self.jobs[-1]["conclusion"] = "success"
        self.remote_assets.pop()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.draft()
        self.assertEqual(len(self.writes), 1)

    def test_bundle_validates_archive_and_attests_each_exact_payload(self):
        with patch.object(
            RELEASE.validate_release, "validate_release_directory"
        ) as validate:
            self.assertEqual(RELEASE.bundle_assets(self.root, CONTEXT), self.assets)
        validate.assert_called_once_with(self.root / "dist", "1.2.3")
        self.assertEqual(len(self.commands), 3)
        for args in self.commands:
            self.assertIn("--deny-self-hosted-runners", args)
            self.assertEqual(args[args.index("--source-digest") + 1], CONTEXT.source)
            self.assertEqual(args[args.index("--source-ref") + 1], "refs/tags/v1.2.3")
            self.assertEqual(
                args[args.index("--signer-workflow") + 1],
                f"{RELEASE.docs.REPOSITORY}/.github/workflows/release.yml",
            )

    def test_checksum_corruption_or_missing_sbom_is_refused(self):
        with patch.object(RELEASE.validate_release, "validate_release_directory"):
            self.assets[RELEASE.SBOM].write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "checksum differs"):
                RELEASE.bundle_assets(self.root, CONTEXT)
            self.assets[RELEASE.SBOM].unlink()
            with self.assertRaisesRegex(ValueError, "inventory"):
                RELEASE.bundle_assets(self.root, CONTEXT)

    def test_unsafe_duplicate_extra_or_incomplete_checksums_are_refused(self):
        path = self.assets["SHA256SUMS"]
        original = path.read_text()
        with patch.object(RELEASE.validate_release, "validate_release_directory"):
            for content in (
                original + original.splitlines()[0] + "\n",
                original + "0" * 64 + "  ../unsafe\n",
                original + "0" * 64 + "  foreign\n",
                "\n".join(original.splitlines()[:-1]),
            ):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(ValueError):
                        RELEASE.bundle_assets(self.root, CONTEXT)

    def test_bundle_symlinks_or_extra_files_are_refused(self):
        extra = self.root / "foreign"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "inventory"):
            RELEASE.bundle_assets(self.root, CONTEXT)
        extra.unlink()
        extra.symlink_to(self.assets[RELEASE.SBOM])
        with self.assertRaisesRegex(ValueError, "symlink"):
            RELEASE.bundle_assets(self.root, CONTEXT)

    def test_attestation_failure_stops_before_any_release_write(self):
        with (
            patch.object(RELEASE.validate_release, "validate_release_directory"),
            patch.object(
                RELEASE.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, []),
            ),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                RELEASE.bundle_assets(self.root, CONTEXT)
        self.assertEqual(self.writes, [])

    def test_coordinated_payload_and_checksum_tampering_still_requires_attestation(
        self,
    ):
        original = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in self.assets.items()
        }
        self.assets[RELEASE.SBOM].write_bytes(b"coordinated forged SBOM")
        self.assets["SHA256SUMS"].write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n"
                for name, path in self.assets.items()
                if name != "SHA256SUMS"
            )
        )

        def verify(args, **kwargs):
            path = Path(args[3])
            if hashlib.sha256(path.read_bytes()).hexdigest() != original[path.name]:
                raise subprocess.CalledProcessError(
                    1, ["synthetic attestation mismatch"]
                )
            return subprocess.CompletedProcess(args, 0)

        with (
            patch.object(RELEASE.validate_release, "validate_release_directory"),
            patch.object(RELEASE.subprocess, "run", side_effect=verify),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                RELEASE.bundle_assets(self.root, CONTEXT)
        self.assertEqual(self.writes, [])

    def test_source_requires_same_repo_tag_push_before_reading_receipt(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "tag push"):
                RELEASE.qualify_source(self.root, CONTEXT.tag)

    def test_source_uses_existing_exact_local_tag_receipt_and_ci_qualification(self):
        receipt = self.root / RELEASE.prepare.RECEIPT
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps({"version": "1.2.3", "release_date": "2026-10-09"})
        )
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## Unreleased\n\n## 1.2.3 - 2026-10-09\n\n- Reviewed change.\n"
        )
        env = {
            "GITHUB_REPOSITORY": RELEASE.docs.REPOSITORY,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/tags/v1.2.3",
            "GITHUB_SHA": CONTEXT.source,
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        with (
            patch.dict("os.environ", env, clear=True),
            patch.object(RELEASE.prepare, "qualify_tag") as qualify,
        ):
            self.assertEqual(RELEASE.qualify_source(self.root, CONTEXT.tag), CONTEXT)
            qualify.assert_called_once_with(self.root, CONTEXT.tag)
            qualify.side_effect = ValueError("tag/event/local source identities differ")
            with self.assertRaisesRegex(ValueError, "identities differ"):
                RELEASE.qualify_source(self.root, CONTEXT.tag)
        with (
            patch.dict("os.environ", env, clear=True),
            patch.object(RELEASE.prepare, "qualify_tag"),
            patch.object(
                RELEASE.docs,
                "tag_identity",
                side_effect=[
                    (CONTEXT.tag_object, CONTEXT.source),
                    ("c" * 40, CONTEXT.source),
                ],
            ),
        ):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                RELEASE.qualify_source(self.root, CONTEXT.tag)

    def test_source_consumes_actual_marshal_preparation_receipt(self):
        receipt_text = (ROOT / RELEASE.prepare.RECEIPT).read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        self.assertNotIn("date", receipt)
        path = self.root / RELEASE.prepare.RECEIPT
        path.parent.mkdir()
        path.write_text(receipt_text, encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        env = {
            "GITHUB_REPOSITORY": RELEASE.docs.REPOSITORY,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": f"refs/tags/{receipt['tag']}",
            "GITHUB_SHA": CONTEXT.source,
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        with (
            patch.dict("os.environ", env, clear=True),
            patch.object(RELEASE.prepare, "qualify_tag") as qualify,
        ):
            result = RELEASE.qualify_source(self.root, receipt["tag"])
        qualify.assert_called_once_with(self.root, receipt["tag"])
        self.assertEqual(result.tag, receipt["tag"])
        self.assertEqual(result.source, CONTEXT.source)
        self.assertEqual(
            result.notes,
            RELEASE.prepare.released_notes(
                changelog, receipt["version"], receipt["release_date"]
            ),
        )

    def test_workflow_draft_precedes_approvals_and_finalizer_waits_for_both(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("needs: [assemble-npm, draft-release]", text)
        self.assertIn("needs: [build, draft-release]", text)
        self.assertIn("needs: [build, draft-release, publish, publish-npm]", text)
        self.assertLess(
            text.index("Generate SPDX SBOM"), text.index("Create release checksums")
        )
        self.assertIn("sha256sum -- review-sensei-sbom.spdx.json", text)
        for job in ("draft-release", "github-release"):
            body = text.split(f"  {job}:\n", 1)[1].split("\n  github-release:", 1)[0]
            self.assertIn("actions: read", body)
            self.assertIn("attestations: read", body)
            self.assertIn("persist-credentials: false", body)
            self.assertNotIn("id-token: write", body)
            self.assertNotIn("RELEASE_SIGNING_PRIVATE_KEY", body)
        self.assertNotIn("--generate-notes", text)
        self.assertNotIn("--clobber", text)


if __name__ == "__main__":
    unittest.main()
