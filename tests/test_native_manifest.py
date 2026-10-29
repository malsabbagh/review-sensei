"""Manifest framing and conflicting owned sources never reveal a clean subset."""

import copy
import hashlib
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.native_manifest import (
    MANIFEST_WIRE_BOUND,
    NativePublicationReader,
    manifest_body,
    read_manifest_body,
)
from review_sensei_github.native_union import NativeUnionReceipt
from review_sensei_github.packed_comment_census import wire
from test_native_group_publication import GroupWriterBackend

from review_sensei.errors import ReviewInputError


class NativeManifestTests(unittest.TestCase):
    def fixture(self):
        backend = GroupWriterBackend()
        state = backend.host.publish_parts()
        receipt = NativeUnionReceipt.from_staging(backend.plan, state, producer_id=999)
        store = backend.host.manifest_store
        store.create(receipt)
        return backend, receipt, store

    def test_cold_selection_binds_original_transaction_and_full_source(self):
        backend, receipt, store = self.fixture()
        selected = store.select(
            backend.result.transaction, result_digest=receipt.result_digest
        )
        self.assertEqual(selected[0], receipt)
        self.assertEqual(selected[1].native_id, "issue:1234000")
        original = backend.result.transaction
        other_identity = {
            name: getattr(original, name)
            for name in (
                "repository",
                "pull_request",
                "base_sha",
                "head_sha",
                "policy_digest",
                "configuration_digest",
                "evidence_digest",
                "reservation_id",
            )
        }
        other_identity["head_sha"] = "f" * 40
        other = replace(
            original,
            **other_identity,
            transaction_id=original.derive_id(**other_identity),
        )
        for transaction, digest in (
            (other, receipt.result_digest),
            (backend.result.transaction, "0" * 64),
        ):
            with self.assertRaises(ReviewInputError):
                store.select(transaction, result_digest=digest)
        extra = copy.deepcopy(backend.manifest_comment)
        extra["id"] += 1
        extra["body"] = manifest_body(receipt, "b" * 64)
        backend.extra_manifest_comments.append(extra)
        with self.assertRaises(ReviewInputError):
            store.select(
                backend.result.transaction, result_digest=receipt.result_digest
            )
        self.assertEqual(backend.manifest_posts, 1)

    def test_cold_reader_composes_selected_source_admission_manifest_and_fresh_union(
        self,
    ):
        # Isolate the already authenticated checkpoint/admission boundary. The
        # manifest and full native union use actual bounded HTTP readers. This
        # is a component composition, not a protected public-caller receipt.
        from review_sensei_github.default_human_inventory import HumanInventoryStore
        from review_sensei_github.publication import (
            finding_index_marker,
            review_result_digest,
        )

        from review_sensei.bounded_evidence import canonical_bytes

        backend = GroupWriterBackend()
        original = backend.result
        tx = original.transaction
        source_reference = {"original_physical_checkpoint": 17}
        source = canonical_bytes(original.to_dict())
        backend.host.manifest_store = replace(
            backend.host.manifest_store,
            source_sha256=hashlib.sha256(canonical_bytes(source_reference)).hexdigest(),
        )
        index = finding_index_marker(
            repository_id=backend.plan.repository_id,
            pull_request=backend.plan.pull_request,
            base_sha=tx.base_sha,
            head_sha=tx.head_sha,
            result_digest=review_result_digest(original),
            entries=backend.assembled.index_entries,
            complete_inventory=True,
        )
        state, _ = backend.host.publish_final(
            body_for_receipt=lambda receipt: (
                backend.body_for_receipt(receipt) + "\n" + index
            ),
            event="COMMENT",
            old_root_nodes={},
        )
        receipt = NativeUnionReceipt.from_staging(backend.plan, state, producer_id=999)
        store = object.__new__(HumanInventoryStore)
        store.http, store.token = backend.http, "synthetic"
        store.ledger = backend.ledger
        from test_review_transaction import IDENTITY, NOW

        store.transaction_after_read = lambda: (
            backend.ledger.load(IDENTITY, now=NOW).record
        )
        store.port = backend.host.manifest_store.port
        store.context = SimpleNamespace(
            repository=backend.host.identity.repository,
            repository_id=receipt.repository_id,
            pull_request=receipt.pull_request,
            producer_id=999,
            app_slug="reviewsensei[bot]",
            head_sha=tx.head_sha,
            base_sha=tx.base_sha,
            transaction_id=tx.transaction_id,
            original_content_digest=original.content_digest(),
            publication_digest=receipt.result_digest,
        )
        store._checkpoint_source = lambda: (copy.deepcopy(source_reference), source)
        store.read_available = lambda: (
            None,
            {
                "finding_index": index,
                "human_inventory": None,
                "visible_prose": [],
            },
        )
        fences = []
        store.revalidate_snapshot = lambda: fences.append(True)
        review = {
            "id": backend.main_id,
            "state": "COMMENTED",
            "body": backend.main_body,
            "commit_id": tx.head_sha,
            "user": {"id": 999, "type": "Bot", "login": "reviewsensei[bot]"},
            "pull_request_url": backend.http.api_url
            + backend.http.repository_path(
                store.context.repository, f"/pulls/{receipt.pull_request}"
            ),
        }
        with patch(
            "review_sensei_github.checkpoint_result_store.uses_grouped_publication",
            return_value=True,
        ):
            reader = NativePublicationReader.for_review(store, review)
            readback = reader(receipt)
            receipt.require_evidence(readback)
            self.assertEqual(len(readback.roots), 2)
            self.assertEqual(fences, [True])
            # Even a complete physical replacement carrying the same body and
            # parts cannot replace the original durable final review ID.
            original_id = backend.main_id
            backend.main_id += 1
            alternate = {**review, "id": backend.main_id}
            with self.assertRaisesRegex(ReviewInputError, "final review identity"):
                NativePublicationReader.for_review(store, alternate)(receipt)
            backend.main_id = original_id
            # A legitimate current body update keeps the original part union.
            backend.main_body += "\nCurrent authority annotation."
            review["body"] = backend.main_body
            NativePublicationReader.for_review(store, review)(receipt)
            self.assertEqual(len(fences), 2)
            source_reference["original_physical_checkpoint"] = 18
            with self.assertRaises(ReviewInputError):
                NativePublicationReader.for_review(store, review)(receipt)
            self.assertEqual(backend.post_count, 2)
            self.assertEqual(backend.manifest_posts, 1)
        with patch(
            "review_sensei_github.checkpoint_result_store.uses_grouped_publication",
            return_value=False,
        ):
            with self.assertRaisesRegex(ReviewInputError, "checkpoint conflicts"):
                reader(receipt)

    def test_native_cleanup_freshly_rejects_changed_main_bytes_before_resolution(self):
        from fake_github_http import json_response
        from review_sensei_github.default_human_reader import (
            DefaultHumanInventoryReader,
        )
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.publication import (
            ReviewApprovalFinalizer,
            approval_eligibility_from_body,
            finding_index_marker,
            parse_finding_index,
        )
        from review_sensei_github.thread_resolution import (
            FindingThreadResolver,
            ThreadResolutionResult,
        )
        from test_review_transaction import IDENTITY, NOW

        from review_sensei.bounded_evidence import canonical_bytes

        backend = GroupWriterBackend()
        _, evidence = backend.publish_final()
        eligibility = approval_eligibility_from_body(
            backend.main_body, native_union_reader=lambda receipt: evidence
        )
        index = parse_finding_index(
            finding_index_marker(
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                base_sha=backend.plan.base_sha,
                head_sha=backend.plan.head_sha,
                result_digest=eligibility.result_digest,
                entries=backend.assembled.index_entries,
                complete_inventory=True,
            ),
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            head_sha=backend.plan.head_sha,
        )
        expected = {
            "id": backend.main_id,
            "body": backend.main_body,
            "state": "COMMENTED",
            "commit_id": backend.plan.head_sha,
            "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/7",
        }
        reader = object.__new__(DefaultHumanInventoryReader)
        reader.store = SimpleNamespace(
            transaction_after_read=lambda: backend.ledger.load(IDENTITY, now=NOW).record
        )
        reader._native_root_read = (canonical_bytes(expected), evidence)
        for field, changed in (
            ("body", backend.main_body + " changed"),
            ("commit_id", "f" * 40),
            ("state", "DISMISSED"),
            ("user", {"id": 1000, "login": "reviewsensei[bot]", "type": "Bot"}),
            ("pull_request_url", "https://api.github.test/repos/owner/repo/pulls/8"),
        ):
            actual = {**expected, field: changed}
            requests = []

            def opened(request, timeout):
                requests.append((request.get_method(), request.full_url))
                self.assertEqual(request.get_method(), "GET")
                self.assertTrue(
                    request.full_url.endswith(f"/reviews/{backend.main_id}")
                )
                return json_response(actual)

            finalizer = ReviewApprovalFinalizer(
                http=GitHubHttp(api_url="https://api.github.test", opener=opened),
                human_inventory_reader=reader,
            )

            def resolve(_resolver, **kwargs):
                self.assertFalse(kwargs["fence"]())
                return ThreadResolutionResult(unconfirmed=tuple(kwargs["instances"]))

            with (
                self.subTest(field=field),
                patch.object(FindingThreadResolver, "resolve", resolve),
            ):
                result = finalizer.reconcile_decided_threads(
                    token="synthetic",
                    repository=IDENTITY.repository,
                    repository_id=IDENTITY.repository_id,
                    pull_request=IDENTITY.pull_request,
                    head_sha=backend.plan.head_sha,
                    app_slug="reviewsensei[bot]",
                    eligibility=eligibility,
                    index=index,
                    reviews=[expected],
                    honored_instances=frozenset(
                        entry.instance for entry in index.entries
                    ),
                )
                self.assertTrue(result.unconfirmed)
                self.assertEqual(len(requests), 1)

    def test_complete_round_trip_is_original_source_bound_and_small(self):
        backend, receipt, store = self.fixture()
        body = backend.manifest_comment["body"]
        self.assertEqual(
            read_manifest_body(body, source_sha256=store.source_sha256), receipt
        )
        self.assertLessEqual(wire({"body": body}), MANIFEST_WIRE_BOUND)
        found = store.find(receipt)
        self.assertEqual(found.native_id, "issue:1234000")
        for changed in (body + " ", body[:-1], body.replace("immutable", "other", 1)):
            with (
                self.subTest(changed=changed[:80]),
                self.assertRaises(ReviewInputError),
            ):
                read_manifest_body(changed, source_sha256=store.source_sha256)
        with self.assertRaises(ReviewInputError):
            read_manifest_body(body, source_sha256="b" * 64)

    def test_duplicate_conflicting_and_malformed_owned_frames_withhold(self):
        for kind in ("duplicate", "source", "parts", "malformed"):
            backend, receipt, store = self.fixture()
            extra = copy.deepcopy(backend.manifest_comment)
            extra["id"] += 1
            if kind == "source":
                extra["body"] = manifest_body(receipt, "b" * 64)
            elif kind == "parts":
                altered = replace(
                    receipt, parts=(replace(receipt.parts[0], native_id="42"),)
                )
                extra["body"] = manifest_body(altered, store.source_sha256)
            elif kind == "malformed":
                extra["body"] = extra["body"][:-2]
            backend.extra_manifest_comments.append(extra)
            with (
                self.subTest(kind=kind),
                self.assertRaises((ReviewInputError, GitHubPublicationTransientError)),
            ):
                store.find(receipt)
            self.assertEqual(backend.manifest_posts, 1)

    def test_wrong_receipt_scope_refuses_before_another_native_effect(self):
        backend, receipt, store = self.fixture()
        for altered in (
            replace(receipt, repository_id=2),
            replace(receipt, pull_request=8),
            replace(receipt, producer_id=777),
        ):
            with self.subTest(altered=altered), self.assertRaises(ReviewInputError):
                store.create(altered)
        self.assertEqual(backend.manifest_posts, 1)
