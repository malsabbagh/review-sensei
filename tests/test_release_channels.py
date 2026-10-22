from __future__ import annotations

import copy
import hashlib
import importlib
import io
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
VERSION = "1.2.3"
OTHER_VERSION = "1.2.4"
RELEASE_DATE = "2026-01-02"
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
    def test_missing_channel_is_a_usage_error_before_any_release_reads(self):
        for command in ("qualify", "record"):
            with (
                self.subTest(command=command),
                patch.object(sys, "argv", ["release_channels.py", command]),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
                patch.object(channels, "event_contract") as contract,
            ):
                with self.assertRaises(SystemExit) as error:
                    channels.main()
                self.assertEqual(error.exception.code, 2)
                self.assertIn("--channel is required", stderr.getvalue())
                contract.assert_not_called()

    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        (self.root / "pyproject.toml").write_text(f'[project]\nversion = "{VERSION}"\n')
        (self.root / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {VERSION} - {RELEASE_DATE}\n\n- Fixture release.\n"
        )
        shutil.copytree(ROOT / "packages/npm", self.root / "packages/npm")
        for path in (self.root / "packages/npm").rglob("package.json"):
            manifest = json.loads(path.read_text())
            manifest["version"] = VERSION
            if "optionalDependencies" in manifest:
                manifest["optionalDependencies"] = {
                    name: VERSION for name in manifest["optionalDependencies"]
                }
            path.write_text(json.dumps(manifest))
        receipt = self.root / channels.prepare.RECEIPT
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"release_date": RELEASE_DATE}))
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

    def test_qualifier_cli_emits_all_outputs_from_workspace_or_source_checkout(self):
        other = Path(self.enterContext(tempfile.TemporaryDirectory())) / "source"
        shutil.copytree(self.root, other)
        output = self.root / "outputs.txt"
        for root in (self.root, other):
            output.write_text("")
            with (
                self.subTest(root=root),
                patch.dict(
                    os.environ,
                    {
                        **self.environment(),
                        "GITHUB_OUTPUT": str(output),
                        "EXPECTED_TAG_OBJECT": TAG_OBJECT,
                    },
                    clear=True,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "release_channels.py",
                        "qualify",
                        "--channel",
                        "python",
                        "--root",
                        str(root),
                    ],
                ),
            ):
                self.assertEqual(channels.main(), 0)
                self.assertEqual(
                    dict(
                        line.split("=", 1) for line in output.read_text().splitlines()
                    ),
                    {
                        "tag_object": TAG_OBJECT,
                        "source_sha": SOURCE,
                        "version": VERSION,
                    },
                )
                self.prepared.assert_called_with(root, TAG)
        for expected in ("", "c" * 40):
            output.write_text("")
            with (
                self.subTest(expected=expected),
                patch.dict(
                    os.environ,
                    {
                        **self.environment(),
                        "GITHUB_OUTPUT": str(output),
                        "EXPECTED_TAG_OBJECT": expected,
                    },
                    clear=True,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "release_channels.py",
                        "qualify",
                        "--channel",
                        "python",
                        "--root",
                        str(other),
                    ],
                ),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                self.assertEqual(channels.main(), 1)
                self.assertEqual(output.read_text(), "")

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
            ("head_branch", "v" + OTHER_VERSION),
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
        for manifest in (self.root / "packages/npm").rglob("package.json"):
            original_manifest = manifest.read_text()
            metadata = json.loads(original_manifest)
            metadata["version"] = OTHER_VERSION
            manifest.write_text(json.dumps(metadata))
            with (
                self.subTest(manifest=manifest.relative_to(self.root)),
                patch.dict(os.environ, self.environment(), clear=True),
                self.assertRaisesRegex(ValueError, "version"),
            ):
                channels.qualify("python", self.root)
            manifest.write_text(original_manifest)

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

    def test_channel_result_requires_all_jobs_and_preserves_publisher_failures(self):
        for channel in channels.WORKFLOWS:
            self.jobs = [
                {
                    "id": index,
                    "name": name,
                    "run_attempt": 2,
                    "status": "completed",
                    "conclusion": "success",
                }
                for index, name in enumerate(sorted(channels.REQUIRED_JOBS[channel]), 1)
            ]
            self.jobs.append(
                {
                    "name": f"Record independent {channel} channel result",
                    "run_attempt": 2,
                    "status": "in_progress",
                    "conclusion": None,
                }
            )
            with (
                self.subTest(channel=channel),
                patch.dict(os.environ, self.environment(channel), clear=True),
                patch.object(channels.docs, "api", return_value=run(channel)),
            ):
                self.assertTrue(channels.record(channel)["successful"])
                publisher = next(j for j in self.jobs if "Publish" in j["name"])
                for conclusion in ("failure", "cancelled", "skipped"):
                    publisher["conclusion"] = conclusion
                    receipt = channels.record(channel)
                    self.assertFalse(receipt["successful"])
                    self.assertIn(publisher, receipt["jobs"])
                publisher["conclusion"] = "success"
                self.jobs.append(
                    {
                        "name": "Additional channel check",
                        "run_attempt": 2,
                        "status": "completed",
                        "conclusion": "failure",
                    }
                )
                self.assertFalse(channels.record(channel)["successful"])

    def test_npm_channel_result_requires_every_published_consumer_verification(self):
        targets = (
            "darwin-arm64",
            "darwin-x64",
            "linux-arm64-gnu",
            "linux-x64-gnu",
            "win32-x64",
        )
        job_names = (
            "Qualify signed prepared source and exact-head CI",
            "Assemble and validate npm package set",
            "Publish npm packages with Trusted Publishing",
            *(f"Native standalone ({target})" for target in targets),
            *(f"Verify published npm consumer ({target})" for target in targets),
        )
        successful_jobs = [
            {
                "id": index,
                "name": name,
                "run_attempt": 2,
                "status": "completed",
                "conclusion": "success",
            }
            for index, name in enumerate(job_names, 1)
        ]
        self.jobs = copy.deepcopy(successful_jobs)
        with (
            patch.dict(os.environ, self.environment("npm"), clear=True),
            patch.object(channels.docs, "api", return_value=run("npm")),
        ):
            self.assertTrue(channels.record("npm")["successful"])
            for target in targets:
                name = f"Verify published npm consumer ({target})"
                for outcome in ("missing", "failure", "skipped", "in_progress"):
                    with self.subTest(target=target, outcome=outcome):
                        self.jobs = copy.deepcopy(successful_jobs)
                        verification = next(j for j in self.jobs if j["name"] == name)
                        if outcome == "missing":
                            self.jobs.remove(verification)
                        elif outcome == "in_progress":
                            verification.update(status="in_progress", conclusion=None)
                        else:
                            verification["conclusion"] = outcome
                        receipt = channels.record("npm")
                        self.assertFalse(receipt["successful"])
                        if outcome != "missing":
                            self.assertIn(verification, receipt["jobs"])

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

    def test_npm_retry_reuses_original_attempt_bundle_with_published_python_gate(
        self,
    ):
        bundle = self.npm_bundle()
        with (
            patch.dict(os.environ, self.npm_env(), clear=True),
            patch.object(channels, "verify_attestation") as attest,
            patch.object(channels, "verify_python_release") as publication,
        ):
            channels.verify_npm(self.root, bundle)
        self.assertEqual(attest.call_count, 7)
        self.assertTrue(all(c.args[2] == 1 for c in attest.call_args_list))
        publication.assert_called_once()

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
            {"version": OTHER_VERSION, "source_sha": SOURCE},
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

    def published_python_fixture(self):
        self.runs["python"].update(status="completed", conclusion="success")
        self.jobs = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
            }
            for name in channels.releases.BUILD_JOBS
            | channels.releases.PUBLISH_JOBS
            | {"Create GitHub release assets"}
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
                (self.root / "CHANGELOG.md").read_text(), VERSION, RELEASE_DATE
            ),
        )
        return contract, {
            "id": 5,
            "tag_name": TAG,
            "target_commitish": SOURCE,
            "name": "ReviewSensei " + TAG,
            "draft": False,
            "prerelease": False,
            "body": channels.releases.release_body_inventory(context, assets),
        }

    def test_npm_requires_published_python_release_and_exact_source_notes_run_assets(
        self,
    ):
        contract, release = self.published_python_fixture()
        with (
            patch.object(channels.releases, "find_release", return_value=release),
            patch.object(channels.major, "published_assets") as verify,
        ):
            channels.verify_python_release(self.root, contract)
            verify.assert_called_once()
        for key, value in (
            ("draft", True),
            ("prerelease", True),
            ("target_commitish", "c" * 40),
            ("body", release["body"] + "edited"),
            ("body", release["body"].replace('"run_id":41', '"run_id":99')),
        ):
            with (
                self.subTest(key=key, value=value),
                patch.object(
                    channels.releases,
                    "find_release",
                    return_value={**release, key: value},
                ),
                patch.object(channels.major, "published_assets"),
                self.assertRaises(ValueError),
            ):
                channels.verify_python_release(self.root, contract)
        with (
            patch.object(channels.releases, "find_release", return_value=None),
            self.assertRaises(ValueError),
        ):
            channels.verify_python_release(self.root, contract)
        with (
            patch.object(channels.releases, "find_release", return_value=release),
            patch.object(
                channels.major,
                "published_assets",
                side_effect=ValueError("asset digest differs"),
            ),
            self.assertRaisesRegex(ValueError, "asset digest"),
        ):
            channels.verify_python_release(self.root, contract)

    def test_npm_requires_latest_python_publisher_and_finalizer_success(self):
        contract, release = self.published_python_fixture()
        original = list(self.jobs)
        for name in (
            "Publish to PyPI with Trusted Publishing",
            "Create GitHub release assets",
        ):
            for status, conclusion in (("completed", "failure"), ("waiting", None)):
                self.jobs = original + [
                    {
                        "name": name,
                        "status": status,
                        "conclusion": conclusion,
                        "run_attempt": 2,
                    }
                ]
                with (
                    self.subTest(name=name, status=status),
                    patch.object(
                        channels.releases, "find_release", return_value=release
                    ),
                    patch.object(channels.major, "published_assets"),
                    self.assertRaisesRegex(ValueError, "prerequisite"),
                ):
                    channels.verify_python_release(self.root, contract)

    def test_public_python_release_with_non_successful_run_is_refused(self):
        contract, release = self.published_python_fixture()
        for status, conclusion in (
            ("in_progress", None),
            ("completed", "failure"),
            ("completed", "cancelled"),
        ):
            self.runs["python"].update(status=status, conclusion=conclusion)
            with (
                self.subTest(status=status, conclusion=conclusion),
                patch.object(channels.releases, "find_release", return_value=release),
                patch.object(channels.major, "published_assets") as assets,
                self.assertRaisesRegex(ValueError, "terminal success"),
            ):
                channels.verify_python_release(self.root, contract)
            assets.assert_not_called()

    def test_npm_retry_after_python_finalization_reuses_original_attested_bundle(self):
        bundle = self.npm_bundle()
        contract, release = self.published_python_fixture()
        original = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in bundle.iterdir()
        }
        with (
            patch.dict(os.environ, self.npm_env(), clear=True),
            patch.object(channels, "verify_attestation") as attest,
            patch.object(channels.major, "published_assets"),
            patch.object(
                channels.releases, "find_release", side_effect=[None, release]
            ),
        ):
            with self.assertRaises(ValueError):
                channels.verify_npm(self.root, bundle)
            self.runs["python"]["run_attempt"] = 3
            self.jobs.append(
                {
                    "name": "Create GitHub release assets",
                    "run_attempt": 3,
                    "status": "completed",
                    "conclusion": "success",
                }
            )
            channels.verify_npm(self.root, bundle)
        self.assertEqual(attest.call_count, 14)
        self.assertTrue(all(call.args[2] == 1 for call in attest.call_args_list))
        self.assertEqual(
            original,
            {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in bundle.iterdir()
            },
        )


if __name__ == "__main__":
    unittest.main()
