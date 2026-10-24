from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import pypi_channel_publication as publication
finally:
    sys.path.pop(0)

SOURCE = "a" * 40
OBJECT = "b" * 40


def wheel(project, version, pin=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        metadata = f"Name: {project}\nVersion: {version}\n"
        if pin:
            metadata += f"Requires-Dist: review-sensei=={pin}\n"
        archive.writestr(
            f"{project.replace('-', '_')}-{version}.dist-info/METADATA", metadata
        )
    return stream.getvalue()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = {}
        self.jobs = {}
        self.registry = {}
        self.bodies = {}
        self.reads = []
        self.enterContext(
            patch.object(publication, "core_installation", return_value=None)
        )
        for index, (project, version, tag, workflow, required) in enumerate(
            (
                (
                    "review-sensei",
                    "1.2.3",
                    "v1.2.3",
                    "release.yml",
                    publication.ENGINE_JOBS,
                ),
                (
                    "review-sensei-github",
                    "0.3.1",
                    "github-v0.3.1",
                    "release-github.yml",
                    {publication.HOSTED_JOB},
                ),
            ),
            1,
        ):
            self.runs[workflow] = {
                "id": index,
                "head_branch": tag,
                "head_sha": SOURCE,
                "path": ".github/workflows/" + workflow,
                "event": "push",
                "run_attempt": 2,
                "repository": {"full_name": publication.docs.REPOSITORY},
                "head_repository": {"full_name": publication.docs.REPOSITORY},
                "status": "in_progress",
                "conclusion": None,
                "pull_requests": [],
            }
            self.jobs[index] = [
                {
                    "id": 10 + n,
                    "name": name,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "run_id": index,
                    "head_sha": SOURCE,
                    "head_branch": tag,
                    "workflow_name": workflow,
                    "created_at": "2026-10-20T10:00:00Z",
                    "started_at": "2026-10-20T10:00:01Z",
                    "completed_at": "2026-10-20T10:00:03Z",
                    "runner_id": 1000,
                    "runner_name": "GitHub Actions 1000",
                    "runner_group_id": 0,
                    "runner_group_name": "GitHub Actions",
                    "labels": ["ubuntu-latest"],
                    "steps": [
                        {
                            "number": 1,
                            "name": "Publish wheel and source distribution",
                            "status": "completed",
                            "conclusion": "success",
                            "started_at": "2026-10-20T10:00:01Z",
                            "completed_at": "2026-10-20T10:00:02Z",
                        }
                    ],
                }
                for n, name in enumerate(sorted(required))
            ]
            stem = project.replace("-", "_") + "-" + version
            urls = []
            for suffix, kind in (
                ("-py3-none-any.whl", "bdist_wheel"),
                (".tar.gz", "sdist"),
            ):
                name = stem + suffix
                raw = (
                    wheel(project, version, "1.2.3" if index == 2 else None)
                    if kind == "bdist_wheel"
                    else b"synthetic source archive"
                )
                url = "https://files.pythonhosted.org/packages/" + name
                self.bodies[url] = raw
                self.bodies[
                    f"https://pypi.org/integrity/{project}/{version}/{name}/provenance"
                ] = b"{}"
                urls.append(
                    {
                        "filename": name,
                        "packagetype": kind,
                        "yanked": False,
                        "size": len(raw),
                        "digests": {"sha256": hashlib.sha256(raw).hexdigest()},
                        "url": url,
                    }
                )
            self.registry[f"https://pypi.org/pypi/{project}/{version}/json"] = {
                "info": {"name": project, "version": version},
                "urls": urls,
            }
        self.enterContext(
            patch.object(publication, "signed_release", return_value=(OBJECT, SOURCE))
        )
        self.enterContext(
            patch.object(publication.docs, "api_pages", side_effect=self.pages)
        )
        self.enterContext(
            patch.object(
                publication.docs,
                "api",
                side_effect=lambda path: next(
                    r for r in self.runs.values() if path == f"actions/runs/{r['id']}"
                ),
            )
        )
        self.enterContext(
            patch.object(publication, "public_read", side_effect=self.read)
        )
        self.verify = self.enterContext(patch.object(publication.subprocess, "run"))

    def pages(self, path, key):
        for workflow, run in self.runs.items():
            if (
                path
                == f"actions/workflows/{workflow}/runs?head_sha={run['head_sha']}&event=push"
            ):
                return [run]
            if path == f"actions/runs/{run['id']}/jobs?filter=all":
                return self.jobs[run["id"]]
        raise AssertionError(path)

    def read(self, url, maximum):
        self.reads.append(url)
        return (
            json.dumps(self.registry[url]).encode()
            if url in self.registry
            else self.bodies[url]
        )

    def qualify(self, name="accepted"):
        return publication.qualify(SOURCE, "1.2.3", "0.3.1", self.root / name)

    def test_verified_python_jobs_and_all_four_artifacts_qualify_independently_of_npm(
        self,
    ):
        for name, state, outcome in (
            ("pending", "waiting", None),
            ("failed", "completed", "failure"),
        ):
            self.jobs[1].append(
                {
                    "id": 99,
                    "name": "Publish npm packages with Trusted Publishing",
                    "run_attempt": 2,
                    "status": state,
                    "conclusion": outcome,
                }
            )
            self.runs["release.yml"].update(
                status="in_progress" if name == "pending" else "completed",
                conclusion=outcome,
            )
            receipt = self.qualify(name)
            self.assertEqual(
                set(receipt["projects"]), {"review-sensei", "review-sensei-github"}
            )
            self.assertEqual(
                sum(len(r["artifacts"]) for r in receipt["projects"].values()), 4
            )
            plan = json.loads((self.root / name / "verification-plan.json").read_text())
            self.assertEqual(len(plan), 4)
            self.assertEqual({p["publication_attempt"] for p in plan}, {1})
            command = self.verify.call_args
            self.assertTrue(command.kwargs["check"])
            self.assertEqual(command.kwargs["timeout"], 180)
            self.assertNotIn("GH_TOKEN", command.kwargs["env"])

    def test_missing_or_yanked_python_artifact_refuses_before_verification(self):
        for key in self.registry:
            original = copy.deepcopy(self.registry[key])
            for mutation in ("missing", "yanked", "duplicate"):
                value = copy.deepcopy(original)
                if mutation == "missing":
                    value["urls"].pop()
                if mutation == "yanked":
                    value["urls"][0]["yanked"] = True
                if mutation == "duplicate":
                    value["urls"][1] = value["urls"][0]
                self.registry[key] = value
                with (
                    self.subTest(project=key, mutation=mutation),
                    self.assertRaises(ValueError),
                ):
                    self.qualify(hashlib.sha256((key + mutation).encode()).hexdigest())
                self.registry[key] = original
        self.verify.assert_not_called()

    def test_corrupt_bytes_invalid_provenance_or_source_mismatch_fail(self):
        url = next(iter(self.bodies))
        original = self.bodies[url]
        self.bodies[url] = b"changed"
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            self.qualify("corrupt")
        self.bodies[url] = original
        self.verify.side_effect = subprocess.CalledProcessError(
            1, ["verifier"], stderr="synthetic-secret"
        )
        with self.assertRaisesRegex(
            ValueError, "cryptographic publication verification failed"
        ):
            self.qualify("bad-provenance")
        self.verify.side_effect = None
        with (
            patch.object(
                publication,
                "signed_release",
                side_effect=[(OBJECT, SOURCE), (OBJECT, "c" * 40)],
            ),
            self.assertRaisesRegex(ValueError, "selected source"),
        ):
            self.qualify("wrong-hosted-source")

    def test_hosted_only_publication_uses_each_packages_own_signed_source_and_attempt(
        self,
    ):
        old_core = "c" * 40
        self.runs["release.yml"]["head_sha"] = old_core
        for item in self.jobs[1]:
            item["head_sha"] = old_core
        proof = {
            "engine_source": old_core,
            "workflow_source": SOURCE,
            "mode": "recognized-build-input-equivalence",
            "inputs": {"fixture": "exact"},
        }
        with (
            patch.object(
                publication,
                "signed_release",
                side_effect=lambda tag: (
                    OBJECT,
                    old_core if tag == "v1.2.3" else SOURCE,
                ),
            ),
            patch.object(
                publication, "core_equivalence", return_value=proof
            ) as equivalence,
        ):
            receipt = self.qualify("hosted-only")
            self.assertEqual(receipt["projects"]["review-sensei"]["source"], old_core)
            self.assertEqual(
                receipt["projects"]["review-sensei-github"]["source"], SOURCE
            )
            self.assertEqual(receipt["core_equivalence"], proof)
            plan = json.loads(
                (self.root / "hosted-only/verification-plan.json").read_text()
            )
            for item in plan:
                self.assertEqual(
                    item["source"],
                    old_core if item["workflow"] == "release.yml" else SOURCE,
                )
                self.assertEqual(item["publication_attempt"], 1)
                self.assertEqual(item["run_attempt"], 2)
            equivalence.assert_called_with(old_core, SOURCE)
            del receipt["core_equivalence"]
            with self.assertRaisesRegex(
                ValueError, "require core build-input equivalence"
            ):
                publication.assert_current(receipt)

    def test_registry_only_core_uses_original_signed_publication_without_equivalence(
        self,
    ):
        old_core = "c" * 40
        self.runs["release.yml"]["head_sha"] = old_core
        for item in self.jobs[1]:
            item["head_sha"] = old_core
        proof = {
            "mode": "pypi-exact-v1",
            "engine_source": old_core,
            "workflow_source": SOURCE,
            "workflow_path": ".github/workflows/review-sensei-run.yml",
            "workflow_blob_sha": "d" * 40,
            "workflow_sha256": "e" * 64,
            "installation_lanes": 4,
        }
        with (
            patch.object(
                publication,
                "signed_release",
                side_effect=lambda tag: (
                    OBJECT,
                    old_core if tag == "v1.2.3" else SOURCE,
                ),
            ),
            patch.object(publication, "core_installation", return_value=proof),
            patch.object(
                publication,
                "core_equivalence",
                side_effect=ValueError("README differs"),
            ) as equivalence,
        ):
            receipt = self.qualify("registry-only")
            self.assertEqual(receipt["core_installation"], proof)
            self.assertNotIn("core_equivalence", receipt)
            self.assertEqual(receipt["projects"]["review-sensei"]["source"], old_core)
            equivalence.assert_not_called()
            for mutation in ("missing", "mixed", "source", "blob", "mode"):
                bad = copy.deepcopy(receipt)
                if mutation == "missing":
                    del bad["core_installation"]
                elif mutation == "mixed":
                    bad["core_equivalence"] = {"mode": "same-source"}
                else:
                    key = {
                        "source": "workflow_source",
                        "blob": "workflow_blob_sha",
                        "mode": "mode",
                    }[mutation]
                    bad["core_installation"][key] = "forged"
                with (
                    self.subTest(mutation=mutation),
                    self.assertRaisesRegex(ValueError, "installation"),
                ):
                    publication.assert_current(bad)

    def test_requested_but_unrecognized_registry_contract_never_falls_back(self):
        with (
            patch.object(
                publication,
                "core_installation",
                side_effect=ValueError("registry-only workflow is not recognized"),
            ),
            patch.object(publication, "core_equivalence") as equivalence,
            self.assertRaisesRegex(ValueError, "not recognized"),
        ):
            self.qualify("unrecognized-registry")
        equivalence.assert_not_called()
        self.assertEqual(self.reads, [])
        self.verify.assert_not_called()

    def test_changed_core_graph_refuses_split_source_before_registry_or_verifier(self):
        with (
            patch.object(
                publication,
                "signed_release",
                side_effect=[(OBJECT, "c" * 40), (OBJECT, SOURCE)],
            ),
            patch.object(
                publication,
                "core_equivalence",
                side_effect=ValueError("core build input changed"),
            ),
            self.assertRaisesRegex(ValueError, "core build input changed"),
        ):
            self.qualify("changed-core")
        self.assertEqual(self.reads, [])
        self.verify.assert_not_called()

    def test_missing_failed_pending_or_newer_failed_python_job_cannot_be_masked(self):
        for run_id in self.jobs:
            original = copy.deepcopy(self.jobs[run_id])
            for mutation in (
                "missing",
                "pending",
                "failed",
                "newer-failed",
                "duplicate",
                "invalid-attempt",
            ):
                jobs = copy.deepcopy(original)
                if mutation == "missing":
                    jobs.pop()
                if mutation == "pending":
                    jobs[0].update(status="waiting", conclusion=None)
                if mutation == "failed":
                    jobs[0]["conclusion"] = "failure"
                if mutation == "newer-failed":
                    jobs.append({**jobs[0], "run_attempt": 2, "conclusion": "failure"})
                if mutation == "duplicate":
                    jobs.append(jobs[0])
                if mutation == "invalid-attempt":
                    jobs[0]["run_attempt"] = 3
                self.jobs[run_id] = jobs
                with (
                    self.subTest(run_id=run_id, mutation=mutation),
                    self.assertRaises(ValueError),
                ):
                    self.qualify(f"job-{run_id}-{mutation}")
                self.jobs[run_id] = original
        self.verify.assert_not_called()

    def test_tag_and_public_registry_are_rechecked_after_cryptographic_verification(
        self,
    ):
        def race(*args, **kwargs):
            next(iter(self.registry.values()))["urls"][0]["yanked"] = True

        self.verify.side_effect = race
        with self.assertRaises(ValueError):
            self.qualify()
        self.verify.assert_called_once()

    def test_hosted_wheel_must_pin_the_engine_exactly(self):
        for pin in (None, "1.2.2"):
            with self.assertRaises(ValueError):
                publication.validate_wheel_metadata(
                    wheel("review-sensei-github", "0.3.1", pin),
                    "0.3.1",
                    "review-sensei-github",
                    "1.2.3",
                )


class PublicReadTests(unittest.TestCase):
    def test_untrusted_urls_refuse_before_any_network_read(self):
        for url in (
            "http://pypi.org/x",
            "https://pypi.org.evil/x",
            "https://user:password@pypi.org/x",
            "https://pypi.org:8443/x",
            "https://pypi.org/x#fragment",
        ):
            with (
                patch.object(publication, "build_opener") as opener,
                self.assertRaises(ValueError),
            ):
                publication.public_read(url, 10)
            opener.assert_not_called()

    def test_response_byte_and_time_bounds_and_safe_failure_diagnostics(self):
        class Response(io.BytesIO):
            status = 200
            headers = {}

        for body, maximum, headers in (
            (b"abcd", 3, {}),
            (b"a", 3, {"Content-Length": "4"}),
            (b"a", 3, {"Content-Length": "invalid"}),
        ):
            response = Response(body)
            response.headers = headers
            with (
                patch.object(publication, "build_opener") as opener,
                self.assertRaises(ValueError),
            ):
                opener.return_value.open.return_value = response
                publication.public_read("https://pypi.org/x", maximum)
        with patch.object(publication, "build_opener") as opener:
            opener.return_value.open.return_value = Response(b"abc")
            self.assertEqual(publication.public_read("https://pypi.org/x", 3), b"abc")
        with (
            patch.object(publication, "build_opener") as opener,
            self.assertRaisesRegex(ValueError, "read failed") as error,
        ):
            opener.return_value.open.side_effect = OSError("synthetic-secret")
            publication.public_read("https://pypi.org/x", 3)
        self.assertNotIn("synthetic-secret", str(error.exception))


class ProvenancePolicyTests(unittest.TestCase):
    def setUp(self):
        from cryptography import x509
        from cryptography.x509.oid import ObjectIdentifier
        from pypi_attestations import AttestationType
        from sigstore.verify import policy

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        artifact = root / "review_sensei-1.2.3-py3-none-any.whl"
        artifact.write_bytes(wheel("review-sensei", "1.2.3"))
        self.provenance = root / "provenance.json"
        self.provenance.write_text("{}")
        self.plan = root / "plan.json"
        self.plan.write_text(
            json.dumps(
                [
                    {
                        "artifact": str(artifact),
                        "provenance": str(self.provenance),
                        "source": SOURCE,
                        "workflow": "release.yml",
                        "tag": "v1.2.3",
                        "environment": "pypi",
                        "run_id": 1,
                        "publication_attempt": 2,
                    }
                ]
            )
        )
        uri = f"https://github.com/{publication.docs.REPOSITORY}"
        self.claims = {
            policy.OIDCIssuerV2.oid: "https://token.actions.githubusercontent.com",
            policy.OIDCBuildSignerURI.oid: uri
            + "/.github/workflows/release.yml@refs/tags/v1.2.3",
            policy.OIDCBuildSignerDigest.oid: SOURCE,
            policy.OIDCRunnerEnvironment.oid: "github-hosted",
            policy.OIDCSourceRepositoryURI.oid: uri,
            policy.OIDCSourceRepositoryDigest.oid: SOURCE,
            policy.OIDCSourceRepositoryRef.oid: "refs/tags/v1.2.3",
            policy.OIDCBuildTrigger.oid: "push",
            policy.OIDCRunInvocationURI.oid: uri + "/actions/runs/1/attempts/2",
            ObjectIdentifier("1.3.6.1.4.1.57264.1.23"): "pypi",
        }
        self.bundle = SimpleNamespace(
            publisher=SimpleNamespace(
                model_dump=lambda: {
                    "kind": "GitHub",
                    "repository": publication.docs.REPOSITORY,
                    "workflow": "release.yml",
                    "environment": "pypi",
                }
            ),
            attestations=[SimpleNamespace(verify=self.verify)],
        )
        self.kind = AttestationType.PYPI_PUBLISH_V1
        self.extensions = lambda: x509.Extensions(
            [
                x509.Extension(
                    oid,
                    False,
                    x509.UnrecognizedExtension(
                        oid, bytes([12, len(value)]) + value.encode()
                    ),
                )
                for oid, value in self.claims.items()
            ]
        )

    def verify(self, identity, distribution):
        identity.verify(SimpleNamespace(extensions=self.extensions()))
        self.assertEqual(distribution.name, "review_sensei-1.2.3-py3-none-any.whl")
        self.assertEqual(len(distribution.digest), 64)
        return self.kind, None

    def run_plan(self):
        with patch(
            "pypi_attestations.Provenance.model_validate_json",
            return_value=SimpleNamespace(attestation_bundles=[self.bundle]),
        ):
            publication.verify_plan(self.plan)

    def test_reexposed_metadata_plan_preserves_verified_original_invocation(self):
        from sigstore.verify import policy

        from tests.test_pypi_publication_attempts import captured_publication_case

        run, jobs, original, _alias = captured_publication_case()

        def pages(path, key):
            return [run] if key == "workflow_runs" else jobs

        with (
            patch.object(publication.docs, "api_pages", side_effect=pages),
            patch.object(publication.docs, "api", return_value=run),
        ):
            selected = publication.publication_run(
                run["head_sha"],
                "v0.8.6",
                "release.yml",
                publication.ENGINE_JOBS,
                publication.PUBLISH_JOB,
            )
        self.assertEqual(selected["run_attempt"], 3)
        self.assertEqual(selected["publication_attempt"], original["run_attempt"])
        plan = json.loads(self.plan.read_text())
        plan[0].update(
            run_id=selected["run_id"],
            publication_attempt=selected["publication_attempt"],
        )
        self.plan.write_text(json.dumps(plan))
        # Actual independently captured engine certificate invocation, not the
        # re-exposed job's run_attempt. Full signature verification stays in the
        # unchanged production attestation verifier; this fixture tests policy.
        self.claims[policy.OIDCRunInvocationURI.oid] = (
            "https://github.com/malsabbagh/review-sensei/actions/runs/39405656742/attempts/2"
        )
        self.run_plan()
        plan[0]["publication_attempt"] = 3
        self.plan.write_text(json.dumps(plan))
        with self.assertRaises(Exception):
            self.run_plan()

    def test_every_signed_claim_is_enforced_against_the_exact_publication(self):
        self.run_plan()
        for oid in list(self.claims):
            original = self.claims[oid]
            with self.subTest(oid=oid):
                self.claims[oid] = "other"
                with self.assertRaises(Exception):
                    self.run_plan()
                del self.claims[oid]
                with self.assertRaises(Exception):
                    self.run_plan()
                self.claims[oid] = original

    def test_wrong_publisher_predicate_signature_or_malformed_provenance_refuses(self):
        with (
            patch.object(self.bundle.publisher, "model_dump", return_value={}),
            self.assertRaisesRegex(ValueError, "publisher"),
        ):
            self.run_plan()
        self.kind = "https://slsa.dev/provenance/v1"
        with self.assertRaisesRegex(ValueError, "predicate"):
            self.run_plan()
        self.bundle.attestations[0].verify = lambda *args: (_ for _ in ()).throw(
            ValueError("invalid signature")
        )
        with self.assertRaisesRegex(ValueError, "invalid signature"):
            self.run_plan()
        # This path uses the real provenance parser rather than the policy fixture.
        with self.assertRaises(ValueError):
            publication.verify_plan(self.plan)


if __name__ == "__main__":
    unittest.main()
