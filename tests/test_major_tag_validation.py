"""Promotion events and published integrity are simulated; no tag writes/network."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import dispatch_major_tag_validation as dispatch
    import major_tag_gate as gate
    import major_tag_surface as surface
    import qualify_published_cli as installer
finally:
    sys.path.pop(0)

OBJECT, SOURCE, IMMUTABLE = "a" * 40, "b" * 40, "c" * 40
REPO = gate.docs.REPOSITORY
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
WHEEL = b"synthetic wheel, never executed"
DIGEST = hashlib.sha256(WHEEL).hexdigest()


def event(tag="v5", after=OBJECT):
    return {
        "repository": {"full_name": REPO, "default_branch": "main"},
        "ref": "refs/tags/" + tag,
        "after": after,
        "deleted": False,
    }


def context(tag="v5", sha=SOURCE):
    return {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_REF": "refs/tags/" + tag,
        "GITHUB_SHA": sha,
    }


def binding():
    return {
        "schema": 1,
        "repository": REPO,
        "tag": "v" + VERSION,
        "tag_object": IMMUTABLE,
        "source": SOURCE,
        "run_id": 123,
        "assets": {
            f"review_sensei-{VERSION}-py3-none-any.whl": DIGEST,
            f"review_sensei-{VERSION}.tar.gz": "d" * 64,
            "SHA256SUMS": "e" * 64,
            gate.SBOM: "f" * 64,
        },
    }


def release():
    return {
        "id": 17,
        "tag_name": "v" + VERSION,
        "target_commitish": SOURCE,
        "draft": False,
        "prerelease": False,
        "name": "ReviewSensei v" + VERSION,
        "body": "notes\n<!-- reviewsensei-release:" + json.dumps(binding()) + " -->",
    }


def assets():
    return [
        {
            "id": i + 1,
            "name": name,
            "state": "uploaded",
            "size": len(WHEEL) if name.endswith(".whl") else 123,
            "digest": "sha256:" + digest,
        }
        for i, (name, digest) in enumerate(binding()["assets"].items())
    ]


class MajorEventTests(unittest.TestCase):
    def test_creation_and_force_move_bind_the_new_object_and_peeled_sha(self):
        for tag in ["v0", "v5", "v6", "v123"]:
            selected = gate.event_identity(event(tag), "push", context(tag))
            admitted = gate.bind_event(selected, OBJECT, SOURCE)
            self.assertEqual(
                (admitted["tag_object"], admitted["source_sha"]), (OBJECT, SOURCE)
            )
            self.assertEqual(admitted["event_binding"], "tag-object")

    def test_glob_overmatches_immutable_prerelease_case_or_non_numeric_tags(self):
        for tag in [
            "v0.6.21",
            "v1.2.3rc1",
            "v5-rc1",
            "v05",
            "v5x",
            "V5",
            "v",
            "v5/other",
        ]:
            self.assertIsNone(gate.event_identity(event(tag), "push", context(tag)))

    def test_deletion_branch_and_foreign_repository_are_not_admitted(self):
        deleted = {**event(), "deleted": True}
        self.assertIsNone(gate.event_identity(deleted, "push", context()))
        branch = {**event(), "ref": "refs/heads/v5"}
        self.assertIsNone(gate.event_identity(branch, "push", context()))
        for invalid in [
            {**event(), "repository": {"full_name": "fork/repo"}},
            {**event(), "after": "bad"},
        ]:
            with self.assertRaises(ValueError):
                gate.event_identity(invalid, "push", context())

    def test_event_and_current_tag_must_not_resolve_a_later_head(self):
        selected = gate.event_identity(event(), "push", context())
        for obj, commit in [("d" * 40, "e" * 40), (OBJECT, "e" * 40)]:
            with self.assertRaises(ValueError):
                gate.bind_event(selected, obj, commit)

    def test_peeled_push_is_explicitly_source_bound(self):
        selected = gate.event_identity(event(after=SOURCE), "push", context())
        self.assertEqual(
            gate.bind_event(selected, OBJECT, SOURCE)["event_binding"], "peeled-source"
        )

    def test_dispatch_binds_exact_object_on_default_branch(self):
        e = {
            "repository": event()["repository"],
            "inputs": {"channel": "v5", "tag_object": OBJECT, "source_sha": SOURCE},
        }
        env = {**context(), "GITHUB_REF": "refs/heads/main"}
        selected = gate.event_identity(e, "workflow_dispatch", env)
        self.assertEqual(
            gate.bind_event(selected, OBJECT, SOURCE)["event_binding"], "tag-object"
        )
        with self.assertRaises(ValueError):
            gate.bind_event(selected, "e" * 40, SOURCE)
        with self.assertRaises(ValueError):
            gate.event_identity(e, "workflow_dispatch", context())
        for name in ["pull_request", "create", "repository_dispatch"]:
            with self.assertRaises(ValueError):
                gate.event_identity(e, name, env)


class MajorIntegrityTests(unittest.TestCase):
    def signed_api(self, path):
        if path == "git/ref/tags/v5":
            return {"ref": "refs/tags/v5", "object": {"type": "tag", "sha": OBJECT}}
        self.assertEqual(path, "git/tags/" + OBJECT)
        return {
            "sha": OBJECT,
            "tag": "v5",
            "object": {"type": "commit", "sha": SOURCE},
            "verification": {"verified": True},
        }

    def test_signature_and_annotated_commit_identity(self):
        with patch.object(gate.docs, "api", side_effect=self.signed_api):
            self.assertEqual(gate.signed_major("v5"), (OBJECT, SOURCE))
        for changes in [
            {"sha": "d" * 40},
            {"tag": "v6"},
            {"object": {"type": "tag", "sha": SOURCE}},
            {"verification": {"verified": False}},
            {"verification": {"verified": "true"}},
        ]:

            def api(path):
                value = self.signed_api(path)
                return {**value, **changes} if path.startswith("git/tags/") else value

            with (
                patch.object(gate.docs, "api", side_effect=api),
                self.assertRaises(ValueError),
            ):
                gate.signed_major("v5")
        with (
            patch.object(
                gate.docs,
                "api",
                return_value={
                    "ref": "refs/tags/v5",
                    "object": {"type": "commit", "sha": SOURCE},
                },
            ),
            self.assertRaises(ValueError),
        ):
            gate.signed_major("v5")

    def test_source_files_are_bounded_data(self):
        raw = b"[project]\nversion='0.6.21'\n"
        value = {
            "type": "file",
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode(),
        }
        with patch.object(gate.docs, "api", return_value=value):
            self.assertEqual(gate.source_file(SOURCE, "pyproject.toml"), raw)
        for field, changed in [
            ("type", "symlink"),
            ("encoding", "none"),
            ("size", 524289),
            ("size", 1),
            ("content", "!notbase64!"),
        ]:
            with (
                patch.object(gate.docs, "api", return_value={**value, field: changed}),
                self.assertRaises(Exception),
            ):
                gate.source_file(SOURCE, "pyproject.toml")

    def test_release_binds_exact_signed_stable_source_and_inventory(self):
        self.assertEqual(
            gate.release_binding(release(), VERSION, IMMUTABLE, SOURCE), binding()
        )
        for field, changed in [
            ("draft", True),
            ("prerelease", True),
            ("target_commitish", "main"),
            ("tag_name", "v0.6.20"),
            ("body", "no binding"),
            ("id", True),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.release_binding(
                    {**release(), field: changed}, VERSION, IMMUTABLE, SOURCE
                )
        for field, changed in [
            ("source", "d" * 40),
            ("tag_object", "e" * 40),
            ("repository", "fork/repo"),
            ("run_id", True),
            ("assets", {}),
            ("schema", 2),
            ("schema", True),
        ]:
            body = (
                "<!-- reviewsensei-release:"
                + json.dumps({**binding(), field: changed})
                + " -->"
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.release_binding(
                    {**release(), "body": body}, VERSION, IMMUTABLE, SOURCE
                )

    def test_missing_duplicate_or_changed_asset_digest_fails_closed(self):
        with patch.object(gate.docs, "api_pages", return_value=assets()):
            self.assertEqual(len(gate.published_assets(17, binding())), 4)
        for bad in [
            assets()[:-1],
            assets() + [assets()[0]],
            [{**a, "digest": "sha256:" + "0" * 64} for a in assets()],
            [{**a, "state": "new"} for a in assets()],
        ]:
            with (
                patch.object(gate.docs, "api_pages", return_value=bad),
                self.assertRaises(ValueError),
            ):
                gate.published_assets(17, binding())

    def test_attestation_checks_source_ref_workflow_digest_without_running_wheel(self):
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(
                command, 0, WHEEL if command[1] == "api" else b"[]", b""
            )

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(gate.subprocess, "run", side_effect=run),
        ):
            self.assertEqual(
                gate.verify_wheel(assets()[0], binding(), VERSION, Path(temp)), DIGEST
            )
        self.assertEqual(len(commands), 2)
        for arg in [
            "--source-digest",
            SOURCE,
            "--source-ref",
            "refs/tags/v" + VERSION,
            "--deny-self-hosted-runners",
            "--signer-workflow",
        ]:
            self.assertIn(arg, commands[1])
        self.assertTrue(all(command[0] == "gh" for command in commands))
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(
                gate.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, b"wrong", b""),
            ),
            self.assertRaises(ValueError),
        ):
            gate.verify_wheel(assets()[0], binding(), VERSION, Path(temp))

    def test_current_object_source_release_binding_and_assets_are_rechecked(self):
        receipt = {
            "channel": "v5",
            "tag_object": OBJECT,
            "source_sha": SOURCE,
            "release_tag": "v" + VERSION,
            "release_tag_object": IMMUTABLE,
            "version": VERSION,
            "release_binding": binding(),
        }
        with (
            patch.object(gate, "signed_major", return_value=(OBJECT, SOURCE)),
            patch.object(gate.docs, "tag_identity", return_value=(IMMUTABLE, SOURCE)),
            patch.object(gate.docs, "api", return_value=release()),
            patch.object(gate.docs, "api_pages", return_value=assets()),
        ):
            gate.assert_current(receipt)
            with (
                patch.object(gate, "signed_major", return_value=("d" * 40, SOURCE)),
                self.assertRaises(ValueError),
            ):
                gate.assert_current(receipt)
            with (
                patch.object(gate.docs, "api_pages", return_value=assets()[:-1]),
                self.assertRaises(ValueError),
            ):
                gate.assert_current(receipt)

    def test_installer_rejects_digest_mismatch_before_any_package_install(self):
        installer.require_wheel_digest(DIGEST, DIGEST)
        installer.require_wheel_digest(DIGEST, None)
        with self.assertRaises(ValueError):
            installer.require_wheel_digest(DIGEST, "a" * 64)
        for value in ["", "A" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n"]:
            with self.assertRaises(Exception):
                installer.wheel_sha256(value)
        text = (ROOT / "scripts/qualify_published_cli.py").read_text()
        self.assertLess(
            text.index('require_wheel_digest(report["wheel"]'), text.index('"install",')
        )


class SurfaceTests(unittest.TestCase):
    def test_real_promoted_data_contract(self):
        data = surface.validate_surface(ROOT)
        self.assertEqual(data["installation_lanes"], 4)
        self.assertIn("not a proven runtime defect", data["diagnostics"][0])

    def test_caller_context_substitution_is_not_a_callee_identity_fix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / ".github/workflows"
            folder.mkdir(parents=True)
            for name in ["review-sensei-run.yml", "review-sensei-review.yml"]:
                text = (ROOT / ".github/workflows" / name).read_text()
                # Synthetic parser fixture only. This is not a product fix or
                # proof github.workflow_* identifies a called reusable workflow.
                text = text.replace("job.workflow_ref", "github.workflow_ref").replace(
                    "job.workflow_sha", "github.workflow_sha"
                )
                (folder / name).write_text(text)
            with self.assertRaisesRegex(ValueError, "callee identity contract changed"):
                surface.validate_surface(root)

    def test_yaml_duplicate_alias_and_executable_tags_are_rejected(self):
        for text in [
            "jobs: {}\njobs: {}\n",
            "jobs: &jobs {}\ncopy: *jobs\n",
            "x: !!python/object/apply:os.system [echo]\n",
        ]:
            with self.assertRaises(Exception):
                surface.workflow_data(text)
        self.assertIn("on", surface.workflow_data("on:\n  workflow_call: {}\n"))

    def test_incompatible_install_secret_permission_and_metadata_paths_fail(self):
        original = (ROOT / ".github/workflows/review-sensei-run.yml").read_text()
        caller = (ROOT / ".github/workflows/review-sensei-review.yml").read_text()
        for old, new in [
            ('"review-sensei==$expected_version"', '"review-sensei"'),
            ("GITHUB_TOKEN: ${{ github.token }}", 'GITHUB_TOKEN: ""'),
            ("contents: read", "contents: write"),
            (
                "required: false\n      OPENROUTER_API_KEY",
                "required: true\n      OPENROUTER_API_KEY",
            ),
            (
                "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
                "actions/setup-python@main",
            ),
        ]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                folder = root / ".github/workflows"
                folder.mkdir(parents=True)
                (folder / "review-sensei-run.yml").write_text(
                    original.replace(old, new)
                )
                (folder / "review-sensei-review.yml").write_text(caller)
                with self.subTest(old=old), self.assertRaises(ValueError):
                    surface.validate_surface(root)

    def test_workflow_pins_validator_and_digest_and_propagates_failures(self):
        text = (ROOT / ".github/workflows/major-tag-validation.yml").read_text()
        data = surface.workflow_data(text)
        self.assertEqual(data["permissions"], {"contents": "read"})
        self.assertEqual(data["concurrency"]["cancel-in-progress"], True)
        self.assertIn(
            "format('refs/tags/{0}', inputs.channel)", data["concurrency"]["group"]
        )
        self.assertEqual(
            data["jobs"]["gate"]["permissions"],
            {"contents": "read", "actions": "read", "attestations": "read"},
        )
        self.assertEqual(
            data["jobs"]["qualify"]["if"], "needs.gate.outputs.eligible == 'true'"
        )
        self.assertIn("--expected-wheel-sha256", text)
        self.assertIn("ref: ${{ needs.gate.outputs.validator_sha }}", text)
        self.assertIn('test "$CLI_RESULT" = success', text)
        self.assertIn("if: always()", text)
        self.assertEqual(surface.check_workflow_text(text), [])

    def test_ci_executes_installed_harness_with_fixed_published_baseline(self):
        text = (ROOT / ".github/workflows/major-tag-validation-ci.yml").read_text()
        data = surface.workflow_data(text)
        self.assertEqual(data["permissions"], {"contents": "read"})
        job = data["jobs"]["installed-cli"]
        self.assertEqual(
            job["strategy"]["matrix"]["os"], ["ubuntu-latest", "macos-latest"]
        )
        self.assertIn("--version 0.6.21", text)
        self.assertIn(
            "--expected-wheel-sha256 "
            "58f9ed48e8df2434c2136ba9e57c260e606fd550e396cd422ee2efd1d4d4ef31",
            text,
        )
        self.assertNotIn("github.token", text)
        self.assertIn("if: always()", text)
        self.assertEqual(surface.check_workflow_text(text), [])


class GateProcessTests(unittest.TestCase):
    def test_dispatch_bridge_preserves_object_and_existing_auth_without_tag_writes(
        self,
    ):
        with patch.object(dispatch, "signed_major", return_value=(OBJECT, SOURCE)):
            command = dispatch.command("v5", OBJECT, SOURCE)
        self.assertEqual(
            command[:4], ["gh", "workflow", "run", "major-tag-validation.yml"]
        )
        self.assertIn("tag_object=" + OBJECT, command)
        self.assertIn("source_sha=" + SOURCE, command)
        self.assertNotIn("push", command)
        self.assertNotIn("token", " ".join(command))
        with (
            patch.object(dispatch, "signed_major", return_value=("d" * 40, SOURCE)),
            self.assertRaises(ValueError),
        ):
            dispatch.command("v5", OBJECT, SOURCE)

    def test_gate_failure_never_prints_private_subprocess_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event_file = root / "event.json"
            event_file.write_text(json.dumps(event()))
            env = {
                **context(),
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": str(event_file),
            }
            failure = subprocess.CalledProcessError(
                1,
                ["gh", "api"],
                output="synthetic-secret-token",
                stderr="synthetic-secret-token",
            )
            stream = io.StringIO()
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(sys, "argv", ["gate", "--output", str(root / "report")]),
                patch.object(gate, "qualify", side_effect=failure),
                redirect_stdout(stream),
            ):
                self.assertTrue(gate.main())
            self.assertNotIn("synthetic-secret-token", stream.getvalue())
            self.assertEqual(
                json.loads((root / "report/gate.json").read_text())["status"], "ERROR"
            )

    def test_recheck_propagates_failed_cancelled_skipped_cli_lanes(self):
        for outcome in ["success", "failure", "cancelled", "skipped"]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                receipt = root / "accepted.json"
                receipt.write_text('{"status":"PASS"}')
                with (
                    patch.dict(os.environ, {"CLI_RESULT": outcome}, clear=True),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "gate",
                            "--recheck",
                            str(receipt),
                            "--output",
                            str(root / "final"),
                        ],
                    ),
                    patch.object(gate, "assert_current"),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(gate.main(), outcome != "success")
                self.assertEqual(
                    json.loads((root / "final/gate.json").read_text())[
                        "cli_lanes_result"
                    ],
                    outcome,
                )


class FullGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reads = []
        self.file_mutations = {}
        self.run = {
            "id": 123,
            "head_sha": SOURCE,
            "head_branch": "v" + VERSION,
            "path": ".github/workflows/release.yml",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": REPO},
            "head_repository": {"full_name": REPO},
            "pull_requests": [],
            "run_attempt": 2,
        }
        self.jobs = [
            {"name": name, "run_attempt": 2, "conclusion": "success"}
            for name in gate.docs.REQUIRED_JOBS
        ]
        self.enterContext(patch.object(gate.docs, "api", side_effect=self.api))
        self.enterContext(patch.object(gate.docs, "api_pages", side_effect=self.pages))
        self.enterContext(
            patch.object(gate, "signed_major", return_value=(OBJECT, SOURCE))
        )
        self.enterContext(
            patch.object(gate.docs, "tag_identity", return_value=(IMMUTABLE, SOURCE))
        )
        self.wheel = self.enterContext(
            patch.object(gate, "verify_wheel", return_value=DIGEST)
        )
        self.selected = gate.event_identity(event(), "push", context())

    def api(self, path):
        self.reads.append(path)
        if path.startswith("contents/"):
            relative = path.removeprefix("contents/").split("?", 1)[0]
            self.assertTrue(path.endswith("?ref=" + SOURCE))
            raw = self.file_mutations.get(relative, (ROOT / relative).read_bytes())
            return {
                "type": "file",
                "encoding": "base64",
                "size": len(raw),
                "content": base64.b64encode(raw).decode(),
            }
        if path == "releases/tags/v" + VERSION:
            return release()
        if path == "actions/runs/123":
            return self.run
        if path == f"compare/{SOURCE}...main":
            return {"status": "ahead"}
        raise AssertionError(path)

    def pages(self, path, key=None):
        if path == "actions/runs/123/jobs?filter=all":
            self.assertEqual(key, "jobs")
            return self.jobs
        if path == "releases/17/assets":
            return assets()
        raise AssertionError(path)

    def test_full_gate_reads_only_frozen_source_and_attested_exact_release(self):
        receipt = gate.qualify(self.selected, self.root / "accepted")
        self.assertEqual(
            (receipt["status"], receipt["version"], receipt["wheel_sha256"]),
            ("PASS", VERSION, DIGEST),
        )
        self.assertEqual(receipt["release_binding"], binding())
        self.assertTrue(receipt["surface"]["data_only"])
        self.assertEqual(len(receipt["source_data_sha256"]), 10)
        self.wheel.assert_called_once()

    def test_mixed_manifest_or_prerelease_refuses_before_wheel(self):
        for raw in [
            b'[project]\nname="review-sensei"\nversion="0.6.20"\n',
            b'[project]\nname="review-sensei"\nversion="0.6.22rc1"\n',
        ]:
            self.file_mutations["pyproject.toml"] = raw
            with self.assertRaises(ValueError):
                gate.qualify(self.selected, self.root / hashlib.sha256(raw).hexdigest())
        self.wheel.assert_not_called()

    def test_immutable_target_or_failed_registry_job_refuses_before_installation(self):
        with (
            patch.object(gate.docs, "tag_identity", return_value=(IMMUTABLE, "d" * 40)),
            self.assertRaises(ValueError),
        ):
            gate.qualify(self.selected, self.root / "different-source")
        self.jobs[0]["conclusion"] = "failure"
        with self.assertRaises(ValueError):
            gate.qualify(self.selected, self.root / "failed-job")
        self.wheel.assert_not_called()

    def test_force_move_during_validation_fails_even_if_source_unchanged(self):
        with (
            patch.object(
                gate, "signed_major", side_effect=[(OBJECT, SOURCE), ("d" * 40, SOURCE)]
            ),
            self.assertRaises(ValueError),
        ):
            gate.qualify(self.selected, self.root / "raced")

    def test_actual_released_surface_does_not_turn_static_lint_warning_into_runtime_failure(
        self,
    ):
        self.file_mutations.clear()
        receipt = gate.qualify(self.selected, self.root / "incompatible-surface")
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["surface"]["status"], "PASS")
        self.assertEqual(receipt["version"], VERSION)
        self.assertIn(
            "not a proven runtime defect", receipt["surface"]["diagnostics"][0]
        )

    def test_http_read_or_attestation_failure_never_creates_passing_receipt(self):
        with (
            patch.object(
                gate.docs, "api", side_effect=subprocess.CalledProcessError(1, ["gh"])
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            gate.qualify(self.selected, self.root / "403")
        with (
            patch.object(
                gate,
                "verify_wheel",
                side_effect=subprocess.CalledProcessError(1, ["gh", "attestation"]),
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            gate.qualify(self.selected, self.root / "unattested")


if __name__ == "__main__":
    unittest.main()
