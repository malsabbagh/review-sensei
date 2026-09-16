from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from review_sensei.errors import ReviewInputError
from review_sensei.schemas import validate_public_document
from review_sensei.release_manifest import (
    FAILURE_AUTHENTICATION,
    FAILURE_NETWORK,
    FAILURE_OTHER,
    FAILURE_UNAVAILABLE,
    INSTALL_SOURCE_EXECUTING_COMMIT,
    INSTALL_SOURCE_PYPI,
    allow_executing_commit_fallback,
    begin_channel_promotion,
    bind_canary_evidence,
    build_compatibility_manifest,
    classify_install_failure,
    complete_channel_promotion,
    digest_file,
    evaluate_inflight_tag_movement,
    evaluate_publication_state,
    expected_artifact_digests,
    manifest_digest,
    prove_release_identity,
    record_channel_rollback,
    refuse_immutable_tag_replacement,
    validate_canary_binding,
    validate_channel_promotion_record,
    validate_compatibility_manifest,
    verify_artifact_digests,
    verify_worker_compatibility,
)

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "c" * 40
PREVIOUS = "d" * 40
PROVENANCE = "github-artifact-attestation"
SHA = "a" * 64


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _build_manifest(
    directory: Path,
    *,
    python_bytes: bytes = b"python-wheel",
    workflow_commit: str = COMMIT,
):
    return build_compatibility_manifest(
        release="1.0.0",
        workflow_path=_write(directory / "review-sensei-run.yml", b"workflow"),
        workflow_name="review-sensei-run.yml",
        workflow_commit=workflow_commit,
        python_path=_write(directory / "review-sensei.whl", python_bytes),
        python_name="review-sensei",
        npm_artifacts=(
            (
                "@reviewsensei/cli",
                _write(directory / "cli.tgz", b"npm-cli"),
            ),
        ),
        worker_path=_write(directory / "worker.js", b"worker"),
        worker_name="review-sensei-worker",
        schemas_version="1.0",
        compatible_worker_range=">=1.0.0 <2.0.0",
        provenance=PROVENANCE,
        provenance_kind=PROVENANCE,
    )


