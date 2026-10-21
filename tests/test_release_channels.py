from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
channels = importlib.import_module("release_channels")
SOURCE = "a" * 40
TAG_OBJECT = "b" * 40
VERSION = "0.8.7"
TAG = "v" + VERSION


def run(channel="python", **changes):
    return {
        "id": 41 if channel == "python" else 42,
        "path": ".github/workflows/" + channels.WORKFLOWS[channel],
        "event": "push",
        "head_branch": TAG,
        "head_sha": SOURCE,
        "repository": {"full_name": channels.docs.REPOSITORY},
        "head_repository": {"full_name": channels.docs.REPOSITORY},
        "pull_requests": [],
        "run_attempt": 2,
        "status": "in_progress",
        **changes,
    }


def environment(channel="python"):
    record = run(channel)
    return {
        "GITHUB_REPOSITORY": channels.docs.REPOSITORY,
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF_NAME": TAG,
        "GITHUB_REF": "refs/tags/" + TAG,
        "GITHUB_SHA": SOURCE,
        "GITHUB_RUN_ID": str(record["id"]),
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_WORKFLOW_REF": channels.docs.REPOSITORY
        + "/"
        + record["path"]
        + "@refs/tags/"
        + TAG,
    }


class ReleaseChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        shutil.copy(ROOT / "pyproject.toml", self.root)
        shutil.copy(ROOT / "CHANGELOG.md", self.root)
        shutil.copytree(ROOT / "packages/npm", self.root / "packages/npm")
        shutil.copytree(ROOT / ".publication", self.root / ".publication")
        self.enterContext(
            patch.object(
                channels.docs, "tag_identity", return_value=(TAG_OBJECT, SOURCE)
            )
        )
        self.prepared = self.enterContext(patch.object(channels.prepare, "qualify_tag"))
        self.runs = {key: run(key) for key in channels.WORKFLOWS}
        self.queries = []
        self.event_path = self.root / "event.json"
        self.event_path.write_text(
            json.dumps(
                {
                    "repository": {"full_name": channels.docs.REPOSITORY},
                    "ref": "refs/tags/" + TAG,
                    "created": True,
                    "deleted": False,
                    "after": TAG_OBJECT,
                }
            )
        )
        self.enterContext(
            patch.object(channels.docs, "api_pages", side_effect=self.pages)
        )
        self.jobs = []
        self.artifacts = []

    def environment(self, channel="python"):
        return {**environment(channel), "GITHUB_EVENT_PATH": str(self.event_path)}

    def pages(self, path, key=None):
        self.queries.append(path)
        for channel, workflow in channels.WORKFLOWS.items():
            if path.startswith(f"actions/workflows/{workflow}/runs?"):
                return [self.runs[channel]]
        if path.endswith("/jobs?filter=all"):
            return self.jobs
        if path.endswith("/artifacts"):
            return self.artifacts
        raise AssertionError(path)

    def test_one_signed_marshal_contract_binds_both_channels_and_same_core_version(
        self,
    ):
        contracts = []
        for channel in channels.WORKFLOWS:
            with patch.dict(os.environ, self.environment(channel), clear=True):
                contracts.append(channels.qualify(channel, self.root))
        for field in ("repository", "version", "tag", "tag_object", "source_sha"):
            self.assertEqual(contracts[0][field], contracts[1][field])
        self.assertNotEqual(contracts[0]["run_id"], contracts[1]["run_id"])
        self.assertEqual(contracts[0]["version"], VERSION)
        self.assertEqual(self.prepared.call_count, 2)

    def test_npm_waiting_or_failure_does_not_enter_python_retry_or_qualification(self):
        for status, conclusion in (
            ("waiting", None),
            ("in_progress", None),
            ("completed", "failure"),
        ):
            self.runs["npm"].update(status=status, conclusion=conclusion)
            with patch.dict(os.environ, self.environment(), clear=True):
                self.assertEqual(
                    channels.qualify("python", self.root)["run_attempt"], 2
                )
        self.assertTrue(self.queries)
        self.assertTrue(all("release.yml/runs?" in q for q in self.queries))

    def test_dispatch_fork_branch_wrong_workflow_and_stale_attempt_fail_closed(self):
        for key, value in (
            ("GITHUB_EVENT_NAME", "workflow_dispatch"),
            ("GITHUB_REPOSITORY", "attacker/fork"),
            ("GITHUB_REF", "refs/heads/main"),
            ("GITHUB_WORKFLOW_REF", "attacker/workflow@main"),
            ("GITHUB_SHA", "c" * 40),
            ("GITHUB_RUN_ID", "900"),
            ("GITHUB_RUN_ATTEMPT", "1"),
        ):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {**self.environment(), key: value}, clear=True),
            ):
                with self.assertRaises(ValueError):
                    channels.qualify("python", self.root)
        self.prepared.assert_not_called()

    def test_tag_update_stale_event_and_changed_original_qualification_refuse(self):
        original = json.loads(self.event_path.read_text())
        for key, value in (
            ("created", False),
            ("deleted", True),
            ("after", "c" * 40),
            ("repository", {"full_name": "attacker/fork"}),
        ):
            self.event_path.write_text(json.dumps({**original, key: value}))
            with (
                self.subTest(key=key),
                patch.dict(os.environ, self.environment(), clear=True),
                self.assertRaisesRegex(ValueError, "tag creation"),
            ):
                channels.qualify("python", self.root)
        self.event_path.write_text(json.dumps(original))
        with (
            patch.dict(
                os.environ,
                {**self.environment(), "EXPECTED_TAG_OBJECT": "c" * 40},
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "changed after"),
        ):
            channels.qualify("python", self.root)
        self.prepared.assert_not_called()

    def test_duplicate_original_run_is_rejected_even_when_one_failed(self):
        for state in ("waiting", "completed"):
            with (
                patch.dict(os.environ, self.environment(), clear=True),
                patch.object(
                    channels.docs,
                    "api_pages",
                    return_value=[
                        run(),
                        run(id=99, status=state, conclusion="failure"),
                    ],
                ),
                self.assertRaisesRegex(ValueError, "one original"),
            ):
                channels.qualify("python", self.root)
        self.prepared.assert_not_called()

    def test_tag_source_run_metadata_and_version_mismatch_fail_closed(self):
        original = run()
        for key, value in (
            ("head_sha", "c" * 40),
            ("head_branch", "v0.8.8"),
            ("event", "workflow_dispatch"),
            ("path", ".github/workflows/publish-npm.yml"),
            ("head_repository", {"full_name": "attacker/fork"}),
            ("pull_requests", [{}]),
            ("status", "completed"),
        ):
            self.runs["python"] = {**original, key: value}
            with (
                self.subTest(key=key),
                patch.dict(os.environ, self.environment(), clear=True),
                self.assertRaises(ValueError),
            ):
                channels.qualify("python", self.root)
        self.runs["python"] = original
        manifest = self.root / "packages/npm/cli/package.json"
        metadata = json.loads(manifest.read_text())
        metadata["version"] = "0.8.8"
        manifest.write_text(json.dumps(metadata))
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            self.assertRaisesRegex(ValueError, "version"),
        ):
            channels.qualify("python", self.root)

    def test_drift_between_contract_reads_is_refused(self):
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(
                channels.docs,
                "tag_identity",
                side_effect=[(TAG_OBJECT, SOURCE), ("c" * 40, SOURCE)],
            ),
            self.assertRaises(ValueError),
        ):
            channels.qualify("python", self.root)

    def test_channel_result_retains_failure_and_original_run_and_artifact_facts(self):
        self.jobs = [
            {
                "id": 1,
                "name": "Build",
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 2,
                "name": "Finalizer",
                "run_attempt": 2,
                "status": "completed",
                "conclusion": "failure",
            },
        ]
        self.artifacts = [
            {
                "id": 8,
                "name": "bundle",
                "digest": "sha256:" + "c" * 64,
                "expired": False,
            }
        ]
        with (
            patch.dict(os.environ, self.environment(), clear=True),
            patch.object(channels.docs, "api", return_value=run()),
        ):
            receipt = channels.record("python")
        self.assertFalse(receipt["successful"])
        self.assertEqual(receipt["run_id"], 41)
        self.assertEqual(receipt["source_sha"], SOURCE)
        self.assertEqual(receipt["jobs"][1]["conclusion"], "failure")
        self.assertEqual(receipt["artifacts"], self.artifacts)

    def npm_bundle(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        records = []
        for index, name in enumerate(channels.npm.ALL_PACKAGES):
            filename = f"package-{index}.tgz"
            raw = filename.encode()
            (bundle / filename).write_bytes(raw)
            records.append(
                {
                    "file": filename,
                    "name": name,
                    "version": VERSION,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "integrity": "sha512-fixture",
                }
            )
        (bundle / "integrity.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records)
        )
        (bundle / "bundle-metadata.json").write_text(
            json.dumps({"version": VERSION, "source_sha": SOURCE})
        )
        (bundle / "SHA256SUMS").write_text(
            "".join(
                f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                for p in bundle.iterdir()
                if p.name != "integrity.jsonl"
            )
        )
        self.artifacts = [
            {
                "id": 8,
                "name": channels.BUNDLE,
                "digest": "sha256:" + "d" * 64,
                "expired": False,
                "workflow_run": {"id": 42, "head_sha": SOURCE},
            }
        ]
        return bundle

    def npm_env(self):
        return {
            **self.environment("npm"),
            "BUNDLE_ARTIFACT_ID": "8",
            "BUNDLE_ARTIFACT_DIGEST": "d" * 64,
            "BUNDLE_BUILD_ATTEMPT": "1",
        }

    def test_npm_retry_reuses_original_attempt_bundle_without_python_finalizer_success(
        self,
    ):
        bundle = self.npm_bundle()
        with (
            patch.dict(os.environ, self.npm_env(), clear=True),
            patch.object(channels, "verify_attestation") as attest,
            patch.object(channels, "verify_draft") as draft,
        ):
            channels.verify_npm(self.root, bundle)
        self.assertEqual(attest.call_count, 7)
        self.assertTrue(all(c.args[2] == 1 for c in attest.call_args_list))
        draft.assert_called_once()

    def test_cross_run_artifact_wrong_digest_expiry_and_duplicate_artifact_refused(
        self,
    ):
        bundle = self.npm_bundle()
        original = copy.deepcopy(self.artifacts[0])
        for key, value in (
            ("id", 9),
            ("digest", "sha256:" + "e" * 64),
            ("expired", True),
            ("workflow_run", {"id": 40, "head_sha": SOURCE}),
            ("workflow_run", {"id": 42, "head_sha": "c" * 40}),
        ):
            self.artifacts = [{**original, key: value}]
            with (
                self.subTest(key=key, value=value),
                patch.dict(os.environ, self.npm_env(), clear=True),
                self.assertRaisesRegex(ValueError, "original same-run"),
            ):
                channels.verify_npm(self.root, bundle)
        self.artifacts = [original, original]
        with (
            patch.dict(os.environ, self.npm_env(), clear=True),
            self.assertRaisesRegex(ValueError, "original same-run"),
        ):
            channels.verify_npm(self.root, bundle)

    def test_npm_bundle_source_or_version_mismatch_refuses_before_attestation(self):
        bundle = self.npm_bundle()
        for metadata in (
            {"version": "0.8.8", "source_sha": SOURCE},
            {"version": VERSION, "source_sha": "c" * 40},
        ):
            (bundle / "bundle-metadata.json").write_text(json.dumps(metadata))
            with (
                patch.dict(os.environ, self.npm_env(), clear=True),
                patch.object(channels, "verify_attestation") as attest,
                self.assertRaises(channels.npm.PublishError),
            ):
                channels.verify_npm(self.root, bundle)
            attest.assert_not_called()

    def test_verified_attestation_requires_exact_original_invocation_and_strict_cli_policy(
        self,
    ):
        contract = {
            "workflow": "publish-npm.yml",
            "source_sha": SOURCE,
            "tag": TAG,
            "run_id": 42,
        }
        invocation = (
            f"https://github.com/{channels.docs.REPOSITORY}/actions/runs/42/attempts/1"
        )
        data = [
            {
                "verificationResult": {
                    "signature": {"certificate": {"runInvocationURI": invocation}},
                    "statement": {
                        "predicate": {
                            "runDetails": {"metadata": {"invocationId": invocation}}
                        }
                    },
                }
            }
        ]
        with patch.object(
            channels.docs,
            "gh_run",
            return_value=SimpleNamespace(stdout=json.dumps(data)),
        ) as cli:
            channels.verify_attestation(self.root / "package.tgz", contract, 1)
        command = cli.call_args.args[0]
        for required in (
            "--source-ref",
            "refs/tags/" + TAG,
            "--source-digest",
            SOURCE,
            "--deny-self-hosted-runners",
            "--signer-workflow",
            channels.docs.REPOSITORY + "/.github/workflows/publish-npm.yml",
        ):
            self.assertIn(required, command)
        for location in ("certificate", "statement"):
            changed = copy.deepcopy(data)
            value = changed[0]["verificationResult"]
            if location == "certificate":
                value["signature"]["certificate"]["runInvocationURI"] = (
                    invocation.replace("runs/42", "runs/99")
                )
            else:
                value["statement"]["predicate"]["runDetails"]["metadata"][
                    "invocationId"
                ] = invocation.replace("attempts/1", "attempts/2")
            with (
                patch.object(
                    channels.docs,
                    "gh_run",
                    return_value=SimpleNamespace(stdout=json.dumps(changed)),
                ),
                self.assertRaisesRegex(ValueError, "another run"),
            ):
                channels.verify_attestation(self.root / "package.tgz", contract, 1)

    def test_missing_failed_newer_or_cross_run_python_draft_refuses_npm(self):
        self.jobs = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
            }
            for name in channels.releases.BUILD_JOBS | {"Prepare draft GitHub release"}
        ]
        contract = {
            "tag": TAG,
            "version": VERSION,
            "tag_object": TAG_OBJECT,
            "source_sha": SOURCE,
        }
        assets = {
            f"review_sensei-{VERSION}-py3-none-any.whl": "e" * 64,
            f"review_sensei-{VERSION}.tar.gz": "e" * 64,
            "SHA256SUMS": "e" * 64,
            channels.major.SBOM: "e" * 64,
        }
        context = channels.releases.Context(
            TAG,
            TAG_OBJECT,
            SOURCE,
            41,
            2,
            channels.prepare.released_notes(
                (self.root / "CHANGELOG.md").read_text(), VERSION, "2026-10-20"
            ),
        )
        release = {
            "id": 5,
            "tag_name": TAG,
            "target_commitish": SOURCE,
            "name": "ReviewSensei " + TAG,
            "draft": True,
            "prerelease": False,
            "body": channels.releases.release_body_inventory(context, assets),
        }
        with (
            patch.object(channels.releases, "find_release", return_value=release),
            patch.object(channels.major, "published_assets") as verify,
        ):
            channels.verify_draft(self.root, contract)
            verify.assert_called_once()
            # A failed finalizer is irrelevant to npm; only the latest draft/build prerequisites apply.
            self.jobs.append(
                {
                    "name": "Create GitHub release assets",
                    "run_attempt": 2,
                    "status": "completed",
                    "conclusion": "failure",
                }
            )
            channels.verify_draft(self.root, contract)
            self.jobs.append(
                {
                    "name": "Prepare draft GitHub release",
                    "run_attempt": 2,
                    "status": "completed",
                    "conclusion": "failure",
                }
            )
            with self.assertRaisesRegex(ValueError, "prerequisite"):
                channels.verify_draft(self.root, contract)
        self.jobs.pop()
        foreign = {
            **release,
            "body": release["body"].replace('"run_id":41', '"run_id":99'),
        }
        with (
            patch.object(channels.releases, "find_release", return_value=foreign),
            self.assertRaisesRegex(ValueError, "another Python run"),
        ):
            channels.verify_draft(self.root, contract)
        with (
            patch.object(channels.releases, "find_release", return_value=None),
            self.assertRaisesRegex(ValueError, "draft is required"),
        ):
            channels.verify_draft(self.root, contract)


if __name__ == "__main__":
    unittest.main()
