"""Isolated receipt data/reader tests; no historical execution grant."""

import copy
import unittest
from dataclasses import replace

from review_sensei_github.errors import GitHubHTTPResponseTooLargeError
from review_sensei_github.quarantine_census import _comment
from review_sensei_github.quarantine_command_receipt import (
    _membership_binding,
    freeze_receipt_membership,
    project_receipt_page,
    read_receipt_page,
)
from test_quarantine_census import IDENTITY, CensusBackend

from review_sensei.errors import ReviewInputError


class ReceiptGraphQLTests(unittest.TestCase):
    def prepare(self):
        backend = CensusBackend(25, 12500)
        ledger = backend.ledger()
        rows = [_comment(node, ledger, IDENTITY) for node in backend.rows]
        kwargs = dict(
            mutable_bounds=((rows[0]["id"], 65763),), future_bounds=(2000, 65536)
        )
        with self.assertRaisesRegex(ReviewInputError, "single page exceeds wire"):
            project_receipt_page(rows, **kwargs)
        projection = project_receipt_page(
            rows, **kwargs, allow_graphql=True, identity=IDENTITY
        )
        self.assertEqual(projection.protocol, "graphql")
        self.assertEqual(projection.first, 28)
        backend.rows[0]["body"] = "confirmed current root"
        backend.rows.append(backend.node(25, "own event"))
        final = [_comment(node, ledger, IDENTITY) for node in backend.rows]
        membership = freeze_receipt_membership(
            final,
            projection=projection,
            receipt_text="exact own receipt <!-- marker -->",
            update_id=None,
            app_id=317764649,
        )
        return backend, ledger, projection, membership

    def read(self, backend, ledger, membership, reconcile=False):
        return read_receipt_page(
            ledger.http,
            path="/repos/owner/repo/issues/7/comments",
            token="synthetic",
            membership=membership,
            reconcile=reconcile,
            ledger=ledger,
            identity=IDENTITY,
        )

    def test_complete_fresh_read_and_stored_or_absent_receipt_loss(self):
        backend, ledger, projection, membership = self.prepare()
        self.assertEqual(len(self.read(backend, ledger, membership)), 26)
        self.assertEqual(backend.calls[0]["first"], 28)
        # Absent effect gives unchanged complete data; original writer still
        # finds zero matching receipts and must hold (never replay the POST).
        self.assertFalse(
            any(
                row["body"] == membership.receipt_text
                for row in self.read(backend, ledger, membership, True)
            )
        )
        backend.rows.append(backend.node(26, membership.receipt_text))
        rows = self.read(backend, ledger, membership, True)
        self.assertEqual(sum(row["body"] == membership.receipt_text for row in rows), 1)
        self.assertEqual(len(backend.calls), 3)
        self.assertEqual(projection.future, (2000, 65536))

    def test_bad_complete_responses_refuse_after_one_request_without_retry(self):
        for mutation in (
            "unknown",
            "body",
            "owner",
            "missing",
            "duplicate",
            "prefix",
            "scope",
            "metadata",
            "oversize",
        ):
            with self.subTest(mutation=mutation):
                backend, ledger, _, membership = self.prepare()
                if mutation == "unknown":
                    backend.rows.append(backend.node(26, "foreign"))
                elif mutation == "body":
                    backend.rows[1]["body"] = "changed"
                elif mutation == "owner":
                    backend.rows[1]["author"]["databaseId"] += 1
                elif mutation == "missing":
                    backend.rows.pop()
                elif mutation == "duplicate":
                    backend.rows[-1] = copy.deepcopy(backend.rows[0])
                elif mutation == "prefix":
                    backend.change = lambda response, connection, count: connection[
                        "pageInfo"
                    ].update(hasNextPage=True)
                elif mutation == "scope":
                    backend.rows[0]["pullRequest"]["number"] = 8
                elif mutation == "metadata":
                    backend.rows[0]["extra"] = "x" * 1100
                else:
                    backend.rows[0]["body"] = "x" * 524288
                with self.assertRaises(
                    (ReviewInputError, GitHubHTTPResponseTooLargeError)
                ):
                    self.read(backend, ledger, membership)
                self.assertEqual(len(backend.calls), 1)

    def test_protocol_copy_and_wrong_scope_refuse_before_io(self):
        backend, ledger, _, membership = self.prepare()
        for forged in (
            replace(membership, protocol="rest"),
            replace(membership, scope=("other/repo", 7, 123)),
            replace(membership, first=101),
        ):
            with self.assertRaises(ReviewInputError):
                self.read(backend, ledger, forged)
            self.assertNotEqual(
                _membership_binding(membership),
                (
                    forged.bodies,
                    forged.owners,
                    forged.receipt_text,
                    forged.receipt_update_id,
                    forged.app_id,
                    forged.maximum_bytes,
                    forged.protocol,
                    forged.scope,
                    forged.first,
                ),
            )
        self.assertEqual(backend.calls, [])

    def test_only_exact_own_receipt_may_change_during_reconciliation(self):
        for body, app_id in (
            ("foreign", 317764649),
            ("exact own receipt <!-- marker -->", 42),
        ):
            backend, ledger, _, membership = self.prepare()
            node = backend.node(26, body)
            node["author"]["databaseId"] = app_id
            backend.rows.append(node)
            with self.assertRaises(ReviewInputError):
                self.read(backend, ledger, membership, True)
            self.assertEqual(len(backend.calls), 1)

    def test_opt_in_preserves_small_rest_and_refuses_large_future_envelope(self):
        backend = CensusBackend(1, 20)
        ledger = backend.ledger()
        rows = [_comment(node, ledger, IDENTITY) for node in backend.rows]
        self.assertEqual(
            project_receipt_page(rows, allow_graphql=True, identity=IDENTITY).protocol,
            "rest",
        )
        with self.assertRaisesRegex(ReviewInputError, "GraphQL page exceeds"):
            project_receipt_page(
                rows, future_bounds=(524288,), allow_graphql=True, identity=IDENTITY
            )
        with self.assertRaisesRegex(ReviewInputError, "item bound"):
            project_receipt_page(
                rows, future_bounds=(1,) * 99, allow_graphql=True, identity=IDENTITY
            )

    def test_late_nested_membership_poison_never_calls_equality_or_hash(self):
        callbacks = []

        class Poison:
            def __eq__(self, other):
                callbacks.append("eq")
                return True

            def __hash__(self):
                callbacks.append("hash")
                return 0

            def __len__(self):
                callbacks.append("len")
                return 1

        for field in (
            "bodies-id",
            "bodies-digest",
            "owners-id",
            "owners-login",
            "receipt_text",
            "scope",
        ):
            backend, ledger, _, membership = self.prepare()
            poison = Poison()

            def change(response, connection, count):
                if field == "bodies-id":
                    object.__setattr__(
                        membership,
                        "bodies",
                        ((poison, membership.bodies[0][1]),) + membership.bodies[1:],
                    )
                elif field == "bodies-digest":
                    object.__setattr__(
                        membership,
                        "bodies",
                        ((membership.bodies[0][0], poison),) + membership.bodies[1:],
                    )
                elif field == "owners-id":
                    object.__setattr__(
                        membership,
                        "owners",
                        ((membership.owners[0][0], (poison, "Bot", None)),)
                        + membership.owners[1:],
                    )
                elif field == "owners-login":
                    object.__setattr__(
                        membership,
                        "owners",
                        ((membership.owners[0][0], (317764649, "Bot", poison)),)
                        + membership.owners[1:],
                    )
                elif field == "scope":
                    object.__setattr__(membership, "scope", (poison, 7, 123))
                else:
                    object.__setattr__(membership, field, poison)

            backend.change = change
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                self.read(backend, ledger, membership)
            self.assertEqual(callbacks, [])
            self.assertEqual(len(backend.calls), 1)

    def test_identity_keys_and_export_refuse_without_callbacks(self):
        from review_sensei_github.quarantine_command_receipt import _receipt_scope

        from review_sensei import session

        callbacks = []

        class Key:
            def __hash__(self):
                return hash("repository")

            def __eq__(self, other):
                callbacks.append("key-eq")
                return False

        identity = session.SessionIdentity("owner/repo", 7, 123)
        vars(identity)[Key()] = 42
        callbacks.clear()
        with self.assertRaisesRegex(ReviewInputError, "identity fields"):
            _receipt_scope(identity)
        self.assertEqual(callbacks, [])

        class Export:
            def __getattribute__(self, name):
                callbacks.append("export-get")
                return object.__getattribute__(self, name)

        original = session.SessionIdentity
        session.SessionIdentity = Export()
        try:
            with self.assertRaises(ReviewInputError):
                _receipt_scope(IDENTITY)
            self.assertEqual(callbacks, [])
        finally:
            session.SessionIdentity = original
        self.assertEqual(_receipt_scope(IDENTITY), ("owner/repo", 7, 123))

    def test_original_rest_nullable_owner_shapes_remain_valid(self):
        from types import SimpleNamespace

        rows = [
            {"id": 1, "body": "retained deleted author", "user": None},
            {"id": 2, "body": "retained bot", "user": {"id": 999, "type": "Bot"}},
        ]
        projection = project_receipt_page(rows, future_bounds=(65536,))
        membership = freeze_receipt_membership(
            rows,
            projection=projection,
            receipt_text="own receipt",
            update_id=None,
            app_id=999,
        )
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path))
            return 200, copy.deepcopy(rows)

        http = SimpleNamespace(request=request)
        self.assertEqual(
            read_receipt_page(
                http,
                path="/repos/owner/repo/issues/7/comments",
                token="synthetic",
                membership=membership,
            ),
            rows,
        )
        self.assertEqual(
            calls, [("GET", "/repos/owner/repo/issues/7/comments?per_page=100&page=1")]
        )
