"""A positive Actions slot, never absence of another root, authorizes creation."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from copy import copy
from pathlib import Path
from unittest.mock import patch

from fake_github_http import json_response, make_http

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

IDENTITY = SessionIdentity("owner/repo", 7, repository_id=123)
HEAD, BASE, POLICY = "b" * 40, "a" * 40, "c" * 40


def registration():
    return {
        "schema_version": "quarantine-activation-v1",
        "repository": "owner/repo",
        "repository_id": 123,
        "pull_request": 7,
        "predecessor_comment_id": 41,
        "predecessor_record_sha256": "d" * 64,
        "transaction_id": "e" * 64,
        "head_sha": HEAD,
        "base_sha": BASE,
        "workflow_id": 700,
        "workflow_path": ".github/workflows/review-sensei-quarantine.yml",
        "workflow_blob": "f" * 40,
        "run_number": 1,
    }


def run():
    return {
        "id": 900,
        "repository": {"id": 123, "full_name": "owner/repo"},
        "workflow_id": 700,
        "run_number": 1,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "status": "in_progress",
        "path": ".github/workflows/review-sensei-quarantine.yml",
        "head_sha": POLICY,
        "actor": {"login": "alice", "type": "User", "id": 99, "node_id": "user-99"},
        "triggering_actor": {"login": "alice", "type": "User", "id": 99},
    }


def event(scope=None):
    scope = scope or registration()
    return {
        "repository": {"id": 123, "full_name": "owner/repo"},
        "sender": {"login": "alice", "type": "User", "id": 99},
        "inputs": {
            name: str(scope[name])
            for name in (
                "predecessor_comment_id",
                "predecessor_record_sha256",
                "transaction_id",
                "head_sha",
                "base_sha",
                "pull_request",
            )
        },
    }


def responses(actual=None, permission="maintain", blob="f" * 40):
    return [
        json_response(actual or run()),
        json_response({"sha": blob, "type": "file"}),
        json_response(
            {
                "permission": permission,
                "user": {"id": 99, "login": "alice", "type": "User"},
            }
        ),
    ]


@contextmanager
def registered_resource(rows=None):
    # The external installed-resource boundary is synthetic. The actual registry
    # reader, closed validation and authority issuer all execute unchanged.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "quarantine_activations.json").write_text(
            json.dumps(
                {
                    "schema_version": "quarantine-activation-registry-v1",
                    "registrations": rows if rows is not None else [registration()],
                }
            )
        )
        with patch(
            "review_sensei_github.quarantine_activation.files", return_value=root
        ):
            yield


class QuarantineActivationTests(unittest.TestCase):
    def test_v2_distinct_pinned_ordinals_share_one_workflow_without_reassignment(self):
        from review_sensei_github.quarantine_activation import (
            read_registry,
            require_registry_successor,
        )

        first = registration()
        second = {
            **first,
            "schema_version": "quarantine-activation-v2",
            "pull_request": 8,
            "predecessor_comment_id": 42,
            "run_number": 2,
        }

        def registry(rows):
            return json.dumps(
                {
                    "schema_version": "quarantine-activation-registry-v1",
                    "registrations": rows,
                }
            ).encode()

        self.assertEqual(read_registry(registry([first, second])), [first, second])
        self.assertEqual(
            require_registry_successor(registry([first]), registry([first, second])),
            [first, second],
        )
        for changed in ([], [second, first], [{**first, "run_number": 2}, second]):
            with self.subTest(changed=changed), self.assertRaises(ReviewInputError):
                require_registry_successor(registry([first, second]), registry(changed))
        for changed in (
            {**second, "run_number": 1},
            {**second, "predecessor_comment_id": 41, "pull_request": 7},
        ):
            with self.subTest(changed=changed), self.assertRaises(ReviewInputError):
                read_registry(registry([first, changed]))

    def test_v2_authority_requires_exact_registered_ordinal_and_attempt_one(self):
        scope = {
            **registration(),
            "schema_version": "quarantine-activation-v2",
            "run_number": 7,
        }
        with registered_resource([scope]):
            for number, attempt in ((1, 1), (6, 1), (8, 1), (7, 2)):
                http, calls = make_http(
                    responses(
                        actual={
                            **run(),
                            "id": 9900 + number + attempt,
                            "run_number": number,
                            "run_attempt": attempt,
                        }
                    )
                )
                with (
                    self.subTest(number=number, attempt=attempt),
                    self.assertRaises(ReviewInputError),
                ):
                    self.authorize(http, run_id=9900 + number + attempt)
                self.assertTrue(all(method == "GET" for method, _, _ in calls))
            http, _ = make_http(
                responses(actual={**run(), "id": 9950, "run_number": 7})
            )
            slot = self.authorize(http, run_id=9950)
            self.assertEqual(slot.registration, scope)
            slot.consume_creation(http=http, identity=IDENTITY, run_id=9950)
            again, _ = make_http(
                responses(actual={**run(), "id": 9950, "run_number": 7})
            )
            with self.assertRaises(ReviewInputError):
                self.authorize(again, run_id=9950)

    def authorize(self, http, **extra):
        from review_sensei_github.quarantine_activation import authorize_creation_slot

        return authorize_creation_slot(
            **{
                "http": http,
                "token": "synthetic-actions",
                "metadata_token": "synthetic-metadata",
                "identity": IDENTITY,
                "run_id": 900,
                "policy_commit": POLICY,
                "predecessor_comment_id": 41,
                "event": event(),
                "actor": "alice",
                "triggering_actor": "alice",
                **extra,
            }
        )

    def test_empty_installed_registry_or_unregistered_scope_never_issues_authority(
        self,
    ):
        for rows, inputs in (
            ([], event()),
            ([registration()], event({**registration(), "head_sha": "0" * 40})),
        ):
            with self.subTest(rows=bool(rows)), registered_resource(rows):
                http, calls = make_http(responses())
                with self.assertRaises(ReviewInputError):
                    self.authorize(http, event=inputs)
                self.assertEqual(calls, [])

    def test_plain_dictionary_cannot_be_supplied_as_registration_authority(self):
        with registered_resource([]):
            http, calls = make_http(responses())
            with self.assertRaises(TypeError):
                self.authorize(http, registration=registration())
            self.assertEqual(calls, [])

    def test_missing_metadata_wrong_source_and_revocation_never_issue_a_slot(self):
        cases = [
            [json_response({"message": "Not Found"}, 404)],
            responses(blob="0" * 40),
            responses(permission="write"),
            responses(
                actual={**run(), "actor": {"login": "alice", "type": "User", "id": 100}}
            ),
        ]
        for values in cases:
            with self.subTest(values=len(values)), registered_resource():
                http, calls = make_http(values)
                with self.assertRaises(ReviewInputError):
                    self.authorize(http)
                self.assertTrue(all(method == "GET" for method, _, _ in calls))

    def test_only_registered_first_execution_can_authorize_one_creation(self):
        from review_sensei_github.quarantine_activation import CreationSlot

        with registered_resource():
            http, calls = make_http(responses())
            slot = self.authorize(http)
            slot.require_process(http=http, identity=IDENTITY, run_id=900)
            self.assertEqual(slot.registration, registration())
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(method == "GET" for method, _, _ in calls))
            # Repeated issuance cannot replace a consumed or lost slot.
            again, _ = make_http(responses())
            with self.assertRaisesRegex(ReviewInputError, "already issued"):
                self.authorize(again)
            for candidate in (
                copy(slot),
                CreationSlot(
                    http=http,
                    identity=IDENTITY,
                    run_id=900,
                    registration=registration(),
                ),
            ):
                with self.assertRaisesRegex(ReviewInputError, "foreign"):
                    candidate.consume_creation(http=http, identity=IDENTITY, run_id=900)
            with self.assertRaisesRegex(ReviewInputError, "foreign"):
                slot.consume_creation(http=again, identity=IDENTITY, run_id=900)
            with self.assertRaisesRegex(ReviewInputError, "foreign"):
                slot.consume_creation(http=http, identity=IDENTITY, run_id=901)
            slot.consume_creation(http=http, identity=IDENTITY, run_id=900)
            with self.assertRaisesRegex(ReviewInputError, "consumed"):
                slot.consume_creation(http=http, identity=IDENTITY, run_id=900)

            # Missing/deleted first-run metadata creates no replacement chance.
            for changes in (
                {"run_number": 2},
                {"run_attempt": 2},
                {"workflow_id": 701},
                {"event": "pull_request"},
                {"head_sha": "0" * 40},
            ):
                with self.subTest(changes=changes):
                    other, dispatches = make_http([json_response({**run(), **changes})])
                    with self.assertRaisesRegex(ReviewInputError, "first execution"):
                        self.authorize(other)
                    self.assertTrue(all(method == "GET" for method, _, _ in dispatches))

    def test_source_and_role_reads_use_existing_metadata_capability_not_actions_token(
        self,
    ):
        with registered_resource():
            http, calls = make_http(responses(actual={**run(), "id": 902}))
            headers = []
            original = http.opener

            def observe(request, timeout):
                headers.append(request.get_header("Authorization"))
                return original(request, timeout)

            http.opener = observe
            self.authorize(http, run_id=902)
            self.assertEqual(len(calls), 3)
            self.assertEqual(
                headers,
                [
                    "Bearer synthetic-actions",
                    "Bearer synthetic-metadata",
                    "Bearer synthetic-metadata",
                ],
            )

    def test_maintain_projection_is_current_and_bound_to_numeric_actor(self):
        values = responses(actual={**run(), "id": 903}, permission="write")
        values[-1] = json_response(
            {
                "permission": "write",
                "role_name": "maintain",
                "user": {"id": 99, "login": "alice", "type": "User"},
            }
        )
        with registered_resource():
            http, calls = make_http(values)
            self.authorize(http, run_id=903)
            self.assertEqual(len(calls), 3)
        for user in ({"id": 100, "login": "alice", "type": "User"}, None):
            values = responses()
            values[-1] = json_response({"permission": "admin", "user": user})
            with (
                registered_resource(),
                self.subTest(user=user),
                self.assertRaises(ReviewInputError),
            ):
                http, calls = make_http(values)
                self.authorize(http)

    def test_revalidation_rechecks_revocation_and_never_reissues_creation_authority(
        self,
    ):
        values = responses(actual={**run(), "id": 904})
        values += responses(actual={**run(), "id": 904}, permission="write")
        with registered_resource():
            http, calls = make_http(values)
            slot = self.authorize(http, run_id=904)
            with self.assertRaises(ReviewInputError):
                slot.revalidate(
                    http=http,
                    identity=IDENTITY,
                    run_id=904,
                    token="synthetic-actions",
                    metadata_token="synthetic-metadata",
                    policy_commit=POLICY,
                )
            self.assertEqual(len(calls), 6)
            self.assertTrue(all(method == "GET" for method, _, _ in calls))
            with self.assertRaises(ReviewInputError):
                slot.revalidate(
                    http=http,
                    identity=IDENTITY,
                    run_id=904,
                    token="synthetic-actions",
                    metadata_token="synthetic-metadata",
                    policy_commit="0" * 40,
                )
            self.assertEqual(len(calls), 6)
