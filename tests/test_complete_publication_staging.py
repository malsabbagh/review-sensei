"""Nonactivating whole inventory and visible COMMENT-review staging boundaries."""

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

from review_sensei.bounded_evidence import (
    AuthenticatedPart,
    EvidenceReadBudget,
    canonical_bytes,
)
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.approval import approval_eligibility_from_result
from review_sensei.hosting.github.http import GitHubHttp
from review_sensei.hosting.github.prose_publication import (
    CommentReviewProseStore,
    ProseStagingError,
    VisibleProseReceipt,
)
from review_sensei.hosting.github.publication_parts import (
    prepare_finding_prose_parts,
)
from review_sensei.human_assessment import HumanInventoryResolution, PendingHumanReview
from review_sensei.human_inventory import read_human_inventory, stage_human_inventory
from tests.fake_github_http import json_response
from tests.test_authenticated_partitions import binding
from tests.test_evidence_capacity import human_result, varied_comments
from tests.test_human_assessment import APP, BASE, HEAD

PRODUCER = 12345


def inventory_context(**changes):
    value = binding(producer=f"github-bot:{PRODUCER}", purpose="human-inventory")
    value.update(repository="owner/repo", base_sha=BASE, head_sha=HEAD)
    value.update(changes)
    return value


class Parts:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.owner = f"github-bot:{PRODUCER}"

    def write(self, document, remaining):
        assert 0 < remaining <= 60
        identifier = hashlib.sha256(canonical_bytes(document)).hexdigest()
        self.values.setdefault(identifier, copy.deepcopy(document))
        self.calls.append(("write", identifier))
        return identifier

    def read(self, identifier, remaining):
        assert 0 < remaining <= 60
        self.calls.append(("read", identifier))
        if identifier not in self.values:
            raise ReviewInputError("missing retained evidence part")
        return AuthenticatedPart(self.values[identifier], self.owner)


class Reviews:
    def __init__(self):
        self.items = []
        self.calls = []
        self.hook = None
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def item(self, identifier, body):
        return {
            "id": identifier,
            "body": body,
            "state": "COMMENTED",
            "commit_id": HEAD,
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
            "user": {"type": "Bot", "id": PRODUCER, "login": APP},
        }

    def open(self, request, timeout):
        assert 0 < timeout <= 30
        parsed = urlparse(request.full_url)
        payload = json.loads(request.data) if request.data else None
        self.calls.append((request.method, parsed.path, payload))
        if self.hook:
            result = self.hook(request.method, parsed.path, payload)
            if result is not None:
                return result
        if request.method == "GET" and parsed.query:
            page = int(parse_qs(parsed.query)["page"][0])
            return json_response(self.items[page - 1 : page])
        if request.method == "GET":
            identifier = int(parsed.path.rsplit("/", 1)[1])
            return json_response(
                next(item for item in self.items if item["id"] == identifier)
            )
        if request.method == "POST":
            assert parsed.path == "/repos/owner/repo/pulls/1/reviews"
            assert set(payload) == {"commit_id", "event", "body"}
            assert payload["event"] == "COMMENT" and payload["commit_id"] == HEAD
            item = self.item(len(self.items) + 100, payload["body"])
            self.items.append(item)
            return json_response(item, 201)
        raise AssertionError(request.method)

    def store(self, budget=None):
        return CommentReviewProseStore(
            http=self.http,
            token="synthetic-existing-publication-capability",
            repository="owner/repo",
            pull_request=1,
            producer_id=PRODUCER,
            app_slug=APP,
            budget=budget or EvidenceReadBudget(),
        )


