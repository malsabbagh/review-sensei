"""Promotion events and published integrity are simulated; no tag writes/network."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
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


def wheel_bytes(version=VERSION, name="review-sensei", metadata=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            f"review_sensei-{VERSION}.dist-info/METADATA",
            metadata
            if metadata is not None
            else f"Name: {name}\nVersion: {version}\n\n",
        )
    return stream.getvalue()


WHEEL = wheel_bytes()
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
        selected = gate.event_identity(
            event(after=SOURCE), "push", {**context(), "GITHUB_SHA": SOURCE}
        )
        self.assertEqual(
            gate.bind_event(selected, OBJECT, SOURCE)["event_binding"], "peeled-source"
        )
        self.assertFalse(
            gate.bind_event(selected, OBJECT, SOURCE)["event_object_bound"]
        )
        selected["event_sha"] = OBJECT
        self.assertTrue(gate.bind_event(selected, OBJECT, SOURCE)["event_object_bound"])
        self.assertEqual(
            gate.bind_event(selected, OBJECT, SOURCE)["event_binding"], "tag-object"
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
    def test_first_page_parser_rejects_missing_malformed_and_oversized_headers(self):
        for raw in [
            "[]",
            "HTTP/2.0 403 Forbidden\n\n[]",
            "HTTP/2.0 200 OK\nbad\n\n[]",
            "x" * (512 * 1024 + 1),
        ]:
            with (
                patch.object(
                    gate.docs,
                    "gh_run",
                    return_value=subprocess.CompletedProcess([], 0, raw, ""),
                ),
                self.assertRaises(ValueError),
            ):
                gate.docs.api_first_page("releases/17/assets?per_page=5&page=1")

    def test_asset_inventory_refuses_advertised_next_page_even_with_four_items(self):
        with (
            patch.object(
                gate.docs, "api_first_page", return_value=(assets(), True)
            ) as read,
            self.assertRaises(ValueError),
        ):
            gate.published_assets(17, binding())
        read.assert_called_once()

    def test_first_page_parser_requires_headers_and_reports_next_link(self):
        response = (
            'HTTP/2.0 200 OK\r\nLink: <https://github.com/next>; rel="next"\r\n\r\n[]'
        )
        with patch.object(
            gate.docs,
            "gh_run",
            return_value=subprocess.CompletedProcess([], 0, response, ""),
        ):
            self.assertEqual(
                gate.docs.api_first_page("releases/17/assets?per_page=5&page=1"),
                ([], True),
            )

    def test_metadata_budget_edges_and_duplicate_or_invalid_archive(self):
        prefix = f"Name: review-sensei\nVersion: {VERSION}\n\n".encode()
        for size in [gate.MAX_WHEEL_METADATA - 1, gate.MAX_WHEEL_METADATA]:
            gate.validate_wheel_metadata(
                wheel_bytes(metadata=prefix + b"x" * (size - len(prefix))), VERSION
            )
        for raw in [
            b"not a wheel",
            wheel_bytes(metadata=b""),
            wheel_bytes(
                metadata=prefix + b"x" * (gate.MAX_WHEEL_METADATA + 1 - len(prefix))
            ),
        ]:
            with self.assertRaises(ValueError):
                gate.validate_wheel_metadata(raw, VERSION)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(f"review_sensei-{VERSION}.dist-info/METADATA", prefix)
            archive.writestr("other.dist-info/METADATA", prefix)
        with self.assertRaises(ValueError):
            gate.validate_wheel_metadata(stream.getvalue(), VERSION)

    def test_real_failed_child_process_is_noninteractive_host_fixed_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            child = Path(temp) / "child.py"
            child.write_text(
                "import os,sys\nassert os.environ['GH_HOST']=='github.com'\nassert 'GH_DEBUG' not in os.environ\nassert os.environ['GH_PROMPT_DISABLED']=='1'\nprint('synthetic-secret-token',file=sys.stderr)\nsys.exit(4)\n"
            )
            with (
                patch.dict(os.environ, {"GH_HOST": "wrong.invalid", "GH_DEBUG": "api"}),
                self.assertRaisesRegex(ValueError, "dispatch.*exit 4") as caught,
            ):
                gate.docs.gh_run(
                    [sys.executable, str(child)], "GitHub major validation dispatch"
                )
            self.assertNotIn("synthetic-secret-token", str(caught.exception))

    def test_missing_signature_verification_fails_closed(self):
        def api(path):
            value = self.signed_api(path)
            if path.startswith("git/tags/"):
                value.pop("verification")
            return value

        with (
            patch.object(gate.docs, "api", side_effect=api),
            self.assertRaisesRegex(ValueError, "not verified"),
        ):
            gate.signed_major("v5")

    def test_github_metadata_host_and_environment_are_fixed(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GH_HOST": "wrong.invalid",
                    "GH_DEBUG": "api",
                    "GH_TOKEN": "synthetic",
                },
            ),
            patch.object(
                gate.docs.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "{}", ""),
            ) as run,
        ):
            gate.docs.api("git/ref/tags/v5")
        args, kwargs = run.call_args
        self.assertIn("--hostname", args[0])
        self.assertIn("github.com", args[0])
        self.assertEqual(kwargs["env"]["GH_HOST"], "github.com")
        self.assertNotIn("GH_DEBUG", kwargs["env"])
        self.assertEqual(kwargs["env"]["GH_TOKEN"], "synthetic")
        self.assertEqual(kwargs["timeout"], 60)

    def test_asset_inventory_refuses_fifth_item_after_one_request(self):
        with patch.object(
            gate.docs, "api_first_page", return_value=(assets() + [assets()[0]], False)
        ) as api:
            with self.assertRaises(ValueError):
                gate.published_assets(17, binding())
        api.assert_called_once_with("releases/17/assets?per_page=5&page=1")

    def test_non_object_release_binding_is_explicitly_rejected(self):
        for value in [[], 1, "text", None]:
            body = "<!-- reviewsensei-release:" + json.dumps(value) + " -->"
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "JSON object"),
            ):
                gate.release_binding(
                    {**release(), "body": body}, VERSION, IMMUTABLE, SOURCE
                )

    def test_valid_digest_with_wrong_wheel_metadata_refuses_before_attestation(self):
        for raw in [
            wheel_bytes("0.0.1"),
            wheel_bytes(name="other"),
            wheel_bytes(
                metadata="Name: review-sensei\nVersion: "
                + VERSION
                + "\nVersion: 0.0.1\n"
            ),
            wheel_bytes(metadata=b"x" * 65537),
        ]:
            digest = hashlib.sha256(raw).hexdigest()
            bound = binding()
            bound["assets"][f"review_sensei-{VERSION}-py3-none-any.whl"] = digest
            with (
                tempfile.TemporaryDirectory() as temp,
                patch.object(
                    gate.docs.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, raw, b""),
                ) as run,
            ):
                with self.assertRaisesRegex(ValueError, "wheel metadata"):
                    gate.verify_wheel(
                        {**assets()[0], "size": len(raw)}, bound, VERSION, Path(temp)
                    )
                self.assertEqual(run.call_count, 1)

    def test_attestation_nonzero_and_timeout_have_safe_stage_diagnostics(self):
        for failure, expected in [
            (
                subprocess.CalledProcessError(
                    4, ["gh"], stderr=b"synthetic-secret-token"
                ),
                "attestation.*exit 4",
            ),
            (
                subprocess.TimeoutExpired(["gh"], 60, stderr=b"synthetic-secret-token"),
                "attestation.*timed out",
            ),
        ]:
            with (
                tempfile.TemporaryDirectory() as temp,
                patch.object(
                    gate.docs.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, WHEEL, b""),
                        failure,
                    ],
                ),
            ):
                with self.assertRaisesRegex(ValueError, expected) as caught:
                    gate.verify_wheel(assets()[0], binding(), VERSION, Path(temp))
                self.assertNotIn("synthetic-secret-token", str(caught.exception))

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
        with patch.object(gate.docs, "api_first_page", return_value=(assets(), False)):
            self.assertEqual(len(gate.published_assets(17, binding())), 4)
        for bad in [
            assets()[:-1],
            assets() + [assets()[0]],
            [{**a, "digest": "sha256:" + "0" * 64} for a in assets()],
            [{**a, "state": "new"} for a in assets()],
        ]:
            with (
                patch.object(gate.docs, "api_first_page", return_value=(bad, False)),
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
            patch.object(gate.docs.subprocess, "run", side_effect=run),
        ):
            self.assertEqual(
                gate.verify_wheel(assets()[0], binding(), VERSION, Path(temp)), DIGEST
            )
        attestations = [c for c in commands if c[1:3] == ["attestation", "verify"]]
        self.assertEqual(len(attestations), 1)
        for arg in [
            "--source-digest",
            SOURCE,
            "--source-ref",
            "refs/tags/v" + VERSION,
            "--deny-self-hosted-runners",
            "--signer-workflow",
        ]:
            self.assertIn(arg, attestations[0])
        self.assertTrue(all(command[0] == "gh" for command in commands))
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(
                gate.docs.subprocess,
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
            "pypi_publication": {"source": SOURCE},
        }
        with (
            patch.object(gate, "signed_major", return_value=(OBJECT, SOURCE)),
            patch.object(gate.pypi, "assert_current"),
            patch.object(gate.docs, "tag_identity", return_value=(IMMUTABLE, SOURCE)),
            patch.object(
                gate.docs,
                "api",
                return_value=release(),
            ),
        ):
            with patch.object(
                gate.docs, "api_first_page", return_value=(assets(), False)
            ):
                gate.assert_current(receipt)
            with (
                patch.object(gate, "signed_major", return_value=("d" * 40, SOURCE)),
                self.assertRaises(ValueError),
            ):
                gate.assert_current(receipt)
            with (
                patch.object(
                    gate.docs,
                    "api_first_page",
                    return_value=(assets()[:-1], False),
                ),
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
        # Bootstrap, command, hosted and local runtime lanes must each install
        # the exact version; a missing lane is deliberate contract drift.
        self.assertEqual(data["installation_lanes"], 4)
        self.assertIn("not a proven runtime defect", data["diagnostics"][0])

    def test_caller_context_substitution_is_not_a_callee_identity_fix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / ".github/workflows"
            folder.mkdir(parents=True)
            for name in ["review-sensei-run.yml", "review-sensei-review.yml"]:
                text = (ROOT / ".github/workflows" / name).read_text()
                if name == "review-sensei-run.yml":
                    data = surface.workflow_data(text)
                    identity = next(
                        step
                        for step in data["jobs"]["bootstrap"]["steps"]
                        if step.get("id") == "release-identity"
                    )
                    for key, field in [
                        ("REVIEW_SENSEI_WORKFLOW_REF", "workflow_ref"),
                        ("REVIEW_SENSEI_WORKFLOW_SHA", "workflow_sha"),
                    ]:
                        self.assertEqual(
                            identity["env"][key], "${{ job." + field + " }}"
                        )
                        original = "${{ job." + field + " }}"
                        replacement = "${{ github." + field + " }}"
                        changed = text.replace(original, replacement, 1)
                        self.assertNotEqual(changed, text)
                        text = changed
                    # Preserve the Action pin comments while proving the exact
                    # semantic env fields changed, rather than trusting replace.
                    changed_data = surface.workflow_data(text)
                    changed_identity = next(
                        step
                        for step in changed_data["jobs"]["bootstrap"]["steps"]
                        if step.get("id") == "release-identity"
                    )
                    self.assertEqual(
                        changed_identity["env"]["REVIEW_SENSEI_WORKFLOW_REF"],
                        "${{ github.workflow_ref }}",
                    )
                    self.assertEqual(
                        changed_identity["env"]["REVIEW_SENSEI_WORKFLOW_SHA"],
                        "${{ github.workflow_sha }}",
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
                "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
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

    def test_retry_downloads_the_original_gate_artifact_id(self):
        data = surface.workflow_data(
            (ROOT / ".github/workflows/major-tag-validation.yml").read_text()
        )
        gate_job = data["jobs"]["gate"]
        self.assertEqual(
            gate_job["outputs"]["receipt_artifact_id"],
            "${{ steps.gate-receipt.outputs.artifact-id }}",
        )
        upload = next(
            step for step in gate_job["steps"] if step.get("id") == "gate-receipt"
        )
        self.assertTrue(upload["uses"].startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        downloads = [
            step
            for step in data["jobs"]["current"]["steps"]
            if step.get("uses", "").startswith("actions/download-artifact@")
        ]
        self.assertEqual(len(downloads), 1)
        download = downloads[0]
        self.assertEqual(
            download["with"]["artifact-ids"],
            "${{ needs.gate.outputs.receipt_artifact_id }}",
        )
        self.assertTrue(download["with"]["merge-multiple"])
        self.assertNotIn("name", download["with"])
        self.assertNotIn("run-id", download["with"])
        self.assertNotIn("github.run_attempt", json.dumps(download["with"]))

    @unittest.skipIf(os.name == "nt", "Linux receipt guard uses Bash")
    def test_missing_gate_artifact_id_fails_before_download(self):
        data = surface.workflow_data(
            (ROOT / ".github/workflows/major-tag-validation.yml").read_text()
        )
        steps = data["jobs"]["current"]["steps"]
        guard = next(
            step
            for step in steps
            if step.get("name") == "Require retained gate receipt ID"
        )
        downloads = [
            step
            for step in steps
            if step.get("uses", "").startswith("actions/download-artifact@")
        ]
        self.assertEqual(len(downloads), 1)
        download = downloads[0]
        self.assertLess(steps.index(guard), steps.index(download))
        self.assertEqual(
            guard["env"]["GATE_RECEIPT_ARTIFACT_ID"],
            "${{ needs.gate.outputs.receipt_artifact_id }}",
        )
        # Number.MAX_SAFE_INTEGER is accepted; its successor must be rejected.
        valid = {"665432109", "9007199254740991"}
        for value in (
            "",
            "0",
            "1,2",
            "-1",
            "not-an-id",
            "665432109",
            "9007199254740991",
            "9007199254740992",
            "9" * 100,
            " 1",
            "1 ",
            "+1",
            "https://example.com/1",
            "1\n",
        ):
            with self.subTest(value=value):
                result = subprocess.run(
                    ["bash", "-c", guard["run"]],
                    env={"PATH": os.defpath, "GATE_RECEIPT_ARTIFACT_ID": value},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, value in valid)
                if result.returncode:
                    self.assertIn(
                        "Retained gate receipt artifact ID is missing or invalid",
                        result.stdout,
                    )

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
        self.assertIn(
            '--expected-github-wheel-sha256 "$EXPECTED_GITHUB_WHEEL_SHA256"', text
        )
        self.assertEqual(
            data["jobs"]["gate"]["outputs"]["github_wheel_sha256"],
            "${{ steps.gate.outputs.github_wheel_sha256 }}",
        )
        self.assertIn('--github-version "$PUBLISHED_GITHUB_VERSION"', text)
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
        self.assertIn("--version 0.6.23", text)
        self.assertIn(
            "--expected-wheel-sha256 "
            "e3966ccc999fad1cfdc0b278f22e43b570d8c3f809acd3a507ad075517f2bf37",
            text,
        )
        self.assertIn("--github-version 0.1.1", text)
        self.assertIn(
            "--expected-github-wheel-sha256 "
            "139f40dc5778af743e868616a1eb9a3cd2d8ab4eda564ab514f35720edaed0ba",
            text,
        )
        self.assertNotIn("github.token", text)
        self.assertIn("if: always()", text)
        self.assertEqual(surface.check_workflow_text(text), [])
        for trigger in ["push", "pull_request"]:
            self.assertTrue(
                any(
                    fnmatch.fnmatchcase("scripts/major_tag_gate.py", pattern)
                    for pattern in data["on"][trigger]["paths"]
                )
            )

    def test_workflows_and_ci_use_the_same_yaml_requirement(self):
        self.assertEqual(
            (ROOT / "requirements/major-tag-validation.txt").read_text().strip(),
            "PyYAML==6.0.3\npypi-attestations==0.0.30\nsigstore==4.5.0",
        )
        self.assertIn("-r test.txt", (ROOT / "requirements/ci.txt").read_text())
        self.assertIn(
            "-r major-tag-validation.txt", (ROOT / "requirements/test.txt").read_text()
        )
        for name in ["major-tag-validation.yml", "major-tag-validation-ci.yml"]:
            text = (ROOT / ".github/workflows" / name).read_text()
            self.assertIn("-r requirements/major-tag-validation.txt", text)
            self.assertNotIn("PyYAML==", text)


class GateProcessTests(unittest.TestCase):
    def test_dispatch_failure_reports_stage_and_exit_without_private_diagnostics(self):
        args = [
            "dispatch",
            "--channel",
            "v5",
            "--tag-object",
            OBJECT,
            "--source-sha",
            SOURCE,
            "--dispatch",
        ]
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", args),
            patch.object(dispatch, "signed_major", return_value=(OBJECT, SOURCE)),
            patch.object(
                gate.docs.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(
                    4, ["gh"], stderr="synthetic-secret-token"
                ),
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(dispatch.main(), 1)
        self.assertIn(
            "GitHub major validation dispatch failed (exit 4)", stderr.getvalue()
        )
        self.assertNotIn("synthetic-secret-token", stderr.getvalue())

    def test_existing_output_receipt_cannot_be_overwritten_in_either_mode(self):
        for recheck in [False, True]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                output = root / "receipt"
                output.mkdir()
                prior = output / "gate.json"
                original = b'{"status":"PASS","immutable_previous_evidence":true}\n'
                prior.write_bytes(original)
                event_file = root / "event.json"
                event_file.write_text(json.dumps(event()))
                workflow_output = root / "outputs.txt"
                args = ["gate", "--output", str(output)]
                if recheck:
                    args.extend(["--recheck", str(prior)])
                env = {
                    **context(),
                    "GITHUB_EVENT_NAME": "push",
                    "GITHUB_EVENT_PATH": str(event_file),
                    "GITHUB_OUTPUT": str(workflow_output),
                }
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(sys, "argv", args),
                    patch.object(
                        gate, "qualify", return_value={"status": "PASS"}
                    ) as qualify,
                    patch.object(gate, "assert_current") as current,
                    redirect_stdout(io.StringIO()),
                ):
                    result = gate.main()
                self.assertEqual(prior.read_bytes(), original)
                self.assertTrue(result)
                self.assertEqual(workflow_output.read_text(), "eligible=false\n")
                qualify.assert_not_called()
                current.assert_not_called()

    def test_invalid_output_main_writes_no_partial_eligible_true(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event_file = root / "event.json"
            event_file.write_text(json.dumps(event()))
            output = root / "outputs.txt"
            receipt = {
                "status": "PASS",
                "version": VERSION + "\nextra=true",
                "github_version": "0.1.1",
                "wheel_sha256": DIGEST,
                "github_wheel_sha256": "e" * 64,
                "source_sha": SOURCE,
                "tag_object": OBJECT,
                "channel": "v5",
            }
            env = {
                **context(),
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": str(event_file),
                "GITHUB_OUTPUT": str(output),
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(sys, "argv", ["gate", "--output", str(root / "report")]),
                patch.object(gate, "qualify", return_value=receipt),
                redirect_stdout(io.StringIO()),
            ):
                self.assertTrue(gate.main())
            self.assertEqual(output.read_text(), "eligible=false\n")
            self.assertEqual(
                json.loads((root / "report/gate.json").read_text())["status"], "ERROR"
            )

    def test_unwritable_failure_output_preserves_original_error_and_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "receipt"
            output.mkdir()
            prior = output / "gate.json"
            original = b'{"status":"PASS"}\n'
            prior.write_bytes(original)
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(root)}, clear=True),
                patch.object(sys, "argv", ["gate", "--output", str(output)]),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertTrue(gate.main())
            self.assertEqual(prior.read_bytes(), original)
            receipt = json.loads(stdout.getvalue())
            self.assertEqual(receipt["status"], "ERROR")
            self.assertEqual(
                receipt["error"],
                "output must be a new directory; existing evidence left unchanged",
            )
            self.assertNotIn(str(root), stdout.getvalue() + stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_workflow_outputs_reject_newlines_before_any_eligible_output(self):
        receipt = {
            "status": "PASS",
            "version": VERSION,
            "github_version": "0.1.1",
            "wheel_sha256": DIGEST,
            "github_wheel_sha256": "e" * 64,
            "source_sha": SOURCE,
            "tag_object": OBJECT,
            "channel": "v5",
        }
        for key in [
            "version",
            "github_version",
            "wheel_sha256",
            "source_sha",
            "tag_object",
            "channel",
        ]:
            for bad in ["\nextra=true", "\rextra=true", None]:
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    gate.workflow_outputs({**receipt, key: bad})
        self.assertTrue(gate.workflow_outputs(receipt).startswith("eligible=true\n"))

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
    def test_pypi_publication_qualifies_while_npm_is_pending_or_failed(self):
        for status, conclusion, npm in [
            ("in_progress", None, None),
            ("completed", "failure", "failure"),
        ]:
            with self.subTest(npm=npm):
                self.run.update(status=status, conclusion=conclusion)
                for job in self.jobs:
                    if job["name"] == "Publish npm packages with Trusted Publishing":
                        job["conclusion"] = npm
                receipt = gate.qualify(
                    self.selected, self.root / ("pending" if npm is None else "failed")
                )
                self.assertEqual(receipt["status"], "PASS")

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
        self.enterContext(
            patch.object(gate.docs, "api_first_page", return_value=(assets(), False))
        )
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
        self.publication = {
            "source": SOURCE,
            "projects": {
                "review-sensei": {
                    "run_id": 123,
                    "artifacts": {
                        f"review_sensei-{VERSION}-py3-none-any.whl": {"sha256": DIGEST}
                    },
                },
                "review-sensei-github": {
                    "artifacts": {
                        f"review_sensei_github-{installer.hosted_version((ROOT / installer.HOSTED_MANIFEST).read_text(), VERSION)}-py3-none-any.whl": {
                            "sha256": "e" * 64
                        }
                    }
                },
            },
        }
        self.pypi = self.enterContext(
            patch.object(gate.pypi, "qualify", return_value=self.publication)
        )
        self.enterContext(patch.object(gate.pypi, "assert_current"))
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
        raise AssertionError(path)

    def test_full_gate_reads_only_frozen_source_and_attested_exact_release(self):
        receipt = gate.qualify(self.selected, self.root / "accepted")
        self.assertEqual(
            (receipt["status"], receipt["version"], receipt["wheel_sha256"]),
            ("PASS", VERSION, DIGEST),
        )
        self.assertEqual(receipt["release_binding"], binding())
        self.assertEqual(receipt["github_wheel_sha256"], "e" * 64)
        self.assertEqual(receipt["pypi_publication"], self.publication)
        self.assertTrue(receipt["surface"]["data_only"])
        self.assertEqual(len(receipt["source_data_sha256"]), 11)
        manifest = (ROOT / installer.HOSTED_MANIFEST).read_bytes()
        self.assertEqual(
            receipt["source_data_sha256"][installer.HOSTED_MANIFEST],
            hashlib.sha256(manifest).hexdigest(),
        )
        hosted = tomllib.loads(manifest.decode())
        self.assertEqual(receipt["github_version"], hosted["project"]["version"])
        self.wheel.assert_called_once()

    def test_hosted_manifest_must_pin_the_exact_engine_before_wheel(self):
        original = (ROOT / installer.HOSTED_MANIFEST).read_text()
        pin = '"review-sensei==' + VERSION + '"'
        for raw in [
            original.replace(pin, '"review-sensei==0.0.1"'),
            original.replace(pin, '"review-sensei>=' + VERSION + '"'),
            original.replace('name = "review-sensei-github"', 'name = "other"'),
        ]:
            self.assertNotEqual(raw, original)
            self.file_mutations[installer.HOSTED_MANIFEST] = raw.encode()
            with self.assertRaises(ValueError):
                gate.qualify(
                    self.selected, self.root / hashlib.sha256(raw.encode()).hexdigest()
                )
        self.wheel.assert_not_called()

    def test_hosted_only_channel_keeps_core_assets_on_the_actual_core_source(self):
        core_source = "d" * 40
        original_binding = {**binding(), "source": core_source}
        original_release = {
            **release(),
            "target_commitish": core_source,
            "body": "notes\n<!-- reviewsensei-release:"
            + json.dumps(original_binding)
            + " -->",
        }
        self.run["head_sha"] = core_source
        self.publication["projects"]["review-sensei"]["source"] = core_source

        def read(path):
            return (
                original_release
                if path == "releases/tags/v" + VERSION
                else self.api(path)
            )

        with (
            patch.object(
                gate.docs, "tag_identity", return_value=(IMMUTABLE, core_source)
            ),
            patch.object(gate.docs, "api", side_effect=read),
        ):
            receipt = gate.qualify(self.selected, self.root / "hosted-only")
        self.assertEqual(receipt["source_sha"], SOURCE)
        self.assertEqual(receipt["release_source_sha"], core_source)
        self.assertEqual(receipt["release_binding"]["source"], core_source)
        self.assertEqual(self.wheel.call_args.args[1]["source"], core_source)
        self.pypi.assert_called_once_with(
            SOURCE, VERSION, receipt["github_version"], self.root / "hosted-only/pypi"
        )

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
        self.pypi.side_effect = ValueError(
            "latest Python publication prerequisite did not succeed"
        )
        with self.assertRaises(ValueError):
            gate.qualify(self.selected, self.root / "failed-job")
        self.wheel.assert_not_called()

    def test_github_and_pypi_must_bind_the_same_release_run_and_engine_bytes(self):
        engine = self.publication["projects"]["review-sensei"]
        engine["run_id"] = 999
        with self.assertRaisesRegex(ValueError, "run/source differs"):
            gate.qualify(self.selected, self.root / "wrong-publication")
        engine["run_id"] = 123
        engine["artifacts"][f"review_sensei-{VERSION}-py3-none-any.whl"]["sha256"] = (
            "f" * 64
        )
        with self.assertRaisesRegex(ValueError, "wheel digests differ"):
            gate.qualify(self.selected, self.root / "different-registry-bytes")

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
