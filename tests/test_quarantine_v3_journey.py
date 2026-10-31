"""Inactive V3 public-entry composition; every external service is synthetic.

The test PyPI authority uses a local signing key/certificate and the real
Sigstore claim policies. It replaces only the external attestation format/trust
boundary, not the V3 issuer, its transport, scope checks or cold readers.
"""

import base64
import copy
import hashlib
import importlib.metadata
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID, ObjectIdentifier
from fake_github_http import json_response
from review_sensei_github import quarantine_published_tail as tail
from review_sensei_github.publication import approval_eligibility_from_body
from test_quarantine_activation import event, registered_resource
from test_quarantine_evidence import git
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture

from review_sensei.bounded_evidence import (
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    canonical_bytes,
)
from review_sensei.errors import ReviewInputError


class FixtureProvenance:
    """A synthetic external trust root; no production trust/certificate is used."""

    @classmethod
    def model_validate_json(cls, raw):
        from pypi_attestations import AttestationType

        value = json.loads(raw)
        certificate = x509.load_pem_x509_certificate(value["certificate"].encode())

        class Attestation:
            def verify(self, policy, distribution, *, offline=False):
                assert offline is True
                statement = canonical_bytes(
                    {"name": distribution.name, "digest": distribution.digest}
                )
                certificate.public_key().verify(
                    base64.b64decode(value["signature"]), statement
                )
                policy.verify(certificate)
                return AttestationType.PYPI_PUBLISH_V1, None

        return SimpleNamespace(
            attestation_bundles=[
                SimpleNamespace(
                    publisher=SimpleNamespace(model_dump=lambda: value["publisher"]),
                    attestations=[Attestation()],
                )
            ]
        )