class CompletePublicationStagingTests(unittest.TestCase):
    def test_rediscovery_keeps_original_inventory_metadata_and_resolution(self):
        original = varied_comments(1)[0]
        retained = approval_eligibility_from_result(
            human_result((original,)),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
        )
        inventory = retained.human_review
        retained = replace(
            retained,
            human_review=replace(
                inventory, resolved=(inventory.findings[0].fingerprint,)
            ),
        )
        changed = replace(
            original,
            symbol="different_annotation",
            blocking=True,
            effective_blocking=True,
        )
        current = approval_eligibility_from_result(
            human_result((changed,)),
            head_sha=HEAD,
            base_sha=BASE,
            enabled=True,
            app_authored=False,
            retained_eligibility=retained,
        )
        self.assertEqual(
            current.human_review.inventory_document(),
            retained.human_review.inventory_document(),
        )
        self.assertEqual(current.human_review.resolved, retained.human_review.resolved)
        self.assertTrue(current.facts.has_blocking_findings)
        self.assertFalse(
            current.evaluate(app_authored=False, has_open_review_threads=False).approved
        )

    def test_varied_100_250_complete_stage_readback_and_all_resolution_growth(self):
        for count in (100, 250):
            with self.subTest(count=count):
                comments = varied_comments(count, detail=3, unique_paths=count)
                inventory = PendingHumanReview.from_result(human_result(comments), BASE)
                parts = prepare_finding_prose_parts(comments, head_sha=HEAD)
                budget = EvidenceReadBudget()
                evidence = Parts()
                manifest = stage_human_inventory(
                    inventory,
                    binding=inventory_context(),
                    max_manifest_bytes=8192,
                    writer=evidence.write,
                    reader=evidence.read,
                    budget=budget,
                )
                reviews = Reviews()
                store = reviews.store(budget)
                receipts = store.stage(parts, head_sha=HEAD)
                restored = read_human_inventory(
                    manifest,
                    expected_binding=inventory_context(),
                    reader=evidence.read,
                    budget=budget,
                )
                store.revalidate(receipts)
                self.assertEqual(
                    restored.inventory_document(), inventory.inventory_document()
                )
                self.assertEqual(
                    len({identity for item in receipts for identity in item.instances}),
                    count,
                )
                self.assertEqual(
                    sum(item.bytes for item in receipts),
                    sum(part.byte_length for part in parts),
                )
                fully_resolved = replace(
                    restored,
                    resolved=tuple(item.fingerprint for item in restored.findings),
                )
                resolution = HumanInventoryResolution.from_inventory(fully_resolved)
                self.assertEqual(resolution.restore(restored).pending, ())
                self.assertEqual(restored.inventory_digest, resolution.inventory_digest)
                self.assertLess(len(canonical_bytes(resolution.to_dict())), 24 * 1024)
                self.assertLessEqual(budget.calls, 60)
                for _ in range(4):
                    budget.consume(fence=True)
                self.assertLessEqual(budget.calls, 64)
                self.assertEqual(len(reviews.items), len(parts))
                self.assertTrue(
                    all(
                        "<!-- reviewsensei:" not in item["body"]
                        for item in reviews.items
                    )
                )
                self.assertTrue(
                    all(
                        method == "GET" or payload["event"] == "COMMENT"
                        for method, _, payload in reviews.calls
                    )
                )

    def test_trusted_kind_snapshot_count_and_actual_root_capacity_fail_closed(self):
        inventory = PendingHumanReview.from_result(
            human_result(varied_comments(100, detail=3)), BASE
        )
        evidence = Parts()
        for context, limit in (
            (inventory_context(purpose="baseline"), 8192),
            (inventory_context(base_sha="c" * 40), 8192),
            (inventory_context(), 1),
        ):
            with self.assertRaises(ReviewInputError):
                stage_human_inventory(
                    inventory,
                    binding=context,
                    max_manifest_bytes=limit,
                    writer=evidence.write,
                    reader=evidence.read,
                    budget=EvidenceReadBudget(),
                )
            self.assertEqual(evidence.calls, [])
        manifest = stage_human_inventory(
            inventory,
            binding=inventory_context(),
            max_manifest_bytes=8192,
            writer=evidence.write,
            reader=evidence.read,
            budget=EvidenceReadBudget(),
        )
        for changed in (
            dict(manifest, item_count=99),
            dict(manifest, binding=inventory_context(head_sha="c" * 40)),
        ):
            with self.assertRaises(ReviewInputError):
                read_human_inventory(
                    changed,
                    expected_binding=inventory_context(),
                    reader=evidence.read,
                    budget=EvidenceReadBudget(),
                )
        evidence.owner = "github-bot:999"
        with self.assertRaises(ReviewInputError):
            read_human_inventory(
                manifest,
                expected_binding=inventory_context(),
                reader=evidence.read,
                budget=EvidenceReadBudget(),
            )

    def test_missing_or_mutated_inventory_part_never_materializes_partial_state(self):
        inventory = PendingHumanReview.from_result(
            human_result(varied_comments(100, detail=3)), BASE
        )
        evidence = Parts()
        manifest = stage_human_inventory(
            inventory,
            binding=inventory_context(),
            max_manifest_bytes=8192,
            writer=evidence.write,
            reader=evidence.read,
            budget=EvidenceReadBudget(),
        )
        key = next(iter(evidence.values))
        part = evidence.values.pop(key)
        with self.assertRaises(ReviewInputError):
            read_human_inventory(
                manifest,
                expected_binding=inventory_context(),
                reader=evidence.read,
                budget=EvidenceReadBudget(),
            )
        evidence.values[key] = dict(part, data=part["data"][:-4] + "AAAA")
        with self.assertRaises(ReviewInputError):
            read_human_inventory(
                manifest,
                expected_binding=inventory_context(),
                reader=evidence.read,
                budget=EvidenceReadBudget(),
            )

    def test_each_post_and_readback_failure_retains_and_reconciles(self):
        parts = prepare_finding_prose_parts(
            varied_comments(100, detail=3, unique_paths=100), head_sha=HEAD
        )
        for phase in ("POST", "GET"):
            for failure in range(len(parts)):
                with self.subTest(phase=phase, failure=failure):
                    reviews = Reviews()
                    attempted = [0]

                    def fail(method, path, payload):
                        if method == phase and (
                            phase == "POST" or path.rsplit("/", 1)[-1].isdigit()
                        ):
                            index = attempted[0]
                            attempted[0] += 1
                            if index == failure:
                                if phase == "POST":
                                    # The host commits, but the response is ambiguous.
                                    reviews.items.append(
                                        reviews.item(
                                            len(reviews.items) + 100, payload["body"]
                                        )
                                    )
                                return json_response({}, 503)
                        return None

                    reviews.hook = fail
                    with self.assertRaises(ProseStagingError) as caught:
                        reviews.store().stage(parts, head_sha=HEAD)
                    self.assertEqual(
                        caught.exception.diagnostic, "visible_prose_staging_failed"
                    )
                    self.assertTrue(reviews.items)
                    reviews.hook = None
                    receipts = reviews.store().stage(parts, head_sha=HEAD)
                    self.assertEqual(len(receipts), len(parts))
                    self.assertEqual(len(reviews.items), len(parts))
                    reviews.store().revalidate(receipts)

    def test_exact_readback_owner_numeric_id_pr_head_placement_and_body(self):
        parts = prepare_finding_prose_parts(varied_comments(1), head_sha=HEAD)
        for change in (
            lambda item: item.update(id=True),
            lambda item: item["user"].update(id=PRODUCER + 1),
            lambda item: item["user"].update(login="foreign[bot]"),
            lambda item: item["user"].update(type="User"),
            lambda item: item.update(
                pull_request_url="https://api.github.test/repos/owner/other/pulls/1"
            ),
            lambda item: item.update(commit_id="c" * 40),
            lambda item: item.update(state="APPROVED"),
            lambda item: item.update(body=item["body"] + "altered"),
        ):
            reviews = Reviews()
            receipts = reviews.store().stage(parts, head_sha=HEAD)
            change(reviews.items[0])
            with self.assertRaises((ReviewInputError, StopIteration)):
                reviews.store().revalidate(receipts)

    def test_incomplete_duplicate_metadata_refuses_before_writes(self):
        parts = prepare_finding_prose_parts(varied_comments(1), head_sha=HEAD)
        reviews = Reviews()
        reviews.items = [
            reviews.item(100, "ordinary review"),
            reviews.item(100, "duplicate"),
        ]
        with self.assertRaises(ProseStagingError):
            reviews.store().stage(parts, head_sha=HEAD)
        self.assertFalse(any(method == "POST" for method, _, _ in reviews.calls))

    def test_shared_budget_and_original_deadline_are_not_reset(self):
        parts = prepare_finding_prose_parts(
            varied_comments(100, detail=3), head_sha=HEAD
        )
        budget = EvidenceReadBudget()
        for _ in range(58):
            budget.consume()
        original = budget.snapshot()
        reviews = Reviews()
        with self.assertRaises(ProseStagingError):
            reviews.store(budget).stage(parts, head_sha=HEAD)
        self.assertEqual(budget.calls, 59)
        self.assertEqual(
            budget.snapshot()["deadline_unix_ms"], original["deadline_unix_ms"]
        )
        self.assertFalse(any(method == "POST" for method, _, _ in reviews.calls))
        expired = EvidenceReadBudget(clock=lambda: 0.0)
        expired.clock = lambda: 60.0
        with self.assertRaises(ProseStagingError):
            reviews.store(expired).stage(parts, head_sha=HEAD)

    def test_invalid_forged_part_and_receipt_metadata_refuse(self):
        part = prepare_finding_prose_parts(varied_comments(1), head_sha=HEAD)[0]
        for changes in (
            {"index": True},
            {"paths": ("../escape",)},
            {"body": "<!-- reviewsensei:eligibility:v1 marker -->"},
            {"instances": ("x",)},
        ):
            with self.assertRaises(ReviewInputError):
                replace(part, **changes)
        reviews = Reviews()
        receipt = reviews.store().stage((part,), head_sha=HEAD)[0]
        self.assertEqual(VisibleProseReceipt.from_dict(receipt.to_dict()), receipt)
        for changes in (
            {"review_id": True},
            {"bytes": 65537},
            {"placement": "issue-comment"},
            {"paths": ["../escape"]},
            {"instances": ["a"]},
        ):
            with self.assertRaises(ReviewInputError):
                VisibleProseReceipt.from_dict(dict(receipt.to_dict(), **changes))
        with self.assertRaises(ReviewInputError):
            reviews.store().revalidate((receipt, receipt))


if __name__ == "__main__":
    unittest.main()