class CompatibilityManifestContractTests(unittest.TestCase):
    def test_build_output_includes_exact_file_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            python_path = directory / "review-sensei.whl"
            manifest = _build_manifest(directory)
            self.assertEqual(manifest.release, "1.0.0")
            self.assertEqual(manifest.workflow_commit, COMMIT)
            self.assertEqual(manifest.python.sha256, digest_file(python_path))
            self.assertEqual(
                manifest.workflow.sha256,
                digest_file(directory / "review-sensei-run.yml"),
            )
            verify_artifact_digests(manifest, expected_artifact_digests(manifest))

    def test_missing_artifact_file_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            missing = directory / "missing.whl"
            with self.assertRaises(ReviewInputError):
                build_compatibility_manifest(
                    release="1.0.0",
                    workflow_path=_write(directory / "workflow.yml", b"workflow"),
                    workflow_name="workflow.yml",
                    workflow_commit=COMMIT,
                    python_path=missing,
                    python_name="review-sensei",
                    npm_artifacts=(
                        ("@reviewsensei/cli", _write(directory / "cli.tgz", b"npm")),
                    ),
                    worker_path=_write(directory / "worker.js", b"worker"),
                    worker_name="review-sensei-worker",
                    schemas_version="1.0",
                    compatible_worker_range=">=1.0.0",
                    provenance=PROVENANCE,
                    provenance_kind=PROVENANCE,
                )

    def test_duplicate_npm_names_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaises(ReviewInputError):
                build_compatibility_manifest(
                    release="1.0.0",
                    workflow_path=_write(directory / "workflow.yml", b"workflow"),
                    workflow_name="workflow.yml",
                    workflow_commit=COMMIT,
                    python_path=_write(directory / "pkg.whl", b"python"),
                    python_name="review-sensei",
                    npm_artifacts=(
                        ("@reviewsensei/cli", _write(directory / "a.tgz", b"a")),
                        ("@reviewsensei/cli", _write(directory / "b.tgz", b"b")),
                    ),
                    worker_path=_write(directory / "worker.js", b"worker"),
                    worker_name="review-sensei-worker",
                    schemas_version="1.0",
                    compatible_worker_range=">=1.0.0",
                    provenance=PROVENANCE,
                    provenance_kind=PROVENANCE,
                )

    def test_mismatched_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            observed = expected_artifact_digests(manifest)
            observed["python"] = "b" * 64
            with self.assertRaisesRegex(ReviewInputError, "digest does not match"):
                verify_artifact_digests(manifest, observed)

    def test_partial_or_extra_observed_digests_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            observed = expected_artifact_digests(manifest)
            missing = dict(observed)
            missing.pop("worker")
            extra = dict(observed)
            extra["sidecar"] = SHA
            with self.assertRaisesRegex(ReviewInputError, "missing or ambiguous"):
                verify_artifact_digests(manifest, missing)
            with self.assertRaisesRegex(ReviewInputError, "missing or ambiguous"):
                verify_artifact_digests(manifest, extra)

    def test_outdated_worker_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            with self.assertRaisesRegex(
                ReviewInputError, "outside the compatible range"
            ):
                verify_worker_compatibility(manifest, "0.9.0")
            verify_worker_compatibility(manifest, "1.0.0")

    def test_build_rejects_missing_or_untrusted_provenance_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            kwargs = dict(
                release="1.0.0",
                workflow_path=_write(directory / "workflow.yml", b"workflow"),
                workflow_name="workflow.yml",
                workflow_commit=COMMIT,
                python_path=_write(directory / "pkg.whl", b"python"),
                python_name="review-sensei",
                npm_artifacts=(
                    ("@reviewsensei/cli", _write(directory / "cli.tgz", b"npm")),
                ),
                worker_path=_write(directory / "worker.js", b"worker"),
                worker_name="review-sensei-worker",
                schemas_version="1.0",
                compatible_worker_range=">=1.0.0",
                provenance="signed",
            )
            with self.assertRaisesRegex(
                ReviewInputError, "explicit trusted mechanism"
            ):
                build_compatibility_manifest(**kwargs, provenance_kind="signed")
            with self.assertRaises(TypeError):
                build_compatibility_manifest(**kwargs)

    def test_provenance_kind_disagreement_is_rejected(self) -> None:
        manifest = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "workflow_commit": COMMIT,
            "compatible_worker_range": ">=1.0.0",
            "provenance": "pypi-trusted-publishing",
            "provenance_kind": "github-artifact-attestation",
            "artifacts": {
                "workflow": {"name": "wf", "version": "1.0.0", "sha256": SHA},
                "python": {"name": "py", "version": "1.0.0", "sha256": SHA},
                "npm": [{"name": "cli", "version": "1.0.0", "sha256": SHA}],
                "schemas_version": "1.0",
                "worker": {"name": "worker", "version": "1.0.0", "sha256": SHA},
            },
        }
        with self.assertRaisesRegex(ReviewInputError, "disagrees with provenance"):
            validate_compatibility_manifest(manifest)

    def test_legacy_v1_schema_accepts_open_provenance_without_workflow_commit(self) -> None:
        legacy = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "compatible_worker_range": ">=1.0.0",
            "provenance": "signed",
            "artifacts": {
                "workflow": {"name": "wf", "version": "1.0.0", "sha256": SHA},
                "python": {"name": "py", "version": "1.0.0", "sha256": SHA},
                "npm": [{"name": "cli", "version": "1.0.0", "sha256": SHA}],
                "schemas_version": "1.0",
                "worker": {"name": "worker", "version": "1.0.0", "sha256": SHA},
            },
        }
        validate_public_document(legacy, "compatibility-manifest")
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(legacy)

    def test_malformed_manifest_and_sidecar_provenance_are_rejected(self) -> None:
        artifact = {"name": "x", "version": "1.0.0", "sha256": SHA}
        malformed = {
            "schema_version": "1.0",
            "release": "1.0.0",
            "compatible_worker_range": ">=1.0.0",
            "provenance": PROVENANCE,
            "artifacts": {
                "workflow": artifact,
                "python": artifact,
                "npm": [artifact],
                "schemas_version": "1.0",
                "worker": artifact,
            },
        }
        sidecar = {
            **malformed,
            "workflow_commit": COMMIT,
            "provenance": "sidecar-digest",
        }
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(malformed)
        with self.assertRaises(ReviewInputError):
            validate_compatibility_manifest(sidecar)

    def test_pypi_and_executing_commit_paths_prove_release_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            prove_release_identity(
                source=INSTALL_SOURCE_PYPI,
                requested_version="1.0.0",
                installed_version="1.0.0",
                manifest=manifest,
                executing_commit=COMMIT,
                observed_python_sha256=manifest.python.sha256,
            )
            prove_release_identity(
                source=INSTALL_SOURCE_EXECUTING_COMMIT,
                requested_version="1.0.0",
                installed_version="1.0.0",
                manifest=manifest,
                executing_commit=COMMIT,
            )
            with self.assertRaises(ReviewInputError):
                prove_release_identity(
                    source=INSTALL_SOURCE_PYPI,
                    requested_version="1.0.0",
                    installed_version="1.0.0",
                    manifest=manifest,
                    executing_commit=COMMIT,
                    observed_python_sha256="b" * 64,
                )
            with self.assertRaises(ReviewInputError):
                prove_release_identity(
                    source=INSTALL_SOURCE_EXECUTING_COMMIT,
                    requested_version="1.0.0",
                    installed_version="1.0.0",
                    manifest=manifest,
                    executing_commit=PREVIOUS,
                )
            with self.assertRaisesRegex(
                ReviewInputError, "does not match the manifest release"
            ):
                prove_release_identity(
                    source=INSTALL_SOURCE_PYPI,
                    requested_version="1.0.0",
                    installed_version="1.0.1",
                    manifest=manifest,
                    executing_commit=COMMIT,
                    observed_python_sha256=manifest.python.sha256,
                )

    def test_worker_caret_and_tilde_ranges_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            caret_manifest = build_compatibility_manifest(
                release="1.0.0",
                workflow_path=_write(
                    Path(temporary) / "caret-workflow.yml", b"workflow"
                ),
                workflow_name="review-sensei-run.yml",
                workflow_commit=COMMIT,
                python_path=_write(Path(temporary) / "caret.whl", b"python"),
                python_name="review-sensei",
                npm_artifacts=(
                    ("@reviewsensei/cli", _write(Path(temporary) / "caret.tgz", b"npm")),
                ),
                worker_path=_write(Path(temporary) / "caret-worker.js", b"worker"),
                worker_name="review-sensei-worker",
                schemas_version="1.0",
                compatible_worker_range="^1.0.0",
                provenance=PROVENANCE,
                provenance_kind=PROVENANCE,
            )
            verify_worker_compatibility(caret_manifest, "1.0.0")
            verify_worker_compatibility(caret_manifest, "1.2.9")
            with self.assertRaisesRegex(
                ReviewInputError, "outside the compatible range"
            ):
                verify_worker_compatibility(caret_manifest, "2.0.0")
            tilde_manifest = build_compatibility_manifest(
                release="1.0.0",
                workflow_path=_write(
                    Path(temporary) / "tilde-workflow.yml", b"workflow"
                ),
                workflow_name="review-sensei-run.yml",
                workflow_commit=COMMIT,
                python_path=_write(Path(temporary) / "tilde.whl", b"python"),
                python_name="review-sensei",
                npm_artifacts=(
                    ("@reviewsensei/cli", _write(Path(temporary) / "tilde.tgz", b"npm")),
                ),
                worker_path=_write(Path(temporary) / "tilde-worker.js", b"worker"),
                worker_name="review-sensei-worker",
                schemas_version="1.0",
                compatible_worker_range="~1.0.0",
                provenance=PROVENANCE,
                provenance_kind=PROVENANCE,
            )
            verify_worker_compatibility(tilde_manifest, "1.0.9")
            with self.assertRaisesRegex(
                ReviewInputError, "outside the compatible range"
            ):
                verify_worker_compatibility(tilde_manifest, "1.1.0")
            verify_worker_compatibility(manifest, "1.0.0")

    def test_network_and_auth_failures_are_not_unavailable_distributions(self) -> None:
        unavailable = "ERROR: No matching distribution found for review-sensei==1.0.0"
        network = "ERROR: Failed to establish a new connection: [Errno 101] Network is unreachable"
        auth = "ERROR: 401 Client Error: Unauthorized for url: https://pypi.org/simple/review-sensei/"
        other = (
            "ERROR: ResolutionImpossible: review-sensei 1.0.0 depends on missing extra"
        )
        unavailable_with_network_substring = (
            "INFO: upstream docs mention SSLError handling\n"
            "ERROR: No matching distribution found for review-sensei==1.0.0"
        )
        self.assertEqual(classify_install_failure(unavailable), FAILURE_UNAVAILABLE)
        self.assertEqual(classify_install_failure(network), FAILURE_NETWORK)
        self.assertEqual(classify_install_failure(auth), FAILURE_AUTHENTICATION)
        self.assertEqual(classify_install_failure(other), FAILURE_OTHER)
        self.assertEqual(
            classify_install_failure(unavailable_with_network_substring),
            FAILURE_UNAVAILABLE,
        )
        allow_executing_commit_fallback(FAILURE_UNAVAILABLE)
        allow_executing_commit_fallback(
            classify_install_failure(unavailable_with_network_substring)
        )
        with self.assertRaises(ReviewInputError):
            allow_executing_commit_fallback(FAILURE_NETWORK)
        with self.assertRaises(ReviewInputError):
            allow_executing_commit_fallback(FAILURE_AUTHENTICATION)
        with self.assertRaises(ReviewInputError):
            allow_executing_commit_fallback(FAILURE_OTHER)

    def test_fixture_canary_binds_manifest_and_live_canary_stays_operator_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            bound = bind_canary_evidence(manifest, "fixture-downstream")
            live = bind_canary_evidence(manifest, "operator-live")
            self.assertEqual(bound.status, "bound")
            self.assertEqual(bound.manifest_sha256, manifest_digest(manifest))
            self.assertEqual(live.status, "deferred-operator-only")
            validate_canary_binding(bound.to_dict())
            with self.assertRaises(ReviewInputError):
                begin_channel_promotion(
                    manifest=manifest,
                    canary=live,
                    previous_target=PREVIOUS,
                    publication_state="complete",
                    recorded_at="2026-09-15T00:00:00Z",
                )

    def test_partial_publication_cannot_promote_v4(self) -> None:
        published = {
            "workflow": True,
            "python": True,
            "npm": False,
            "worker": True,
            "schemas": True,
        }
        self.assertEqual(evaluate_publication_state(published), "partial")
        self.assertEqual(
            evaluate_publication_state({key: True for key in published}),
            "complete",
        )
        self.assertEqual(
            evaluate_publication_state({key: False for key in published}),
            "failed",
        )
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            canary = bind_canary_evidence(manifest, "fixture-downstream")
            with self.assertRaisesRegex(ReviewInputError, "complete publication"):
                begin_channel_promotion(
                    manifest=manifest,
                    canary=canary,
                    previous_target=PREVIOUS,
                    publication_state="partial",
                    recorded_at="2026-09-15T00:00:00Z",
                )

    def test_inflight_tag_movement_fails_closed_without_authorized_grace(self) -> None:
        evaluate_inflight_tag_movement(start_sha=COMMIT, current_sha=COMMIT)
        with self.assertRaisesRegex(ReviewInputError, "not authorized"):
            evaluate_inflight_tag_movement(start_sha=COMMIT, current_sha=PREVIOUS)
        with self.assertRaisesRegex(ReviewInputError, "not authorized"):
            evaluate_inflight_tag_movement(
                start_sha=COMMIT,
                current_sha=PREVIOUS,
                authorized_grace={"authorized": False},
            )
        evaluate_inflight_tag_movement(
            start_sha=COMMIT,
            current_sha=PREVIOUS,
            authorized_grace={
                "authorized": True,
                "start_sha": COMMIT,
                "current_sha": PREVIOUS,
            },
        )
        with self.assertRaisesRegex(ReviewInputError, "does not match this run"):
            evaluate_inflight_tag_movement(
                start_sha=COMMIT,
                current_sha=PREVIOUS,
                authorized_grace={
                    "authorized": True,
                    "start_sha": COMMIT,
                    "current_sha": "e" * 40,
                },
            )

    def test_promotion_is_serialized_and_rollback_records_channel_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            restored = _build_manifest(
                directory / "previous",
                python_bytes=b"old",
                workflow_commit=PREVIOUS,
            )
            current_manifest = _build_manifest(directory / "current")
            canary = bind_canary_evidence(current_manifest, "fixture-downstream")
            in_flight = begin_channel_promotion(
                manifest=current_manifest,
                canary=canary,
                previous_target=PREVIOUS,
                publication_state="complete",
                recorded_at="2026-09-15T00:00:00Z",
            )
            with self.assertRaisesRegex(ReviewInputError, "already in flight"):
                begin_channel_promotion(
                    manifest=current_manifest,
                    canary=canary,
                    previous_target=PREVIOUS,
                    publication_state="complete",
                    recorded_at="2026-09-15T00:01:00Z",
                    ledger=(in_flight,),
                )
            completed = complete_channel_promotion(
                in_flight,
                recorded_at="2026-09-15T00:02:00Z",
                ledger=(in_flight,),
            )
            validate_channel_promotion_record(completed.to_dict())
            restored_canary = bind_canary_evidence(restored, "fixture-downstream")
            rollback = record_channel_rollback(
                current=completed,
                restored=restored,
                canary=restored_canary,
                recorded_at="2026-09-15T00:03:00Z",
                ledger=(in_flight, completed),
            )
            self.assertEqual(rollback.action, "rollback")
            self.assertEqual(rollback.previous_target, COMMIT)
            self.assertEqual(rollback.new_target, PREVIOUS)
            self.assertEqual(
                rollback.canary_manifest_sha256, restored_canary.manifest_sha256
            )

    def test_complete_promotion_requires_in_flight_record_in_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            canary = bind_canary_evidence(manifest, "fixture-downstream")
            in_flight = begin_channel_promotion(
                manifest=manifest,
                canary=canary,
                previous_target=PREVIOUS,
                publication_state="complete",
                recorded_at="2026-09-15T00:00:00Z",
            )
            with self.assertRaisesRegex(ReviewInputError, "not in the audit ledger"):
                complete_channel_promotion(
                    in_flight,
                    recorded_at="2026-09-15T00:01:00Z",
                    ledger=(),
                )

    def test_rollback_refuses_restored_manifest_with_wrong_channel_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            restored = _build_manifest(
                directory / "restored",
                python_bytes=b"old",
                workflow_commit=COMMIT,
            )
            current_manifest = _build_manifest(directory / "current")
            canary = bind_canary_evidence(current_manifest, "fixture-downstream")
            in_flight = begin_channel_promotion(
                manifest=current_manifest,
                canary=canary,
                previous_target=PREVIOUS,
                publication_state="complete",
                recorded_at="2026-09-15T00:00:00Z",
            )
            completed = complete_channel_promotion(
                in_flight,
                recorded_at="2026-09-15T00:01:00Z",
                ledger=(in_flight,),
            )
            with self.assertRaisesRegex(
                ReviewInputError, "previous immutable channel SHA"
            ):
                record_channel_rollback(
                    current=completed,
                    restored=restored,
                    canary=bind_canary_evidence(restored, "fixture-downstream"),
                    recorded_at="2026-09-15T00:02:00Z",
                    ledger=(in_flight, completed),
                )

    def test_rollback_refuses_canary_that_does_not_bind_restored_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            restored = _build_manifest(
                directory / "restored",
                python_bytes=b"old",
                workflow_commit=PREVIOUS,
            )
            current_manifest = _build_manifest(directory / "current")
            canary = bind_canary_evidence(current_manifest, "fixture-downstream")
            in_flight = begin_channel_promotion(
                manifest=current_manifest,
                canary=canary,
                previous_target=PREVIOUS,
                publication_state="complete",
                recorded_at="2026-09-15T00:00:00Z",
            )
            completed = complete_channel_promotion(
                in_flight,
                recorded_at="2026-09-15T00:01:00Z",
                ledger=(in_flight,),
            )
            wrong_canary = bind_canary_evidence(current_manifest, "fixture-downstream")
            with self.assertRaisesRegex(
                ReviewInputError, "does not bind the restored manifest"
            ):
                record_channel_rollback(
                    current=completed,
                    restored=restored,
                    canary=wrong_canary,
                    recorded_at="2026-09-15T00:02:00Z",
                    ledger=(in_flight, completed),
                )

    def test_immutable_package_tags_and_version_bytes_cannot_be_replaced(self) -> None:
        refuse_immutable_tag_replacement("v4")
        with self.assertRaisesRegex(ReviewInputError, "cannot be replaced"):
            refuse_immutable_tag_replacement("v1.0.0")
        with tempfile.TemporaryDirectory() as temporary:
            first = _build_manifest(Path(temporary), python_bytes=b"first")
            second = _build_manifest(Path(temporary), python_bytes=b"second")
            canary = bind_canary_evidence(second, "fixture-downstream")
            with self.assertRaisesRegex(ReviewInputError, "cannot be replaced"):
                begin_channel_promotion(
                    manifest=second,
                    canary=canary,
                    previous_target=PREVIOUS,
                    publication_state="complete",
                    recorded_at="2026-09-15T00:00:00Z",
                    published_version_digests={"1.0.0": manifest_digest(first)},
                )

    def test_build_and_validate_scripts_round_trip(self) -> None:
        build = _load_script("build_compatibility_manifest.py")
        validate = _load_script("validate_compatibility_manifest.py")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            workflow = _write(directory / "workflow.yml", b"workflow")
            python_path = _write(directory / "pkg.whl", b"python")
            npm = _write(directory / "cli.tgz", b"npm")
            worker = _write(directory / "worker.js", b"worker")
            output = directory / "compatibility-manifest.json"
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(
                    build.main(
                        [
                            "--release",
                            "1.0.0",
                            "--workflow-commit",
                            COMMIT,
                            "--workflow",
                            str(workflow),
                            "--python",
                            str(python_path),
                            "--npm",
                            f"@reviewsensei/cli={npm}",
                            "--worker",
                            str(worker),
                            "--schemas-version",
                            "1.0",
                            "--compatible-worker-range",
                            ">=1.0.0 <2.0.0",
                            "--provenance-kind",
                            PROVENANCE,
                            "--output",
                            str(output),
                        ]
                    ),
                    0,
                )
                self.assertEqual(validate.main([str(output)]), 0)
                self.assertEqual(validate.main([str(directory / "missing.json")]), 1)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["workflow_commit"], COMMIT)
            self.assertEqual(payload["provenance_kind"], PROVENANCE)
            self.assertIn("digest", stdout.getvalue())
            self.assertIn("validation failed", stderr.getvalue())

    def test_unsupported_identity_and_channel_inputs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = _build_manifest(Path(temporary))
            with self.assertRaises(ReviewInputError):
                prove_release_identity(
                    source="main",
                    requested_version="1.0.0",
                    installed_version="1.0.0",
                    manifest=manifest,
                    executing_commit=COMMIT,
                )
            with self.assertRaises(ReviewInputError):
                verify_artifact_digests(
                    manifest,
                    {**expected_artifact_digests(manifest), "python": "not-a-digest"},
                )
            with self.assertRaises(ReviewInputError):
                verify_worker_compatibility(manifest, "not-a-version")
            with self.assertRaises(ReviewInputError):
                evaluate_publication_state(
                    {
                        "workflow": True,
                        "python": True,
                        "npm": True,
                        "worker": True,
                        "schemas": 1,
                    }
                )
            with self.assertRaises(ReviewInputError):
                refuse_immutable_tag_replacement("main")
            self.assertEqual(classify_install_failure(""), FAILURE_OTHER)


if __name__ == "__main__":
    unittest.main()
