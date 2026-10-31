"""Dormant V3 issuer: original transport, crypto claims, bounds and no data grants."""

import copy
import importlib.metadata
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pypi_attestations
from fake_github_http import FakeHTTPResponse, json_response
from review_sensei_github import quarantine_published_tail as tail
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.api_run_plan import owner_allocations
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from review_sensei_github.http import GitHubHttp
from review_sensei_github.quarantine_native_scope import (
    NativeScope,
    bind_native_scope,
    native_scope,
)
from review_sensei_github.quarantine_origin import AuthenticatedQuarantine
from review_sensei_github.quarantine_publication import (
    MAX_BYTES,
    MAX_READS,
    STARTUP_READ_SLOTS,
    PublicationProof,
    authenticate_publication,
)
from test_quarantine_activation import registered_resource
from test_quarantine_v3_journey import FixtureProvenance, V3JourneyFixture

from review_sensei.api_work import ApiRunIdentity
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

SOURCE = "c" * 40
INTENT = {"core_version": "0.8.17", "hosted_version": "0.3.23"}


class PublicationFixture(V3JourneyFixture):
    fault = None

    def external(self, request, timeout):
        response = super().external(request, timeout)
        path = urlsplit(request.full_url).path
        value = json.loads(response.body)
        if self.fault == "oversized" and path.endswith("/json"):
            return FakeHTTPResponse(b" " * (MAX_BYTES + 1))
        if self.fault == "unsigned" and "/git/tags/" in path:
            value["verification"]["verified"] = False
        if self.fault == "source" and path.endswith("/git/tags/" + "1" * 40):
            value["object"]["sha"] = "d" * 40
        if self.fault == "tag" and "/git/ref/tags/" in path:
            value["object"]["sha"] = "e" * 40
        if self.fault == "runs" and "/actions/workflows/" in path:
            value["total_count"] += 1
        if self.fault == "job" and path.endswith("/jobs"):
            value["jobs"][0]["conclusion"] = "failure"
        if self.fault == "attempt" and path.endswith("/91"):
            value["run_attempt"] = 2
        if (
            self.fault == "pin"
            and "/review-sensei-github/" in path
            and path.endswith("/json")
        ):
            value["info"]["requires_dist"] = ["review-sensei>=0.8.17"]
        if path.endswith("/provenance"):
            if self.fault == "signature":
                value["signature"] = "AAAA"
            if self.fault == "publisher":
                value["publisher"]["repository"] = "someone/else"
        return json_response(value)


