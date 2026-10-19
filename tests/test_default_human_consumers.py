"""Default reference eligibility preserves complete inventory and ordinary policy."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from copy import deepcopy
from dataclasses import replace

from review_sensei_github.approval import ApprovalFacts, ReviewApprovalEligibility

from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    HumanInventoryResolution,
    HumanReviewFinding,
    PendingHumanReview,
)

BASE = "a" * 40
HEAD = "b" * 40
RESULT = "c" * 64


def rich_inventory() -> PendingHumanReview:
    """Synthetic unique text prevents compression from hiding the legacy ceiling."""
    return PendingHumanReview(
        BASE,
        tuple(
            HumanReviewFinding(
                hashlib.sha256(f"finding:{index}".encode()).hexdigest(),
                f"src/file{index}.py",
                " ".join(
                    hashlib.sha256(f"text:{index}:{part}".encode()).hexdigest()
                    for part in range(8)
                ),
            )
            for index in range(250)
        ),
    )


def facts(*, pending: bool = True, **changes) -> ApprovalFacts:
    value = ApprovalFacts(
        enabled=True,
        app_authored=False,
        review_status="complete",
        evidence_policy="confirmed",
        has_blocking_findings=False,
        has_human_adjudication_findings=pending,
        coverage_blocker=None,
        qualification="not-required",
    )
    return replace(value, **changes)


def reference_document() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "purpose": "default-human-inventory",
        "event_key": "d" * 64 + "\t" + "e" * 64,
        "payload_digest": "f" * 64,
    }


def document(inventory: PendingHumanReview) -> dict[str, object]:
    return {
        "schema_version": "5",
        "head_sha": HEAD,
        "result_digest": RESULT,
        "facts": facts(pending=bool(inventory.pending)).to_dict(),
        "human_inventory_reference": reference_document(),
        "human_resolution": HumanInventoryResolution.from_inventory(
            inventory
        ).to_dict(),
    }


class DefaultHumanEligibilityTests(unittest.TestCase):
    def test_reference_document_requires_independent_reader(self):
        with self.assertRaisesRegex(ReviewInputError, "reader"):
            ReviewApprovalEligibility.from_dict(document(rich_inventory()))

    def test_rich_inventory_roundtrip_partial_and_complete_preserve_policy(self):
        original = rich_inventory()
        with self.assertRaises(ReviewInputError):
            original.to_dict()
        seen = []

        def read(reference):
            seen.append(reference.to_dict())
            return original

        for count in (0, 1, 249, 250):
            with self.subTest(resolved=count):
                current = replace(
                    original,
                    resolved=tuple(
                        sorted(item.fingerprint for item in original.findings[:count])
                    ),
                )
                wire = document(current)
                parsed = ReviewApprovalEligibility.from_dict(
                    wire, human_inventory_reader=read
                )
                self.assertEqual(parsed.human_review, current)
                self.assertEqual(parsed.to_dict(), wire)
                marker_size = len("<!-- reviewsensei:eligibility:v1  -->") + len(
                    base64.urlsafe_b64encode(
                        json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
                    )
                )
                self.assertLess(marker_size, 65_536)
                self.assertIsNone(parsed.partitioned_root)
                self.assertEqual(parsed.facts.qualification, "not-required")
                decision = parsed.evaluate(
                    app_authored=False, has_open_review_threads=False
                )
                self.assertEqual(decision.approved, count == 250)
        self.assertEqual(seen, [reference_document()] * 4)

    def test_all_resolved_reference_preserves_both_canonical_eligible_qualification_states(
        self,
    ):
        inventory = rich_inventory()
        current = replace(
            inventory,
            resolved=tuple(sorted(item.fingerprint for item in inventory.findings)),
        )
        for qualification in ("qualified", "not-required"):
            with self.subTest(qualification=qualification):
                wire = document(current)
                wire["facts"]["qualification"] = qualification
                restored = ReviewApprovalEligibility.from_dict(
                    wire, human_inventory_reader=lambda _reference: inventory
                )
                self.assertEqual(restored.facts.qualification, qualification)
                self.assertTrue(
                    restored.evaluate(
                        app_authored=False, has_open_review_threads=False
                    ).approved
                )
                self.assertEqual(restored.to_dict(), wire)

    def test_reference_resolution_cannot_change_or_expand_inventory(self):
        inventory = rich_inventory()
        for change in (
            {"inventory_digest": "0" * 64},
            {"resolved": ["0" * 64]},
            {"resolved": [inventory.findings[0].fingerprint] * 2},
        ):
            with self.subTest(change=change):
                wire = document(inventory)
                wire["human_resolution"].update(change)
                with self.assertRaises(ReviewInputError):
                    ReviewApprovalEligibility.from_dict(
                        wire, human_inventory_reader=lambda _ref: inventory
                    )

    def test_reference_facts_cannot_clear_pending_without_resolution(self):
        inventory = rich_inventory()
        wire = document(inventory)
        wire["facts"]["has_human_adjudication_findings"] = False
        with self.assertRaises(ReviewInputError):
            ReviewApprovalEligibility.from_dict(
                wire, human_inventory_reader=lambda _ref: inventory
            )

    def test_reference_schema_rejects_extra_missing_and_bad_identity(self):
        inventory = rich_inventory()
        original = document(inventory)
        variants = [
            {**original, "human_review": {}},
            {
                key: value
                for key, value in original.items()
                if key != "human_resolution"
            },
            {**original, "head_sha": "not-a-head"},
            {**original, "result_digest": "0" * 63},
            {**original, "human_inventory_reference": {}},
        ]
        for wire in variants:
            with self.subTest(wire=wire):
                with self.assertRaises(ReviewInputError):
                    ReviewApprovalEligibility.from_dict(
                        wire, human_inventory_reader=lambda _ref: inventory
                    )

    def test_all_resolved_does_not_clear_unrelated_policy_blockers(self):
        inventory = rich_inventory()
        inventory = replace(
            inventory, resolved=tuple(item.fingerprint for item in inventory.findings)
        )
        for changes in (
            {"has_blocking_findings": True},
            {"review_status": "partial"},
            {"coverage_blocker": "partial"},
            {"qualification": "missing"},
            {"qualification": "unverified"},
        ):
            with self.subTest(changes=changes):
                wire = deepcopy(document(inventory))
                wire["facts"].update(changes)
                parsed = ReviewApprovalEligibility.from_dict(
                    wire, human_inventory_reader=lambda _ref: inventory
                )
                self.assertFalse(
                    parsed.evaluate(
                        app_authored=False, has_open_review_threads=False
                    ).approved
                )
                self.assertEqual(parsed.to_dict()["facts"], wire["facts"])


class DefaultHumanConsumerTests(unittest.TestCase):
    def state(self, *, extra_human=True, **fact_changes):
        from review_sensei_github.default_human_inventory import HumanInventoryReference

        from tests.test_human_assessment import State, prior

        original = prior(extra_human=extra_human, **fact_changes)
        reference = HumanInventoryReference.from_dict(reference_document())
        original = replace(original, human_inventory_reference=reference)
        state = State(original)
        seen = []

        def reader(actual):
            self.assertEqual(actual, reference)
            seen.append(actual)
            return original.human_review

        return state, reader, seen

    def assessment(self, state, reader, *, target_path="src/app.py"):
        from review_sensei_github.human_assessment import HumanAssessmentPublisher

        from review_sensei.human_assessment import HumanAssessmentService
        from tests.test_human_assessment import APP, HUMAN, Provider, response_for

        publisher = HumanAssessmentPublisher(
            http=state.http, human_inventory_reader=reader
        )
        prepared = publisher.prepare(
            token="read",
            repository="owner/repo",
            pull_request=1,
            prepared=replace(
                state.prepared(), source_updated_at=state.source["updated_at"]
            ),
            app_slug=APP,
        )
        payload = response_for(state.eligibility)
        finding = next(
            item
            for item in prepared.eligibility.human_review.pending
            if item.path == target_path
        )
        payload["assessments"][0]["fingerprint"] = finding.fingerprint
        reply = HumanAssessmentService(Provider(payload)).reply(
            context=prepared.conversation.context,
            pending=prepared.eligibility.human_review,
            source_body=HUMAN,
        )
        return publisher.publish(
            token="issue",
            review_token="review",
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        ), publisher

    def test_public_assessment_preserves_reference_across_partial_and_fresh_owner(self):
        from tests.test_human_assessment import APP, HEAD

        state, reader, seen = self.state()
        first, publisher = self.assessment(state, reader)
        self.assertIs(publisher.finalizer, publisher.conversation.finalizer)
        self.assertEqual(first.status, "replied")
        self.assertNotEqual(first.approval_status, "approved")
        self.assertEqual(state.events(), ["COMMENT"])
        current = publisher.finalizer.load_eligibility(
            token="read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        self.assertEqual(len(current.human_review.pending), 1)
        self.assertIsNotNone(current.human_inventory_reference)
        self.assertEqual(current.facts.qualification, "not-required")
        before = len(seen)
        # An edited, newly snapshotted authorized mention supplies fresh source
        # authority. The new publisher has no retained in-process inventory.
        state.source["updated_at"] = "second-authorized-snapshot"
        second, fresh = self.assessment(state, reader, target_path="src/other.py")
        self.assertIsNot(fresh, publisher)
        self.assertGreater(len(seen), before)
        self.assertEqual(second.approval_status, "approved")
        self.assertEqual(state.events(), ["COMMENT", "COMMENT", "APPROVE"])
        final = fresh.finalizer.load_eligibility(
            token="read",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
        )
        self.assertFalse(final.human_review.pending)
        self.assertEqual(
            final.human_inventory_reference, current.human_inventory_reference
        )

    def test_public_current_reply_uses_reader_on_every_pending_fence(self):
        from review_sensei_github.conversation import ConversationPublisher

        from review_sensei.models import ConversationReply
        from tests.test_human_assessment import APP, HEAD

        state, reader, seen = self.state()
        publisher = ConversationPublisher(
            http=state.http, human_inventory_reader=reader
        )
        outcome = publisher.publish_current(
            token="issue",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            head_sha=HEAD,
            reply=ConversationReply(
                "Please retain the complete pending inventory.", False
            ),
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
        )
        self.assertEqual(outcome.status, "replied")
        self.assertGreater(len(seen), 1)
        self.assertEqual(state.events(), [])
        report = "\n".join(item["body"] for item in state.replies)
        for finding in state.eligibility.human_review.findings:
            self.assertIn(finding.fingerprint, report)

    def test_unavailable_reader_stops_public_prepare_before_writes(self):
        from review_sensei_github.errors import GitHubPublicationError
        from review_sensei_github.human_assessment import HumanAssessmentPublisher

        from tests.test_human_assessment import APP

        state, _, _ = self.state()

        def unavailable(_reference):
            raise ReviewInputError("complete inventory is unavailable")

        publisher = HumanAssessmentPublisher(
            http=state.http, human_inventory_reader=unavailable
        )
        with self.assertRaises(GitHubPublicationError):
            publisher.prepare(
                token="read",
                repository="owner/repo",
                pull_request=1,
                prepared=state.prepared(),
                app_slug=APP,
            )
        self.assertFalse(any(method != "GET" for method, _, _ in state.calls))
        self.assertEqual(state.events(), [])

    def test_scope_factory_is_lazy_for_legacy_and_shares_original_budget(self):
        from review_sensei_github.publication import ReviewApprovalFinalizer

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from tests.test_human_assessment import APP, HEAD, State, prior

        legacy = State(prior())
        calls = []

        def forbidden_factory(**scope):
            calls.append(scope)
            raise AssertionError("legacy inventory must not invoke the new reader")

        legacy_finalizer = ReviewApprovalFinalizer(
            http=legacy.http, human_inventory_reader_factory=forbidden_factory
        )
        self.assertEqual(
            legacy_finalizer.load_eligibility(
                token="read",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
            ),
            legacy.eligibility,
        )
        self.assertFalse(calls)
        state, reader, _ = self.state()
        budget = EvidenceReadBudget()
        roots = []

        class Reader:
            def __call__(self, reference):
                return reader(reference)

            def validate_root(self, review, reviews):
                roots.append(review["id"])
                self_outer.assertIn(review, reviews)

        self_outer = self

        def factory(**scope):
            calls.append(scope)
            return Reader()

        finalizer = ReviewApprovalFinalizer(
            http=state.http,
            human_inventory_reader_factory=factory,
            evidence_budget=budget,
        )
        for _ in range(2):
            finalizer.load_eligibility(
                token="read",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
            )
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["budget"], budget)
        self.assertEqual(calls[0]["repository"], "owner/repo")
        self.assertEqual(calls[0]["head_sha"], HEAD)
        self.assertEqual(roots, [20, 20])
        self.assertEqual(budget.calls, 2)

    def credential_scoped_finalizer(self, budget, *, read_token=None):
        from urllib.parse import urlparse

        from review_sensei_github.publication import ReviewApprovalFinalizer
        from review_sensei_github.publication_recovery import publication_http

        state, reader, _ = self.state()
        http = publication_http(state.http, budget)
        original_open = state.http.opener
        credentials = []
        scopes = []

        def observed_open(request, timeout):
            if urlparse(request.full_url).path.endswith("/pulls/1"):
                credentials.append(request.get_header("Authorization"))
            return original_open(request, timeout)

        state.http.opener = observed_open

        def factory(**scope):
            scopes.append(scope)

            class Reader:
                def __call__(self, reference):
                    return reader(reference)

                def validate_root(self, review, reviews):
                    # A real HTTP read proves which credential the cached
                    # reader uses, not merely that its factory was called.
                    http.request(
                        "GET", "/repos/owner/repo/pulls/1", token=scope["token"]
                    )

            return Reader()

        return (
            state,
            ReviewApprovalFinalizer(
                http=http,
                evidence_budget=budget,
                human_inventory_reader_factory=factory,
                human_inventory_read_token=read_token,
            ),
            scopes,
            credentials,
        )

    def test_scope_factory_uses_current_credential_without_refilling_shared_budget(
        self,
    ):
        from review_sensei.bounded_evidence import EvidenceReadBudget
        from tests.test_human_assessment import APP, HEAD

        budget = EvidenceReadBudget()
        deadline = budget.deadline
        state, finalizer, scopes, credentials = self.credential_scoped_finalizer(budget)
        for token in (
            "synthetic-first",
            "synthetic-first",
            "synthetic-second",
            "synthetic-second",
        ):
            self.assertEqual(
                finalizer.load_eligibility(
                    token=token,
                    repository="owner/repo",
                    pull_request=1,
                    head_sha=HEAD,
                    app_slug=APP,
                    require_valid=True,
                ),
                state.eligibility,
            )
        self.assertEqual(
            [scope["token"] for scope in scopes],
            ["synthetic-first", "synthetic-second"],
        )
        self.assertTrue(all(scope["budget"] is budget for scope in scopes))
        self.assertEqual(
            credentials,
            [
                "Bearer synthetic-first",
                "Bearer synthetic-first",
                "Bearer synthetic-second",
                "Bearer synthetic-second",
            ],
        )
        self.assertEqual(budget.calls, len(state.calls))
        self.assertEqual(budget.calls, 8)
        self.assertEqual(budget.deadline, deadline)
        self.assertEqual(state.events(), [])

    def test_explicit_metadata_credential_preserves_read_context_across_write_tokens(
        self,
    ):
        from urllib.parse import urlparse

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from tests.test_human_assessment import APP, HEAD

        budget = EvidenceReadBudget()
        deadline = budget.deadline
        state, finalizer, scopes, credentials = self.credential_scoped_finalizer(
            budget, read_token="synthetic-metadata"
        )
        original_open = state.http.opener
        review_credentials = []

        def observed_open(request, timeout):
            if urlparse(request.full_url).path.endswith("/reviews"):
                review_credentials.append(request.get_header("Authorization"))
            return original_open(request, timeout)

        state.http.opener = observed_open
        for token in ("synthetic-issue", "synthetic-review"):
            self.assertEqual(
                finalizer.load_eligibility(
                    token=token,
                    repository="owner/repo",
                    pull_request=1,
                    head_sha=HEAD,
                    app_slug=APP,
                    require_valid=True,
                ),
                state.eligibility,
            )
        self.assertEqual([scope["token"] for scope in scopes], ["synthetic-metadata"])
        self.assertIs(scopes[0]["budget"], budget)
        self.assertEqual(credentials, ["Bearer synthetic-metadata"] * 2)
        self.assertEqual(
            review_credentials, ["Bearer synthetic-issue", "Bearer synthetic-review"]
        )
        self.assertEqual(budget.calls, len(state.calls))
        self.assertEqual(budget.calls, 4)
        self.assertEqual(budget.deadline, deadline)
        self.assertEqual(state.events(), [])

    def test_changed_credential_cannot_recreate_expired_reader_allowance(self):
        from review_sensei_github.errors import GitHubPublicationError

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from tests.test_human_assessment import APP, HEAD

        clock = [0.0]
        budget = EvidenceReadBudget(clock=lambda: clock[0])
        state, finalizer, scopes, _ = self.credential_scoped_finalizer(budget)
        arguments = dict(
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            require_valid=True,
        )
        self.assertEqual(
            finalizer.load_eligibility(token="synthetic-first", **arguments),
            state.eligibility,
        )
        calls = len(state.calls)
        charged = budget.calls
        clock[0] = 61.0
        with self.assertRaises(GitHubPublicationError) as caught:
            finalizer.load_eligibility(token="synthetic-second", **arguments)
        causes = []
        cause = caught.exception
        while cause is not None:
            causes.append(str(cause))
            cause = cause.__cause__
        self.assertRegex(" ".join(causes), "budget|deadline")
        self.assertEqual(len(state.calls), calls)
        self.assertEqual(budget.calls, charged)
        self.assertEqual(len(scopes), 1)
        self.assertEqual(state.events(), [])

    def test_scope_factory_root_validation_and_latest_malformed_fail_closed(self):
        from review_sensei_github.errors import GitHubPublicationError
        from review_sensei_github.publication import ReviewApprovalFinalizer

        from review_sensei.bounded_evidence import EvidenceReadBudget
        from tests.test_human_assessment import APP, HEAD

        state, reader, seen = self.state()

        class Reader:
            def __call__(self, reference):
                return reader(reference)

            def validate_root(self, review, reviews):
                raise ReviewInputError("wrong numeric owner or full visible prose")

        finalizer = ReviewApprovalFinalizer(
            http=state.http,
            human_inventory_reader_factory=lambda **_: Reader(),
            evidence_budget=EvidenceReadBudget(),
        )
        self.assertIsNone(
            finalizer.load_eligibility(
                token="read",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
            )
        )
        with self.assertRaises(GitHubPublicationError):
            finalizer.load_eligibility(
                token="read",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                require_valid=True,
            )
        # A malformed newer owned record cannot expose the older valid one.
        state.reviews.append(
            {
                **state.reviews[0],
                "id": 21,
                "body": "<!-- reviewsensei:eligibility:v1 broken -->",
            }
        )
        before = len(seen)
        self.assertIsNone(
            finalizer.load_eligibility(
                token="read",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
            )
        )
        self.assertEqual(len(seen), before)
        self.assertEqual(state.events(), [])

    def test_detached_reference_finalization_requires_persisted_readback(self):
        from review_sensei_github.publication import ReviewApprovalFinalizer

        from tests.test_human_assessment import APP, HEAD

        state, _, _ = self.state(extra_human=False)
        complete = replace(
            state.eligibility,
            human_review=replace(
                state.eligibility.human_review,
                resolved=tuple(
                    item.fingerprint for item in state.eligibility.human_review.findings
                ),
            ),
            facts=replace(
                state.eligibility.facts, has_human_adjudication_findings=False
            ),
        )
        state.reviews = [state.review(complete, 20)]
        result = ReviewApprovalFinalizer(http=state.http).finalize(
            token="review",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=complete,
        )
        self.assertEqual(result.status, "approval_withheld")
        self.assertEqual(state.events(), [])
