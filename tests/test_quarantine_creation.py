"""One-shot root creation and uncertainty through actual native codecs/meter."""

import copy
import hashlib
import itertools
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.quarantine_activation import authorize_creation_slot
from review_sensei_github.quarantine_creation import (
    create_quarantine,
    prepare_quarantine,
)
from review_sensei_github.quarantine_evidence import CurrentReadScope
from review_sensei_github.quarantine_origin import QuarantineOriginReader
from review_sensei_github.review_run import StartupRunLedger
from review_sensei_github.session_ledger import parse_session_comment
from test_quarantine_activation import event, registered_resource
from test_quarantine_evidence import original_objects
from test_quarantine_origin import OriginFixture

from review_sensei.api_work import ApiExecutionOwner, FrozenApiConfiguration
from review_sensei.errors import ReviewInputError

_RUN_IDS = itertools.count(1900)


class CreationFixture(OriginFixture):
    creation_fault = None
    role = "maintain"
    root_posts = 0
    carrier_fault = None

    def creator(self):
        return {
            "id": self.run_id,
            "repository": {"id": 123, "full_name": "owner/repo"},
            "workflow_id": self.run_id + 100,
            "run_number": 1,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "status": "in_progress",
            "path": ".github/workflows/review-sensei-quarantine.yml",
            "head_sha": self.policy,
            "actor": {"login": "alice", "type": "User", "id": 99},
            "triggering_actor": {"login": "alice", "type": "User", "id": 99},
        }

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if self.source_previous is not None:
            if path.endswith("/collaborators/alice/permission"):
                self.trace.append((self.attempt, request.method, path))
                return json_response(
                    {
                        "permission": self.role,
                        "user": {"id": 99, "login": "alice", "type": "User"},
                    }
                )
            if path in {
                f"/repos/owner/repo/actions/runs/{self.run_id}",
                f"/repos/owner/repo/actions/runs/{self.run_id}/attempts/1",
            }:
                self.trace.append((self.attempt, request.method, path))
                return json_response(self.creator())
            if path.endswith(f"/actions/runs/{self.run_id}/attempts/1/jobs"):
                self.trace.append((self.attempt, request.method, path))
                return json_response(
                    {
                        "total_count": 1,
                        "jobs": [
                            {
                                "id": self.run_id * 10,
                                "run_id": self.run_id,
                                "run_attempt": 1,
                                "name": "review-or-reply / hosted",
                                "status": "in_progress",
                            }
                        ],
                    }
                )
            if request.method == "POST" and path.endswith("/issues/7/comments"):
                payload = json.loads(request.data)
                if "reviewsensei:session:v2" in payload.get("body", ""):
                    self.trace.append((self.attempt, request.method, path))
                    self.root_posts += 1
                    if self.creation_fault == "absent":
                        raise TimeoutError(
                            "synthetic unstored new-root acknowledgement"
                        )
                    identifier = 4_000_000 + len(self.extra_comments)
                    item = {
                        "id": identifier,
                        "body": payload["body"],
                        "user": {
                            "id": 999,
                            "login": "reviewsensei[bot]",
                            "type": "Bot",
                        },
                        "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                    }
                    self.extra_comments[identifier] = item
                    if self.creation_fault == "stored":
                        raise TimeoutError("synthetic stored new-root acknowledgement")
                    return json_response(copy.deepcopy(item), 201)
            if request.method == "POST" and self.carrier_fault:
                payload = json.loads(request.data)
                if payload.get("body", "").startswith(
                    "ReviewSensei immutable evidence part v1"
                ):
                    fault, self.carrier_fault = self.carrier_fault, None
                    if fault == "stored":
                        super().external(request, timeout)
                    else:
                        self.trace.append((self.attempt, request.method, path))
                    raise TimeoutError("synthetic uncertain carrier acknowledgement")
        return super().external(request, timeout)

    def prepare(self):
        old = parse_session_comment(
            self.comment["body"], identity=self.selected.identity
        )
        self.next_run()
        self.run_id = next(_RUN_IDS)
        self.event_name = "workflow_dispatch"
        self.source_previous = old
        self.comment["issue_url"] = "https://api.github.test/repos/owner/repo/issues/7"
        from review_sensei_github.api_run_host import review_startup_plan
        from review_sensei_github.api_run_requests import ApiRunRequestTransport
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.publication_recovery import _PaginationObservationHttp

        from review_sensei.api_work import ApiRunIdentity

        # This is a genuinely different run and its original first startup
        # slot. Ordinary rollover refusal is qualified separately; a consumed
        # failed startup cannot be reused or reset for creation.
        self.accounted = ApiRunRequestTransport(
            directory=self.directory,
            run=ApiRunIdentity(123, self.run_id),
            plan=review_startup_plan("hosted"),
            attempt=1,
            job="hosted",
            opener=self.external,
        )
        self.transports.append(self.accounted)
        self.http = _PaginationObservationHttp(
            GitHubHttp(api_url="https://api.github.test", opener=self.accounted.open),
            self.accounted.budget_view,
        )
        native = StartupRunLedger(
            self.http,
            caller=self.accounted,
            token="synthetic-bot",
            app_slug="reviewsensei[bot]",
            reservation_owner={"run_id": str(self.run_id), "head_sha": self.live_head},
        )
        assert native.load(old.identity()).record == old
        registration = {
            "schema_version": "quarantine-activation-v1",
            "repository": "owner/repo",
            "repository_id": 123,
            "pull_request": 7,
            "predecessor_comment_id": 1_000_000,
            "predecessor_record_sha256": old.record_sha256,
            "transaction_id": old.transaction.transaction_id,
            "head_sha": self.live_head,
            "base_sha": self.live_base,
            "workflow_id": self.run_id + 100,
            "workflow_path": ".github/workflows/review-sensei-quarantine.yml",
            "workflow_blob": "f" * 40,
            "run_number": 1,
        }
        native._quarantine_policy_branch = "main"
        native._quarantine_origin_reader = QuarantineOriginReader(
            metadata_token="synthetic-workflow-read",
            actions_token="synthetic-actions",
            current_scope=CurrentReadScope(
                native,
                old.identity(),
                self.live_base,
                self.live_head,
                "synthetic-workflow-read",
            ),
        )
        return native, old, registration


class QuarantineCreationTests(unittest.TestCase):
    def test_one_new_root_and_exact_readback_preserve_old_state_for_known_and_lost_ack(
        self,
    ):
        for fault in (None, "stored", "absent"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = CreationFixture(directory)
                original_objects(fixture)
                fixture.provider_comments = [
                    {
                        "path": "*",
                        "line": None,
                        "side": "FILE",
                        "body": "An original unresolved assessment.",
                        "severity": "high",
                        "blocking": True,
                    }
                ]
                self.assertEqual(fixture.invoke(), 1, fixture.error)
                native, old, registration = fixture.prepare()
                before_root = fixture.comment["body"]
                current = Path.cwd()
                try:
                    os.chdir(fixture.root)
                    with registered_resource([registration]):
                        slot = authorize_creation_slot(
                            http=fixture.http,
                            token="synthetic-actions",
                            metadata_token="synthetic-workflow-read",
                            identity=old.identity(),
                            run_id=fixture.run_id,
                            policy_commit=fixture.policy,
                            predecessor_comment_id=1_000_000,
                            event=event(registration),
                            actor="alice",
                            triggering_actor="alice",
                        )
                        native._quarantine_creation_slot = slot
                        prepared = prepare_quarantine(
                            ledger=native,
                            identity=old.identity(),
                            previous=old,
                            slot=slot,
                            configuration=FrozenApiConfiguration(
                                fixture.policy,
                                hashlib.sha256(fixture.config_bytes).hexdigest(),
                                512,
                            ),
                            owner=ApiExecutionOwner(1, "hosted", 0),
                            metadata_token="synthetic-workflow-read",
                            actions_token="synthetic-actions",
                        )
                        fixture.creation_fault = fault
                        if fault == "absent":
                            with self.assertRaises(ReviewInputError):
                                create_quarantine(
                                    prepared, actions_token="synthetic-actions"
                                )
                            self.assertIsNone(fixture.accounted._state)
                        else:
                            record = create_quarantine(
                                prepared, actions_token="synthetic-actions"
                            )
                            self.assertNotEqual(record.record_sha256, old.record_sha256)
                            self.assertEqual(
                                record.quarantine_lineage["predecessor_record_sha256"],
                                old.record_sha256,
                            )
                            self.assertEqual(
                                fixture.accounted._root, record.record_sha256
                            )
                            self.assertEqual(
                                record.completed_initial_reviews,
                                old.completed_initial_reviews,
                            )
                            self.assertIsNone(
                                record.api_run_accounting["terminal_transaction"]
                            )
                        with self.assertRaises(ReviewInputError):
                            create_quarantine(
                                prepared, actions_token="synthetic-actions"
                            )
                        self.assertEqual(fixture.root_posts, 1)
                        self.assertEqual(fixture.comment["body"], before_root)
                        self.assertIsNone(
                            old.api_run_accounting["terminal_transaction"]
                        )
                        self.assertFalse(
                            any(method == "PATCH" for _, method, _ in fixture.trace)
                        )
                finally:
                    os.chdir(current)


@contextmanager
def prepared_creation():
    with tempfile.TemporaryDirectory() as directory:
        fixture = CreationFixture(directory)
        original_objects(fixture)
        fixture.provider_comments = [
            {
                "path": "*",
                "line": None,
                "side": "FILE",
                "body": "An original unresolved assessment.",
                "severity": "high",
                "blocking": True,
            }
        ]
        assert fixture.invoke() == 1, fixture.error
        native, old, registration = fixture.prepare()
        previous = Path.cwd()
        try:
            os.chdir(fixture.root)
            with registered_resource([registration]):
                slot = authorize_creation_slot(
                    http=fixture.http,
                    token="synthetic-actions",
                    metadata_token="synthetic-workflow-read",
                    identity=old.identity(),
                    run_id=fixture.run_id,
                    policy_commit=fixture.policy,
                    predecessor_comment_id=1_000_000,
                    event=event(registration),
                    actor="alice",
                    triggering_actor="alice",
                )
                native._quarantine_creation_slot = slot
                prepared = prepare_quarantine(
                    ledger=native,
                    identity=old.identity(),
                    previous=old,
                    slot=slot,
                    configuration=FrozenApiConfiguration(
                        fixture.policy,
                        hashlib.sha256(fixture.config_bytes).hexdigest(),
                        512,
                    ),
                    owner=ApiExecutionOwner(1, "hosted", 0),
                    metadata_token="synthetic-workflow-read",
                    actions_token="synthetic-actions",
                )
                yield fixture, native, old, prepared
        finally:
            os.chdir(previous)


class QuarantineCreationFailureTests(unittest.TestCase):
    def test_uncertain_immutable_carrier_stops_before_root_and_never_retries(self):
        for fault in ("stored", "absent"):
            with (
                self.subTest(fault=fault),
                prepared_creation() as (fixture, native, old, prepared),
            ):
                before = len(fixture.trace)
                old_body = fixture.comment["body"]
                fixture.carrier_fault = fault
                with self.assertRaisesRegex(ReviewInputError, "uncertain"):
                    create_quarantine(prepared, actions_token="synthetic-actions")
                self.assertIsNone(fixture.accounted._state)
                self.assertEqual(fixture.root_posts, 0)
                self.assertEqual(
                    sum(method == "POST" for _, method, _ in fixture.trace[before:]), 1
                )
                self.assertEqual(fixture.comment["body"], old_body)
                with self.assertRaises(ReviewInputError):
                    create_quarantine(prepared, actions_token="synthetic-actions")

    def test_unfunded_complete_plan_or_revocation_fails_without_any_post(self):
        from review_sensei_github.quarantine_creation import plan_quarantine

        for failure in ("funding", "role", "head", "base", "policy", "storage"):
            with (
                self.subTest(failure=failure),
                prepared_creation() as (fixture, native, old, prepared),
            ):
                plan = plan_quarantine(prepared)
                if failure == "funding":
                    while (
                        fixture.accounted.budget_view.ordinary_remaining()
                        >= plan["requests"]
                    ):
                        fixture.http.request(
                            "GET",
                            fixture.http.repository_path(
                                "owner/repo", "/git/ref/heads/main"
                            ),
                            token="synthetic-workflow-read",
                        )
                elif failure == "role":
                    fixture.role = "write"
                elif failure == "head":
                    fixture.live_head = "e" * 40
                elif failure == "base":
                    fixture.live_base = "e" * 40
                elif failure == "policy":
                    fixture.policy_ref = "e" * 40
                else:
                    # External retained metadata fills the original finite store;
                    # no usage counter or source-owned reservation is fabricated.
                    for identifier in range(256):
                        fixture.extra_comments[8_000_000 + identifier] = {
                            "id": 8_000_000 + identifier,
                            "body": "bounded retained item\n<!-- reviewsensei:evidence-part:v1 -->",
                            "user": {
                                "id": 999,
                                "login": "reviewsensei[bot]",
                                "type": "Bot",
                            },
                            "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                        }
                    native.load(old.identity())
                before = len(fixture.trace)
                with self.assertRaises(ReviewInputError):
                    create_quarantine(prepared, actions_token="synthetic-actions")
                self.assertEqual(fixture.root_posts, 0)
                self.assertFalse(
                    any(method == "POST" for _, method, _ in fixture.trace[before:])
                )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])

    def test_missing_successor_refuses_both_existing_and_fresh_reader_without_fallback(
        self,
    ):
        from review_sensei_github.quarantine_selection import select_quarantine

        with prepared_creation() as (fixture, native, old, prepared):
            record = create_quarantine(prepared, actions_token="synthetic-actions")
            created = [
                key
                for key, item in fixture.extra_comments.items()
                if "reviewsensei:session:v2" in item["body"]
            ]
            self.assertEqual(len(created), 1)
            del fixture.extra_comments[created[0]]
            items = [fixture.comment, *fixture.extra_comments.values()]
            for ledger in (
                native,
                StartupRunLedger(
                    fixture.http,
                    caller=fixture.accounted,
                    token="synthetic-bot",
                    app_slug="reviewsensei[bot]",
                    reservation_owner={
                        "run_id": str(fixture.run_id),
                        "head_sha": fixture.live_head,
                    },
                ),
            ):
                with self.assertRaisesRegex(ReviewInputError, "fallback"):
                    select_quarantine(ledger, old.identity(), items, [(1_000_000, old)])
            self.assertEqual(fixture.accounted._root, record.record_sha256)
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            self.assertEqual(fixture.root_posts, 1)

    def test_prepared_sources_are_immutable_and_plan_covers_returned_id_width(self):
        from review_sensei_github.quarantine_creation import plan_quarantine

        with prepared_creation() as (fixture, native, old, prepared):
            with self.assertRaises(ReviewInputError):
                prepared.sources = b"{}"
            plan = plan_quarantine(prepared)
            before = fixture.accounted.startup_dispatches
            record = create_quarantine(prepared, actions_token="synthetic-actions")
            self.assertLessEqual(
                fixture.accounted.startup_dispatches - before, plan["requests"]
            )
            self.assertLessEqual(
                len(
                    native._render_comment(
                        repository_id=123, pull_request=7, record=record
                    ).encode()
                ),
                plan["root_bytes_upper_bound"],
            )


class QuarantinePrimingTests(unittest.TestCase):
    def test_genuine_preparation_cannot_prime_foreign_bindings_or_creation_metadata(
        self,
    ):
        from review_sensei_github.quarantine_creation import _archive, _candidate

        from review_sensei.quarantine_assessments import origin_scope

        for alteration in ("binding", "policy", "configuration", "run"):
            with (
                self.subTest(alteration=alteration),
                prepared_creation() as (fixture, native, old, prepared),
            ):
                # Consume the real preparation through a definite pre-effect
                # revocation. The positive slot remains unconsumed; no write or
                # synthetic accounting reset establishes this state.
                fixture.role = "write"
                with self.assertRaisesRegex(ReviewInputError, "revoked"):
                    create_quarantine(prepared, actions_token="synthetic-actions")
                fixture.role = "maintain"
                source = json.loads(prepared.sources)[0]
                binding = dict(source["binding"])
                if alteration == "binding":
                    binding["generation"] += 1
                source_manifest = fixture.seed(source["source"], binding, 1)
                inventory_manifest = fixture.seed(
                    source["inventory"],
                    binding,
                    len(source["inventory"]["assessments"]),
                )
                origin = {
                    "schema_version": "quarantine-assessment-origin-v1",
                    "kind": source["kind"],
                    "source_manifest": source_manifest,
                    "inventory_manifest": inventory_manifest,
                }
                origin["scope_sha256"] = origin_scope(origin)
                archive, archive_binding = _archive(prepared, [origin])
                if alteration == "policy":
                    archive["creation_policy_commit"] = "e" * 40
                elif alteration == "configuration":
                    archive["creation_configuration_digest"] = "e" * 64
                    archive_binding["configuration_digest"] = "e" * 64
                elif alteration == "run":
                    archive["creation_run_id"] += 1
                manifest = fixture.seed(archive, archive_binding, 1)
                record = _candidate(prepared, manifest, 1)
                reader = native._quarantine_origin_reader
                with self.assertRaisesRegex(ReviewInputError, "binding|creation"):
                    reader.prime_prepared(prepared, record, archive)
                native.load(old.identity())
                with self.assertRaises(ReviewInputError):
                    reader.read(native, old.identity(), record)
                self.assertEqual(fixture.root_posts, 0)
                self.assertIsNone(fixture.accounted._state)
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