class QuarantineV3PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = PublicationFixture(self.temp.name)
        self.fixture.publication_source = SOURCE
        self.fixture.publication_reads = []
        self.caller = ApiRunRequestTransport(
            directory=Path(self.temp.name),
            run=ApiRunIdentity(123, 9999),
            plan=review_startup_plan("hosted", quarantine_v3=True),
            attempt=1,
            opener=self.fixture.external,
        )
        self.fixture.accounted = self.caller
        self.http = GitHubHttp(
            api_url="https://api.github.test", opener=self.caller.open
        )
        self.enterContext(
            patch.object(pypi_attestations, "Provenance", FixtureProvenance)
        )
        self.enterContext(
            patch.object(
                importlib.metadata,
                "version",
                side_effect=lambda name: INTENT[
                    "hosted_version" if name.endswith("-github") else "core_version"
                ],
            )
        )
        self.enterContext(
            patch.object(
                importlib.metadata, "requires", return_value=["review-sensei==0.8.17"]
            )
        )

    def issue(self):
        return authenticate_publication(
            http=self.http, token="synthetic-metadata", intent=INTENT, source=SOURCE
        )

    def test_original_transport_immutable_cache_and_forged_receipts(self):
        proof = self.issue()
        self.assertEqual(len(self.caller.observations), MAX_READS)
        self.assertEqual(len(self.fixture.trace), MAX_READS)
        self.assertEqual(proof.receipt["source_commit"], SOURCE)
        self.assertIs(self.issue(), proof)
        self.assertEqual(len(self.caller.observations), MAX_READS + 2)
        proof.require_before("2026-10-28T00:01:00Z")
        for created_at in (None, "invalid", "2026-10-27T23:59:59Z"):
            with self.assertRaises(ReviewInputError):
                proof.require_before(created_at)
        for forged in (
            copy.copy(proof),
            PublicationProof(self.http, INTENT, proof.receipt),
        ):
            with self.assertRaisesRegex(ReviewInputError, "foreign"):
                forged.require(self.http, INTENT, SOURCE)
        for intent, source in (
            ({**INTENT, "core_version": "0.8.18"}, SOURCE),
            (INTENT, "d" * 40),
        ):
            with self.assertRaises(ReviewInputError):
                proof.require(self.http, intent, source)
        self.fixture.fault = "tag"
        with self.assertRaisesRegex(ReviewInputError, "tag changed"):
            self.issue()

    def test_publication_faults_never_issue_or_mutate(self):
        for fault in (
            "unsigned",
            "source",
            "runs",
            "job",
            "attempt",
            "pin",
            "signature",
            "publisher",
            "oversized",
        ):
            with self.subTest(fault=fault):
                self.fixture.fault = fault
                with self.assertRaises(ReviewInputError):
                    self.issue()
                self.assertFalse(
                    getattr(self.caller, "_quarantine_publication_proofs", {})
                )
                self.assertLessEqual(len(self.caller.observations), MAX_READS)
                self.assertTrue(
                    all(method == "GET" for _, method, _ in self.fixture.trace)
                )
                # Every invocation, including unsuccessful reads, is bounded.
                # Separate slots below avoid replenishing a single failed one.
                if fault != "oversized":
                    next_run = self.caller.run.run_id + 1
                    self.fixture.trace = []
                    self.caller = ApiRunRequestTransport(
                        directory=Path(self.temp.name),
                        run=ApiRunIdentity(123, next_run),
                        plan=review_startup_plan("hosted", quarantine_v3=True),
                        attempt=1,
                        opener=self.fixture.external,
                    )
                    self.fixture.accounted = self.caller
                    self.http = GitHubHttp(
                        api_url="https://api.github.test", opener=self.caller.open
                    )

    def test_unissued_native_scope_and_origin_copy_refuse(self):
        identity = SessionIdentity("owner/repo", 7, repository_id=123)
        forged = NativeScope(
            self.caller, identity, SOURCE, "b" * 40, "d" * 64, 999, frozenset()
        )
        self.caller._quarantine_v3_native_scope = forged
        with self.assertRaisesRegex(ReviewInputError, "foreign"):
            native_scope(self.http, repository=identity.repository)
        raw = AuthenticatedQuarantine(
            "d" * 64,
            b"{}",
            b'{"publication_scope":{}}',
            b"{}",
            b"{}",
            b"{}",
            self.caller,
            identity,
        )
        ledger = SimpleNamespace(http=self.http)
        for unissued in (raw, replace(raw)):
            with self.assertRaisesRegex(ReviewInputError, "authenticated origin"):
                bind_native_scope(ledger, identity, unissued)

    def test_missing_crypto_dependencies_cannot_issue(self):
        import sys

        provenance = {
            name: self.fixture.provenance(name, version)
            for name, version in [
                ("review-sensei", "0.8.17"),
                ("review-sensei-github", "0.3.23"),
            ]
        }
        with (
            patch.object(
                self.fixture,
                "provenance",
                side_effect=lambda name, version: provenance[name],
            ),
            patch.dict(sys.modules, {"sigstore.verify": None}),
        ):
            with self.assertRaisesRegex(
                ReviewInputError, "cryptographic verification failed"
            ):
                self.issue()
        self.assertFalse(getattr(self.caller, "_quarantine_publication_proofs", {}))

    def test_creator_scope_drift_refuses_before_new_root_or_inference(self):
        for fault in (
            "ordinal",
            "attempt",
            "workflow",
            "actor",
            "base",
            "head",
            "ordering",
        ):
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(tail.BROKER_QUALIFICATIONS, {}, clear=True),
                patch.dict(tail.FAILED_PUBLISHED_EXECUTORS, {}, clear=True),
            ):
                fixture = V3JourneyFixture(directory)
                fixture.original_origins(failed_count=1, published_count=1)
                registration = json.loads(
                    (fixture.root / "recovery-intent.json").read_bytes()
                )
                creator = fixture.creator()
                if fault == "ordinal":
                    registration["run_number"] += 1
                if fault == "attempt":
                    creator["run_attempt"] = 2
                if fault == "workflow":
                    registration["workflow_blob"] = "e" * 40
                if fault == "actor":
                    creator["actor"]["id"] += 1
                if fault == "base":
                    fixture.live_base = "f" * 40
                if fault == "head":
                    fixture.live_head = "f" * 40
                if fault == "ordering":
                    creator["created_at"] = "2026-10-27T23:59:59Z"
                fixture.creation_metadata = copy.deepcopy(creator)
                before = (
                    fixture.provider_calls,
                    len(fixture.reviews),
                    fixture.comment["body"],
                )
                with (
                    patch.object(fixture, "creator", return_value=creator),
                    registered_resource([registration]),
                ):
                    self.assertNotEqual(fixture.invoke(), 0, fault)
                self.assertEqual(fixture.root_posts, 0)
                self.assertEqual(
                    (
                        fixture.provider_calls,
                        len(fixture.reviews),
                        fixture.comment["body"],
                    ),
                    before,
                )

    def test_prepaid_startup_preserves_protected_tails_at_512(self):
        for job in ("hosted", "local", "command"):
            old, new = (
                review_startup_plan(job),
                review_startup_plan(job, quarantine_v3=True),
            )
            self.assertEqual(new.liability - old.liability, 2 * STARTUP_READ_SLOTS)
            for before, after in zip(old.slots, new.slots, strict=True):
                self.assertEqual(
                    tuple(s for s in after.requests if s != "v3-publication"),
                    before.requests,
                )
            a, b = (
                owner_allocations(512, old.liability),
                owner_allocations(512, new.liability),
            )
            self.assertEqual(
                (a.first_terminal, a.successor_terminal),
                (b.first_terminal, b.successor_terminal),
            )
            self.assertEqual(a.first_work - b.first_work, STARTUP_READ_SLOTS)
            self.assertEqual(new.liability + b.first_total + b.successor_total, 512)