class V3JourneyFixture(PublishedTailJourneyFixture):
    # Synthetic external qualification: two delayed POSTs plus one full-size
    # positive PATCH. The old generic ten-check envelope is tested separately
    # as an inadmissible same-head input; no production qualification changes.
    same_head_check_exposure = {
        "new_objects": 2,
        "new_wire_bytes": 2 * 65536,
        "positive_update_bytes": 65536,
        "max_object_wire_bytes": 65536,
    }

    def original_origins(self, **kwargs):
        old, registration = super().original_origins(**kwargs)
        self.same_head = getattr(type(self), "same_head", False)
        if self.same_head:
            self.live_head = self.requested_head = old.transaction.head_sha
        manifest = registration["published_tail"]
        profile = {
            "executor_sha": "89928b7bdbe942dc8a75419251c86157509eea2c",
            "caller_blob": manifest["caller_blob"],
            "roles": [
                ["published-review", "failure"],
                ["discussion", "cancelled"],
                ["discussion", "cancelled"],
                ["discussion", "cancelled"],
                ["discussion", "failure"],
            ],
            "same_head": True,
            "failure_union_sha256": "a" * 64,
            "core_sha256": "b" * 64,
            "hosted_sha256": "c" * 64,
        }
        profile_digest = hashlib.sha256(canonical_bytes(profile)).hexdigest()
        # This is a synthetic external source-qualification boundary. Both
        # production catalogs remain empty; this is not actual 899 admission.
        tail.FAILED_PUBLISHED_EXECUTORS[profile_digest] = profile
        qualification = copy.deepcopy(
            tail.BROKER_QUALIFICATIONS[manifest["broker_qualification_sha256"]]
        )
        if self.same_head and self.same_head_check_exposure is not None:
            qualification["exposure"]["surfaces"]["checks"] = copy.deepcopy(
                self.same_head_check_exposure
            )
        manifest.update(
            schema_version="failed-published-tail-isolation-v1",
            executor_sha=profile["executor_sha"],
            profile_sha256=profile_digest,
        )
        manifest["executions"][0]["conclusion"] = "failure"
        for execution in self.tail_runs.values():
            execution["referenced_workflows"][0]["sha"] = profile["executor_sha"]
        first = manifest["executions"][0]
        self.tail_runs[first["run_id"]]["conclusion"] = "failure"
        for job in self.tail_jobs[first["run_id"]]["jobs"]:
            if job["name"].endswith("/ hosted"):
                job["conclusion"] = "failure"
        qualification.update(
            contract=manifest["schema_version"], executor_sha=profile["executor_sha"]
        )
        qualification["execution_manifest_sha256"] = tail.execution_manifest_digest(
            manifest
        )
        manifest["broker_qualification_sha256"] = hashlib.sha256(
            canonical_bytes(qualification)
        ).hexdigest()
        tail.BROKER_QUALIFICATIONS[manifest["broker_qualification_sha256"]] = (
            qualification
        )
        registration.update(
            schema_version="quarantine-published-activation-v3",
            base_sha="publication-source-v1",
            head_sha=self.live_head,
            publication={"hosted_version": "0.3.23", "core_version": "0.8.17"},
        )
        # Actual Git ordering: compiled intent at B, prepared publication at S,
        # and only then creator metadata/event. Neither future R nor S is in B.
        git(self.root, "checkout", "--detach", self.policy)
        (self.root / "recovery-intent.json").write_bytes(canonical_bytes(registration))
        git(self.root, "add", "recovery-intent.json")
        git(self.root, "commit", "-m", "synthetic reviewed inactive v3 intent")
        self.intent_commit = git(self.root, "rev-parse", "HEAD")
        (self.root / "release-preparation.txt").write_text(
            "synthetic prepared publication\n"
        )
        git(self.root, "add", "release-preparation.txt")
        git(self.root, "commit", "-m", "synthetic prepared publication before creator")
        self.policy = self.policy_ref = self.live_base = self.requested_base = git(
            self.root, "rev-parse", "HEAD"
        )
        self.publication_source = self.policy
        self.creation_metadata = self.creator()
        self.event_document = event({**registration, "base_sha": self.policy})
        self.old_checks = copy.deepcopy(self.check_runs)
        self.publication_reads = []
        return old, registration

    def publication_artifact(self, project, version):
        name = project.replace("-", "_") + "-" + version + "-py3-none-any.whl"
        digest = hashlib.sha256(
            (project + self.publication_source).encode()
        ).hexdigest()
        return name, digest

    def provenance(self, project, version):
        from sigstore.verify import policy

        hosted = project == "review-sensei-github"
        workflow = "release-github.yml" if hosted else "release.yml"
        tag = ("github-v" if hosted else "v") + version
        environment = "pypi-github" if hosted else "pypi"
        run_id = 91 if hosted else 92
        values = {
            policy.OIDCIssuerV2.oid: "https://token.actions.githubusercontent.com",
            policy.OIDCBuildSignerURI.oid: f"https://github.com/malsabbagh/review-sensei/.github/workflows/{workflow}@refs/tags/{tag}",
            policy.OIDCBuildSignerDigest.oid: self.publication_source,
            policy.OIDCRunnerEnvironment.oid: "github-hosted",
            policy.OIDCSourceRepositoryURI.oid: "https://github.com/malsabbagh/review-sensei",
            policy.OIDCSourceRepositoryDigest.oid: self.publication_source,
            policy.OIDCSourceRepositoryRef.oid: "refs/tags/" + tag,
            policy.OIDCBuildTrigger.oid: "push",
            policy.OIDCRunInvocationURI.oid: f"https://github.com/malsabbagh/review-sensei/actions/runs/{run_id}/attempts/1",
            ObjectIdentifier("1.3.6.1.4.1.57264.1.23"): environment,
        }
        key = Ed25519PrivateKey.generate()
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic publication authority")]
        )
        now = datetime.now(timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
        )
        for oid, text in values.items():
            raw = text.encode()
            encoded = (
                b"\x0c"
                + (bytes([len(raw)]) if len(raw) < 128 else bytes([0x81, len(raw)]))
                + raw
            )
            builder = builder.add_extension(
                x509.UnrecognizedExtension(oid, encoded), critical=False
            )
        cert = builder.sign(key, algorithm=None)
        artifact, digest = self.publication_artifact(project, version)
        return {
            "publisher": {
                "kind": "GitHub",
                "repository": "malsabbagh/review-sensei",
                "workflow": workflow,
                "environment": environment,
            },
            "certificate": cert.public_bytes(serialization.Encoding.PEM).decode(),
            "signature": base64.b64encode(
                key.sign(canonical_bytes({"name": artifact, "digest": digest}))
            ).decode(),
        }

    def external(self, request, timeout):
        url = urlsplit(request.full_url)
        path = url.path
        if hasattr(self, "publication_source") and (
            url.hostname == "pypi.org"
            or path.startswith("/repos/malsabbagh/review-sensei/")
        ):
            self._meter(request)
            self.publication_reads.append(request.full_url)
            assert request.method == "GET"
            if url.hostname == "pypi.org":
                assert request.get_header("Authorization") is None
                bits = path.split("/")
                project, version = bits[2:4]
                if path.endswith("/json"):
                    name, digest = self.publication_artifact(project, version)
                    return json_response(
                        {
                            "info": {
                                "name": project,
                                "version": version,
                                "requires_dist": ["review-sensei==0.8.17"]
                                if project.endswith("-github")
                                else [],
                            },
                            "urls": [
                                {
                                    "filename": name,
                                    "digests": {"sha256": digest},
                                    "packagetype": "bdist_wheel",
                                    "yanked": False,
                                }
                            ],
                        }
                    )
                return json_response(self.provenance(project, version))
            if "/git/ref/tags/" in path:
                tag = path.rsplit("/", 1)[1]
                return json_response(
                    {
                        "ref": "refs/tags/" + tag,
                        "object": {
                            "type": "tag",
                            "sha": ("1" if tag.startswith("github-") else "2") * 40,
                        },
                    }
                )
            if "/git/tags/" in path:
                hosted = path.endswith("1" * 40)
                return json_response(
                    {
                        "sha": path.rsplit("/", 1)[1],
                        "tag": "github-v0.3.23" if hosted else "v0.8.17",
                        "object": {"type": "commit", "sha": self.publication_source},
                        "verification": {"verified": True},
                    }
                )
            if "/actions/workflows/" in path:
                hosted = "release-github.yml" in path
                return json_response(
                    {"total_count": 1, "workflow_runs": [self.publisher_run(hosted)]}
                )
            if path.endswith("/jobs"):
                hosted = "/91/" in path
                return json_response(
                    {
                        "total_count": 1,
                        "jobs": [
                            {
                                "id": 910 if hosted else 920,
                                "run_attempt": 1,
                                "status": "completed",
                                "conclusion": "success",
                                "completed_at": "2026-10-28T00:00:00Z",
                                "name": "Publish review-sensei-github to PyPI"
                                if hosted
                                else "Publish to PyPI with Trusted Publishing",
                            }
                        ],
                    }
                )
            return json_response(self.publisher_run(path.endswith("/91")))
        response = super().external(request, timeout)
        if (
            hasattr(self, "publication_source")
            and request.method == "GET"
            and path.endswith("/check-runs")
            and parse_qs(url.query).get("filter") != ["all"]
        ):
            # GitHub defaults to latest. A delayed old same-name check can
            # hide the successor unless it explicitly requests the full set.
            value = json.loads(response.body)
            rows = value["check_runs"]
            value["check_runs"] = [max(rows, key=lambda c: c["id"])] if rows else []
            value["total_count"] = len(value["check_runs"])
            return json_response(value)
        return response

    def publisher_run(self, hosted):
        return {
            "id": 91 if hosted else 92,
            "run_attempt": 1,
            "head_sha": self.publication_source,
            "head_branch": "github-v0.3.23" if hosted else "v0.8.17",
            "event": "push",
            "path": ".github/workflows/release-github.yml"
            if hosted
            else ".github/workflows/release.yml",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": "malsabbagh/review-sensei"},
            "head_repository": {"full_name": "malsabbagh/review-sensei"},
            "pull_requests": [],
        }


