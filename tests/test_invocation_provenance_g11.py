"""G11 evidence joins never promote source labels or local metadata to authority."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import site
import stat
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei.errors import ReviewInputError
from review_sensei.provenance_manifest import (
    MAX_PROVENANCE_PACKET_BYTES,
    CiRunObservation,
    InstalledPackageObservation,
    InvocationProvenanceEvidence,
    ProvenanceManifest,
    QualificationReceiptBinding,
    SourceEventIdentity,
    bind_qualification_receipt,
    build_attempt_evidence_packet,
    build_provenance_manifest,
    classify_invocation_provenance,
    classify_provenance_compatibility,
    inspect_current_process_packages,
    invocation_route_sha256,
)
from tests.test_provenance_manifest_g11 import _complete_manifest

ROOT = Path(__file__).resolve().parents[1]


def _evidence(**changes):
    return replace(
        InvocationProvenanceEvidence(
            observation_scope="retained_attempt",
            invocation_sha256="1" * 64,
            observed_at="2026-11-12T14:00:00Z",
            core_artifact_version="0.8.18",
            core_artifact_sha256="2" * 64,
            core_artifact_source_commit="2" * 40,
            hosted_artifact_version="0.3.24",
            hosted_artifact_sha256="3" * 64,
            hosted_artifact_source_commit="3" * 40,
            core_module_origin="distribution-owned",
            core_module_file_sha256="4" * 64,
            hosted_module_origin="distribution-owned",
            hosted_module_file_sha256="5" * 64,
            caller_workflow_tag_object="not_applicable",
            caller_workflow_commit="4" * 40,
            caller_workflow_sha256="6" * 64,
            caller_workflow_trust="verified",
            reusable_workflow_sha256="7" * 64,
            reusable_workflow_signature="verified",
            worker_revision="5" * 40,
            activated_registry_sha256="8" * 64,
        ),
        **changes,
    )


def _manifest(**changes):
    return replace(_complete_manifest(), invocation_evidence=_evidence(), **changes)


def _receipt(manifest):
    return bind_qualification_receipt(
        manifest, receipt_sha256="9" * 64, outcome="passed"
    )


class InvocationProvenanceG11Tests(unittest.TestCase):
    def test_complete_scope_join_is_not_authenticity_qualification_or_deployment(self):
        manifest = _manifest()
        receipt = _receipt(manifest)
        report = classify_invocation_provenance(
            manifest, expected=manifest, qualification=receipt
        )
        self.assertEqual(report.status, "scope_matched")
        self.assertTrue(report.receipt_scope_matches)
        for label in (
            "qualification_authenticity_verified",
            "is_runtime_qualification_proof",
            "is_deployment_proof",
            "authority",
        ):
            self.assertIs(report.to_dict()[label], False)
        self.assertFalse(manifest.grants_deployment_proof)
        self.assertEqual(
            ProvenanceManifest.from_dict(manifest.to_dict(), require_digest=True),
            manifest,
        )
        self.assertEqual(
            QualificationReceiptBinding.from_dict(receipt.to_dict()), receipt
        )
        self.assertEqual(
            manifest.invocation_evidence.core_artifact_version,
            manifest.installed_core_version,
        )
        self.assertNotEqual(
            manifest.main_sha, manifest.invocation_evidence.core_artifact_source_commit
        )
        self.assertNotEqual(
            manifest.broker_revision, manifest.invocation_evidence.worker_revision
        )

    def test_source_only_metadata_is_never_invocation_qualification(self):
        for source in (ProvenanceManifest(), _complete_manifest()):
            report = classify_invocation_provenance(source, expected=source)
            self.assertEqual(report.status, "incomplete")
            self.assertIn("invocation_evidence", report.missing_fields)
            self.assertIn("qualification_receipt", report.missing_fields)
            self.assertFalse(report.to_dict()["is_runtime_qualification_proof"])
            legacy = classify_provenance_compatibility(source)
            self.assertEqual(legacy.to_dict()["comparison_scope"], "declared_metadata")
            self.assertFalse(legacy.to_dict()["is_runtime_qualification_proof"])
            with self.assertRaises(ReviewInputError):
                _receipt(source)

    def test_mixed_versions_origins_artifacts_workflows_worker_and_policy_refuse(self):
        expected = _manifest()
        for field, value in (
            ("installed_core_version", "0.8.17"),
            ("installed_hosted_version", "0.3.23"),
            ("reusable_workflow_tag_object", "6" * 40),
            ("reusable_workflow_peeled_commit", "7" * 40),
            ("broker_revision", "8" * 40),
            ("frozen_policy_commit", "9" * 40),
            ("frozen_policy_sha256", "0" * 64),
            ("selected_executor", "github-actions:different-executor"),
            ("broker_route", "/github/token"),
            ("head_sha", "0" * 40),
        ):
            with self.subTest(field=field):
                observed = replace(expected, **{field: value})
                report = classify_invocation_provenance(
                    observed, expected=expected, qualification=_receipt(observed)
                )
                self.assertEqual(report.status, "mismatch")
                self.assertIn(field, report.mismatched_fields)
        for field, value in (
            ("core_module_origin", "outside-distribution"),
            ("hosted_module_origin", "outside-distribution"),
            ("core_module_file_sha256", "0" * 64),
            ("hosted_module_file_sha256", "0" * 64),
            ("core_artifact_sha256", "0" * 64),
            ("core_artifact_version", "0.8.17"),
            ("hosted_artifact_version", "0.3.23"),
            ("core_artifact_source_commit", "0" * 40),
            ("hosted_artifact_source_commit", "0" * 40),
            ("caller_workflow_tag_object", "0" * 40),
            ("hosted_artifact_sha256", "0" * 64),
            ("caller_workflow_commit", "0" * 40),
            ("caller_workflow_sha256", "0" * 64),
            ("reusable_workflow_sha256", "0" * 64),
            ("caller_workflow_trust", "invalid"),
            ("reusable_workflow_signature", "absent"),
            ("reusable_workflow_signature", "invalid"),
            ("worker_revision", "0" * 40),
            ("activated_registry_sha256", "0" * 64),
        ):
            with self.subTest(field=field, value=value):
                observed = replace(
                    expected, invocation_evidence=_evidence(**{field: value})
                )
                report = classify_invocation_provenance(
                    observed, expected=expected, qualification=_receipt(observed)
                )
                self.assertEqual(report.status, "mismatch")
                self.assertIn("invocation_evidence." + field, report.mismatched_fields)

    def test_unknowns_do_not_borrow_expected_or_source_values(self):
        expected = _manifest()
        for name in expected.invocation_evidence.identity_fields():
            with self.subTest(field=name):
                observed = replace(
                    expected, invocation_evidence=_evidence(**{name: "unknown"})
                )
                # A receipt is optional: unknown invocation identity cannot be bound.
                report = classify_invocation_provenance(observed, expected=expected)
                self.assertEqual(report.status, "incomplete")
                self.assertIn("invocation_evidence." + name, report.missing_fields)
                self.assertEqual(getattr(observed.invocation_evidence, name), "unknown")
        report = classify_invocation_provenance(
            expected, expected=ProvenanceManifest(), qualification=_receipt(expected)
        )
        self.assertEqual(report.status, "incomplete")
        self.assertIn("expected.invocation_evidence", report.missing_fields)

    def test_receipt_is_bound_to_exact_manifest_invocation_and_route(self):
        manifest = _manifest()
        original = _receipt(manifest)
        for changed in (
            replace(manifest, head_sha="0" * 40),
            replace(manifest, broker_revision="0" * 40),
            replace(
                manifest, invocation_evidence=_evidence(invocation_sha256="0" * 64)
            ),
            replace(manifest, invocation_evidence=_evidence(worker_revision="0" * 40)),
            replace(
                manifest,
                invocation_evidence=_evidence(activated_registry_sha256="0" * 64),
            ),
            replace(
                manifest, invocation_evidence=_evidence(core_artifact_sha256="0" * 64)
            ),
        ):
            with self.subTest(digest=changed.manifest_sha256):
                self.assertNotEqual(
                    invocation_route_sha256(changed), original.route_sha256
                )
                report = classify_invocation_provenance(
                    changed, expected=changed, qualification=original
                )
                self.assertEqual(report.status, "mismatch")
                self.assertFalse(report.receipt_scope_matches)
                self.assertIn(
                    "qualification_receipt.manifest_sha256", report.mismatched_fields
                )
        failed = replace(original, outcome="failed")
        self.assertEqual(
            classify_invocation_provenance(
                manifest, expected=manifest, qualification=failed
            ).status,
            "mismatch",
        )

    def test_auto_inspection_cannot_fill_identified_or_historical_attempt(self):
        calls = []
        for arguments in (
            {
                "source_event": SourceEventIdentity(
                    event_kind="pull_request", source_id=12
                )
            },
            {
                "source_event": SourceEventIdentity(
                    event_kind="local_cli", source_digest="1" * 64
                )
            },
            {"invocation_evidence": _evidence()},
            {"pull_request": 421},
            {"head_sha": "a" * 40},
            {"invocation_evidence": _evidence(observation_scope="unknown")},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(
                    ReviewInputError, "identified or retained attempt"
                ):
                    build_provenance_manifest(
                        auto_inspect_local_packages=True,
                        version_lookup=lambda name: calls.append(name) or "0.8.21",
                        **arguments,
                    )
        self.assertEqual(calls, [])
        with self.assertRaises(ReviewInputError):
            build_provenance_manifest(
                auto_inspect_local_packages=True,
                invocation_evidence={},
                version_lookup=lambda name: calls.append(name) or "0.8.21",
            )
        self.assertEqual(calls, [])
        local = build_provenance_manifest(
            auto_inspect_local_packages=True,
            version_lookup=lambda name: (
                "0.8.21" if name == "review-sensei" else "0.3.27"
            ),
        )
        self.assertEqual(local.invocation_evidence.observation_scope, "current_process")
        self.assertEqual(local.invocation_evidence.invocation_sha256, "unknown")
        self.assertEqual(local.invocation_evidence.core_artifact_sha256, "unknown")
        self.assertFalse(local.has_complete_runtime_identity)
        explicit_unknown = build_provenance_manifest(
            auto_inspect_local_packages=True,
            invocation_evidence=InvocationProvenanceEvidence(),
            version_lookup=lambda name: "0.8.21",
        )
        self.assertEqual(
            explicit_unknown.invocation_evidence.observation_scope, "current_process"
        )
        missing_labels = build_provenance_manifest(
            auto_inspect_local_packages=True,
            pull_request=None,
            head_sha=None,
            version_lookup=lambda name: "0.8.21",
        )
        self.assertEqual(missing_labels.pull_request, "unknown")
        self.assertEqual(missing_labels.head_sha, "unknown")

    def test_extension_preserves_v1_digest_and_cannot_elevate_or_expand_limits(self):
        legacy = _complete_manifest()
        self.assertNotIn("invocation_evidence", legacy.to_dict())
        # Captured from the pre-extension v1 implementation on main 6a20327c.
        self.assertEqual(
            legacy.manifest_sha256,
            "77bf7e6c022bb130a819e59e46a1738db6a1a08a2b2011b9d866b56494cc8815",
        )
        self.assertEqual(
            ProvenanceManifest.from_dict(legacy.to_dict()).manifest_sha256,
            legacy.manifest_sha256,
        )
        evidence = _evidence().to_dict()
        for key in ("authority", "is_deployment_proof"):
            tampered = dict(evidence, **{key: True})
            with self.assertRaises(ReviewInputError):
                InvocationProvenanceEvidence.from_dict(tampered)
        for bad in (
            "Bearer testcredentialvalue",
            "ghp_123456789012345678901234",
            "../source",
        ):
            with self.assertRaises(ReviewInputError):
                _evidence(core_module_file_sha256=bad)
        manifest = _manifest()
        raw = manifest.to_dict()
        raw["invocation_evidence"]["worker_revision"] = "0" * 40
        with self.assertRaisesRegex(ReviewInputError, "manifest_sha256"):
            ProvenanceManifest.from_dict(raw, require_digest=True)
        self.assertEqual(MAX_PROVENANCE_PACKET_BYTES, 16_384)
        excessive = replace(
            manifest,
            ci_observations=tuple(
                CiRunObservation(
                    run_id=index + 1,
                    workflow_name="CI",
                    head_sha="a" * 40,
                    failed_step_names=tuple(str(i) + "x" * 180 for i in range(8)),
                )
                for index in range(16)
            ),
        )
        with self.assertRaisesRegex(ReviewInputError, "byte ceiling"):
            build_attempt_evidence_packet(provenance=excessive)


class CurrentProcessPackageObservationTests(unittest.TestCase):
    def test_metadata_is_not_imported_origin_or_artifact_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            root = prefix / "site-packages"
            core = root / "review_sensei/__init__.py"
            core.parent.mkdir(parents=True)
            core.write_bytes(b"# installed fixture\n")
            dist = SimpleNamespace(
                version="0.8.21",
                files=["review_sensei/__init__.py"],
                locate_file=lambda path: root / path,
            )
            loaded = SimpleNamespace(
                __spec__=SimpleNamespace(origin=str(core)), __file__=str(core)
            )
            with (
                patch("sys.prefix", str(prefix)),
                patch.dict(
                    sys.modules, {"review_sensei": loaded, "review_sensei_github": None}
                ),
                patch("importlib.metadata.distribution", return_value=dist),
            ):
                observations = inspect_current_process_packages()
                self.assertEqual(observations[0].module_origin, "distribution-owned")
                self.assertEqual(
                    observations[0].module_file_sha256,
                    hashlib.sha256(core.read_bytes()).hexdigest(),
                )
                self.assertEqual(observations[1].module_origin, "unknown")
                self.assertEqual(observations[0].to_dict()["wheel_sha256"], "unknown")
                loaded.__spec__.origin = str(
                    prefix.parent / "checkout/review_sensei/__init__.py"
                )
                self.assertEqual(
                    inspect_current_process_packages()[0].module_origin,
                    "outside-distribution",
                )
                self.assertEqual(
                    inspect_current_process_packages()[0].module_file_sha256, "unknown"
                )
                loaded.__spec__.origin = str(core)
                dist.files = None
                self.assertEqual(
                    inspect_current_process_packages()[0].module_origin, "unknown"
                )
                dist.files = ["review_sensei/__init__.py"]
                core.write_bytes(b"x" * 1_048_577)
                self.assertEqual(
                    inspect_current_process_packages()[0].module_file_sha256, "unknown"
                )
                if hasattr(os, "mkfifo"):
                    core.unlink()
                    os.mkfifo(core)
                    # A preexisting pipe is rejected without any read/open.
                    with (
                        patch(
                            "io.open", side_effect=AssertionError("must not open FIFO")
                        ),
                        patch(
                            "review_sensei.provenance_manifest.os.open",
                            side_effect=AssertionError("must not open FIFO"),
                        ),
                    ):
                        self.assertEqual(
                            inspect_current_process_packages()[0].module_file_sha256,
                            "unknown",
                        )
                    if hasattr(os, "O_NONBLOCK"):
                        # Simulate replacement after the regular-file precheck:
                        # nonblocking open and actual fstat still reject it.
                        with patch(
                            "review_sensei.provenance_manifest.os.stat",
                            return_value=SimpleNamespace(st_mode=stat.S_IFREG),
                        ):
                            self.assertEqual(
                                inspect_current_process_packages()[
                                    0
                                ].module_file_sha256,
                                "unknown",
                            )
        with self.assertRaises(ReviewInputError):
            InstalledPackageObservation(
                "review-sensei",
                module_origin="outside-distribution",
                module_file_sha256="0" * 64,
            )

    def test_current_process_collector_runs_from_installed_wheels_outside_checkout(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = root / "environment"
            # Bootstrap the fixture's own offline installer. Developer runners
            # may intentionally lack pip; copied relocatable POSIX interpreters
            # can also lose their libpython relative search path.
            venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(environment)
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            target = environment / (
                "Lib/site-packages"
                if os.name == "nt"
                else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            )
            wheels = []
            expected = []
            for name, package, source, version in (
                (
                    "review-sensei",
                    "review_sensei",
                    ROOT / "src/review_sensei",
                    "0.8.21",
                ),
                (
                    "review-sensei-github",
                    "review_sensei_github",
                    ROOT
                    / "packages/python/review-sensei-github/src/review_sensei_github",
                    "0.3.27",
                ),
            ):
                wheel = root / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
                _fixture_wheel(wheel, name, package, source, version)
                wheels.append(str(wheel))
                expected.append(
                    hashlib.sha256((source / "__init__.py").read_bytes()).hexdigest()
                )
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PYTHONUTF8"}
            }
            install = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--ignore-installed",
                    "--target",
                    str(target),
                    *wheels,
                ],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(
                install.returncode, 0, install.stderr.decode("utf-8", "replace")[-2000:]
            )
            # Dependency-only fallback is explicit. Candidate modules must still
            # resolve to their own installed wheel, not the checkout or this venv.
            command = (
                "import sys,json; sys.path.extend("
                + repr(site.getsitepackages())
                + "); import review_sensei,review_sensei_github; from review_sensei.provenance_manifest import inspect_current_process_packages; print(json.dumps([x.to_dict() for x in inspect_current_process_packages()]))"
            )
            result = subprocess.run(
                [str(python), "-I", "-c", command],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0, result.stderr.decode("utf-8", "replace")[-2000:]
            )
            observed = json.loads(result.stdout)
            self.assertEqual(
                [row["module_origin"] for row in observed], ["distribution-owned"] * 2
            )
            self.assertEqual([row["module_file_sha256"] for row in observed], expected)
            self.assertEqual([row["version"] for row in observed], ["0.8.21", "0.3.27"])
            self.assertTrue(all(row["wheel_sha256"] == "unknown" for row in observed))
            self.assertNotIn(str(root), result.stdout.decode())
            # Select actual installed-wheel metadata *before* inserting the
            # checkout. An editable checkout can itself expose .egg-info;
            # otherwise the negative case changes both import and metadata
            # selection, rather than testing the intended independent join.
            bootstrap = (
                "import sys,json; sys.path.extend("
                + repr(site.getsitepackages())
                + "); "
            )
            imports = "import review_sensei,review_sensei_github; from review_sensei.provenance_manifest import inspect_current_process_packages; "
            observe = "print(json.dumps([x.to_dict() for x in inspect_current_process_packages()]))"
            shadow = (
                bootstrap
                + "import importlib.metadata as metadata; from pathlib import Path; "
                + "lookup=metadata.distribution; pinned={name:lookup(name) for name in ('review-sensei','review-sensei-github')}; "
                + "assert all(Path(item.locate_file('')).resolve().is_relative_to(Path(sys.prefix).resolve()) for item in pinned.values()); "
                + "sys.path.insert(0, "
                + repr(str(ROOT / "src"))
                + "); "
                + imports
                + "metadata.distribution=lambda name:pinned[name] if name in pinned else lookup(name); "
                + observe
            )
            wrong_origin = subprocess.run(
                [str(python), "-I", "-c", shadow],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                wrong_origin.returncode,
                0,
                wrong_origin.stderr.decode("utf-8", "replace")[-2000:],
            )
            wrong = json.loads(wrong_origin.stdout)
            self.assertEqual(wrong[0]["version"], "0.8.21")
            self.assertEqual(wrong[0]["module_origin"], "outside-distribution")
            self.assertEqual(wrong[0]["module_file_sha256"], "unknown")
            # Reproduce CI's editable-source metadata overlay independently of
            # the developer checkout's installation state. Its inventory is
            # not installed-wheel ownership proof, so unknown is conservative.
            editable = root / "editable" / "src"
            with zipfile.ZipFile(wheels[0]) as archive:
                for member in archive.namelist():
                    if member.startswith("review_sensei/"):
                        destination = editable / member
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(archive.read(member))
            egg = editable / "review_sensei.egg-info"
            egg.mkdir()
            (egg / "PKG-INFO").write_text(
                "Metadata-Version: 2.1\nName: review-sensei\nVersion: 0.8.21\n",
                encoding="utf-8",
            )
            (egg / "SOURCES.txt").write_text(
                "src/review_sensei/__init__.py\n", encoding="utf-8"
            )
            editable_command = (
                bootstrap
                + "sys.path.insert(0, "
                + repr(str(editable))
                + "); "
                + imports
                + observe
            )
            overlaid = subprocess.run(
                [str(python), "-I", "-c", editable_command],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                overlaid.returncode,
                0,
                overlaid.stderr.decode("utf-8", "replace")[-2000:],
            )
            overlaid_rows = json.loads(overlaid.stdout)
            self.assertEqual(overlaid_rows[0]["version"], "0.8.21")
            self.assertEqual(overlaid_rows[0]["module_origin"], "unknown")
            self.assertEqual(overlaid_rows[0]["module_file_sha256"], "unknown")
            # Disk mutation is observable, not silently treated as the wheel.
            initializer = target / "review_sensei/__init__.py"
            initializer.write_bytes(
                initializer.read_bytes() + b"\n# changed installed fixture\n"
            )
            changed = subprocess.run(
                [str(python), "-I", "-c", command],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                changed.returncode, 0, changed.stderr.decode("utf-8", "replace")[-2000:]
            )
            changed_rows = json.loads(changed.stdout)
            self.assertNotEqual(changed_rows[0]["module_file_sha256"], expected[0])
            self.assertEqual(changed_rows[0]["wheel_sha256"], "unknown")


def _fixture_wheel(path, name, package, source, version):
    """Build only the network-free test wheel, not a release qualification build."""
    info = f"{name.replace('-', '_')}-{version}.dist-info"
    members = {
        package + "/" + file.relative_to(source).as_posix(): file.read_bytes()
        for file in source.rglob("*")
        if file.is_file() and file.suffix in {".py", ".json"}
    }
    members[info + "/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    )
    members[info + "/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: provenance-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for member, body in sorted(members.items()):
        writer.writerow(
            (
                member,
                "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(body).digest())
                .decode()
                .rstrip("="),
                len(body),
            )
        )
    writer.writerow((info + "/RECORD", "", ""))
    members[info + "/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, body in sorted(members.items()):
            archive.writestr(member, body)