class SameHeadV3JourneyFixture(V3JourneyFixture):
    same_head = True


class QuarantineV3JourneyTests(unittest.TestCase):
    fixture_types = (V3JourneyFixture, SameHeadV3JourneyFixture)

    def test_published_source_creator_cold_decisions_approval_and_next_review(self):
        import pypi_attestations

        original_version = importlib.metadata.version
        versions = {"review-sensei": "0.8.17", "review-sensei-github": "0.3.23"}
        self.measurements = []

        for fixture_type in self.fixture_types:
            with (
                self.subTest(fixture=fixture_type.__name__),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(tail.BROKER_QUALIFICATIONS, {}, clear=True),
                patch.object(tail, "FAILED_PUBLISHED_EXECUTORS", {}, create=True),
                patch.object(pypi_attestations, "Provenance", FixtureProvenance),
                patch.object(
                    importlib.metadata,
                    "version",
                    side_effect=lambda n: (
                        versions[n] if n in versions else original_version(n)
                    ),
                ),
                patch.object(
                    importlib.metadata,
                    "requires",
                    return_value=["review-sensei==0.8.17"],
                ),
            ):
                fixture = fixture_type(directory)
                if hasattr(fixture, "bridge"):
                    self.addCleanup(fixture.bridge.close)

                def observe(stage):
                    state = fixture.selected.caller.accounting
                    self.assertEqual(state.configuration.limit, 512)
                    self.assertEqual(
                        len(fixture.accounted.observations), len(fixture.trace)
                    )
                    self.assertEqual(
                        state.startup.liability
                        + state.prior_nonstartup_liability
                        + state.confirmed
                        + state.uncertain
                        + state.reserved_undispatched
                        + state.remaining,
                        512,
                    )
                    self.measurements.append(
                        {
                            "fixture": fixture_type.__name__,
                            "stage": stage,
                            "physical_requests": len(fixture.trace),
                            "remaining": state.remaining,
                            "startup_liability": state.startup.liability,
                        }
                    )

                old, registration = fixture.original_origins(
                    failed_count=9, published_count=5
                )
                expected_pending = sum(
                    len(e.human_review.findings) if e.human_review is not None else 0
                    for e in (
                        approval_eligibility_from_body(r["body"])
                        for r in fixture.old_reviews
                    )
                )
                self.assertGreaterEqual(expected_pending, 5)
                old_body = fixture.comment["body"]
                before = fixture.provider_calls
                self.assertNotEqual(fixture.intent_commit, fixture.publication_source)
                self.assertNotIn(
                    fixture.publication_source,
                    (fixture.root / "recovery-intent.json").read_text(),
                )
                with registered_resource([registration]):
                    self.assertEqual(
                        fixture.invoke(), 0, fixture.error or repr(fixture.exception)
                    )
                    self.assertEqual(fixture.root_posts, 1)
                    self.assertEqual(fixture.provider_calls, before + 1)
                    observe("successor-review")
                    from review_sensei_github.quarantine_native_scope import (
                        bind_native_scope,
                        native_scope,
                    )

                    authenticated = fixture.selected.ledger._quarantine_authenticated
                    with self.assertRaises(ReviewInputError):
                        bind_native_scope(
                            fixture.selected.ledger,
                            fixture.selected.identity,
                            replace(authenticated),
                        )
                    with self.assertRaises(ReviewInputError):
                        native_scope(
                            fixture.selected.ledger.http,
                            repository="foreign/repository",
                        )
                    current = fixture.current_record()
                    self.assertEqual(
                        current.api_run_accounting["terminal_transaction"],
                        current.transaction.transaction_id,
                    )
                    self.assertEqual(fixture.comment["body"], old_body)
                    self.assertIsNone(old.api_run_accounting["terminal_transaction"])
                    view = approval_eligibility_from_body(
                        fixture.reviews[-1]["body"]
                    ).quarantine_assessments
                    self.assertEqual(len(view["pending"]), expected_pending)
                    self.assertFalse(
                        any(r["state"] == "APPROVED" for r in fixture.reviews)
                    )
                    # Original-scope same-head writes cannot supply successor
                    # finding decisions or become its current result authority.
                    if fixture.same_head:
                        fixture.check_runs.append(
                            {
                                **copy.deepcopy(fixture.old_checks[-1]),
                                "id": 9000000,
                                "conclusion": "failure",
                            }
                        )
                    for pending in view["pending"]:
                        fixture.quarantine_predecessor_comment_id = None
                        fixture.select_command(
                            "@reviewsensei dismiss "
                            + pending["instance"]
                            + ' --reason "quarantine:'
                            + pending["origin_scope_sha256"]
                            + ' Synthetic exact-source assessment for this instance."',
                            new_run=True,
                        )
                        self.assertEqual(
                            fixture.invoke_command(),
                            0,
                            fixture.error or repr(fixture.exception),
                        )
                        self.assertEqual(
                            len(fixture.accounted.observations), len(fixture.trace)
                        )
                        self.assertEqual(
                            fixture.selected.caller.accounting.configuration.limit, 512
                        )
                        observe("cold-decision")
                    self.assertEqual(
                        sum(r["state"] == "APPROVED" for r in fixture.reviews), 1
                    )
                    scoped_checks = [
                        c
                        for c in fixture.check_runs
                        if ":quarantine:" in c.get("external_id", "")
                    ]
                    self.assertEqual(len(scoped_checks), 1)
                    self.assertEqual(scoped_checks[0]["conclusion"], "success")
                    if fixture.same_head:
                        self.assertEqual(fixture.check_runs[-1]["id"], 9000000)
                        self.assertEqual(
                            fixture.check_runs[-1]["conclusion"], "failure"
                        )
                        fixture.check_runs.append(
                            {
                                **copy.deepcopy(fixture.old_checks[-1]),
                                "id": 9000001,
                                "conclusion": "failure",
                            }
                        )
                    fixture.select_command("@reviewsensei verify", new_run=True)
                    self.assertEqual(
                        fixture.invoke_command(),
                        0,
                        fixture.error or repr(fixture.exception),
                    )
                    observe("idempotent-verify")
                    self.assertEqual(
                        len(
                            [
                                c
                                for c in fixture.check_runs
                                if ":quarantine:" in c.get("external_id", "")
                            ]
                        ),
                        1,
                    )
                    self.assertEqual(
                        sum(r["state"] == "APPROVED" for r in fixture.reviews), 1
                    )
                    fixture.select_command(fixture.command_body, new_run=True)
                    fixture.command_active, fixture.owner_job = False, "hosted"
                    fixture.event_name, fixture.event_document = "pull_request", None
                    self.assertEqual(
                        fixture.invoke(), 0, fixture.error or repr(fixture.exception)
                    )
                    self.assertEqual(fixture.root_posts, 1)
                    observe("next-ordinary-review")
                    self.assertEqual(
                        sum(r["state"] == "APPROVED" for r in fixture.reviews), 1
                    )
                    parts = [
                        x
                        for x in fixture.extra_comments.values()
                        if x.get("body", "").endswith(
                            "<!-- reviewsensei:evidence-part:v1 -->"
                        )
                    ]
                    self.assertLessEqual(len(parts), MAX_STORED_PARTS)
                    self.assertLessEqual(
                        sum(len(x["body"].encode()) for x in parts),
                        MAX_STORED_PART_BYTES,
                    )
                    self.assertTrue(fixture.publication_reads)
