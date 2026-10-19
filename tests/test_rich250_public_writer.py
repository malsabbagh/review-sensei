"""Explicit library writer with real serialized local generation authority."""

import copy
import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from review_sensei_github import ReviewPublisher
from review_sensei_github.eligibility_parts import (
    PartitionedEligibilityAllocation,
    PartitionedEligibilityContext,
)
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.partitioned_publication import PartitionedPublicationPlan
from review_sensei_github.publication import review_result_digest

from review_sensei.bounded_evidence import (
    AuthenticatedPart,
    EvidenceReadBudget,
    canonical_bytes,
    read_partitioned_evidence,
    stage_partitioned_evidence,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewTransaction
from review_sensei.schemas import validate_public_document
from review_sensei.session import LocalSessionLedger, SessionIdentity
from tests.fake_github_http import json_response
from tests.test_evidence_capacity import human_result, publication_diff, varied_comments
from tests.test_human_assessment import APP, BASE, HEAD, State
from tests.test_review_transaction import CONFIGURATION

PRODUCER = 12345


class RichState(State):
    def __init__(self):
        super().__init__()
        self.reviews = []
        self.parts = {}

    def open(self, request, timeout):
        parsed = urlparse(request.full_url)
        path = parsed.path
        if request.method == "GET" and path.endswith("/pulls/1/reviews"):
            query = parse_qs(parsed.query)
            size = int(query.get("per_page", [100])[0])
            start = (int(query.get("page", [1])[0]) - 1) * size
            self.calls.append(("GET", path, None))
            return json_response(self.reviews[start : start + size])
        if request.method == "GET" and "/pulls/1/reviews/" in path:
            self.calls.append(("GET", path, None))
            value = next(
                (
                    item
                    for item in self.reviews
                    if str(item["id"]) == path.rsplit("/", 1)[1]
                ),
                None,
            )
            return json_response(value, 200 if value else 404)
        if request.method == "POST" and path.endswith("/issues/1/comments"):
            document = json.loads(request.data)["body"]
            identifier = str(10000 + len(self.parts))
            self.parts[identifier] = {
                "id": int(identifier),
                "document": json.loads(document),
                "issue_url": self.http.api_url + "/repos/owner/repo/issues/1",
                "user": {"id": PRODUCER, "login": APP, "type": "Bot"},
            }
            self.calls.append(("POST", path, None))
            return json_response(self.parts[identifier], 201)
        if (
            request.method == "GET"
            and "/issues/comments/" in path
            and not path.endswith("/10")
        ):
            self.calls.append(("GET", path, None))
            value = self.parts.get(path.rsplit("/", 1)[1])
            return json_response(value, 200 if value else 404)
        result = super().open(request, timeout)
        if request.method == "POST" and path.endswith("/pulls/1/reviews"):
            item = self.reviews[-1]
            item["user"].update(id=PRODUCER, avatar_url="https://avatar.test/bot")
            item["pull_request_url"] = self.http.api_url + "/repos/owner/repo/pulls/1"
        return result


class LocalPublicationHost:
    """Trusted serialized writer; ledger generation is independent of manifests.

    This fixture owns one local operation. It is not a deployed REST/broker host
    and does not claim fresh-process recovery from an unknown remote write.
    """

    def __init__(self, root, state, result, policy, configuration, *, budget=None):
        self.root, self.state = Path(root), state
        self.identity = SessionIdentity("owner/repo", 1, repository_id=1)
        LocalSessionLedger(self.root).initialize(self.identity)
        self.budget = budget or EvidenceReadBudget()
        self.ledger = LocalSessionLedger(self.root, evidence_budget=self.budget)
        self.reservation = review_result_digest(result)
        reserved = self.ledger.reserve(
            self.identity,
            slot="initial",
            reservation_id=self.reservation,
            expected_generation=0,
        )
        self.plan = PartitionedPublicationPlan(
            binding={
                "repository": "owner/repo",
                "repository_id": 1,
                "pull_request": 1,
                "base_sha": BASE,
                "head_sha": HEAD,
                "policy_digest": policy.digest(),
                "configuration_digest": ReviewTransaction.compute_configuration_digest(
                    configuration
                ),
                "generation": 0,
                "producer": f"github-bot:{PRODUCER}",
                "purpose": "human-inventory",
                "schema_version": "1.0",
            },
            result_digest=review_result_digest(result),
            activation_generation=reserved.generation + 1,
            visible_instances=tuple(
                sorted(finding_instance_fingerprint(item) for item in result.comments)
            ),
            allocation=PartitionedEligibilityAllocation(32768, 2000, 1000),
            budget=self.budget,
        )
        self.lock = threading.Lock()
        self.before_activation = None
        self.unknown = False

    def fence(self):
        record = self.ledger.load(self.identity).record
        if (
            record is None
            or record.generation != self.plan.activation_generation - 1
            or record.reservation_id != self.reservation
            or self.unknown
        ):
            raise ReviewInputError(
                "owned publication generation or original operation changed"
            )

    def request(self, method, path, body=None):
        status, value = self.state.http.request(
            method,
            path,
            token="synthetic",
            body=body,
            timeout_seconds=self.budget.consume(),
        )
        self.budget.check()
        if status not in (200, 201):
            raise ReviewInputError("owned publication transport failed")
        return value

    def write_part(self, document):
        value = self.request(
            "POST",
            "/repos/owner/repo/issues/1/comments",
            {"body": canonical_bytes(document).decode()},
        )
        return str(value["id"])

    def read_part(self, key):
        value = self.request("GET", "/repos/owner/repo/issues/comments/" + key)
        if (
            value["user"]["id"] != PRODUCER
            or value["user"]["type"] != "Bot"
            or value["user"]["login"] != APP
            or value["issue_url"]
            != self.state.http.api_url + "/repos/owner/repo/issues/1"
        ):
            raise ReviewInputError("owned immutable part changed")
        return AuthenticatedPart(value["document"], f"github-bot:{PRODUCER}")

    def stage(self, document, *, binding, item_count):
        return stage_partitioned_evidence(
            document,
            binding=dict(binding),
            item_count=item_count,
            max_manifest_bytes=8192,
            writer=self.write_part,
            reader=self.read_part,
            budget=self.budget,
        )

    def read(self, manifest, *, expected_binding, budget):
        if budget is not self.budget:
            raise ReviewInputError("original publication budget changed")
        return read_partitioned_evidence(
            manifest,
            expected_binding=expected_binding,
            reader=self.read_part,
            budget=budget,
        )

    def activate(self, request):
        self.last_request = request
        with self.lock:
            self.fence()
            actual_id = None

            def mutate(record):
                nonlocal actual_id
                if record.generation != self.plan.activation_generation - 1:
                    raise ReviewInputError("owned publication generation changed")
                if self.before_activation:
                    self.before_activation()
                # Hold the original attempt before a possibly committed remote write.
                # Only exact owned readback and a successful local CAS may clear it.
                self.unknown = True
                value = self.request(
                    "POST",
                    "/repos/owner/repo/pulls/1/reviews",
                    {"body": request.body, "event": "COMMENT", "commit_id": HEAD},
                )
                actual_id = value.get("id") if isinstance(value, dict) else None
                if type(actual_id) is not int or not 0 < actual_id <= 2**63 - 1:
                    raise GitHubPublicationError("owned root readback ID is invalid")
                root = self.request(
                    "GET", f"/repos/owner/repo/pulls/1/reviews/{actual_id}"
                )
                user = root.get("user") if isinstance(root, dict) else None
                if (
                    not isinstance(root, dict)
                    or type(root.get("id")) is not int
                    or root.get("id") != actual_id
                    or root.get("body") != request.body
                    or root.get("commit_id") != HEAD
                    or root.get("state") != "COMMENTED"
                    or root.get("pull_request_url")
                    != self.state.http.api_url + "/repos/owner/repo/pulls/1"
                    or not isinstance(user, dict)
                    or type(user.get("id")) is not int
                    or user.get("id") != PRODUCER
                    or user.get("type") != "Bot"
                    or user.get("login") != APP
                ):
                    raise GitHubPublicationError("owned root readback failed")
                return record.evolve(generation=record.generation + 1)

            accepted = self.ledger.replace(self.identity, mutate)
            self.unknown = False
            self.owned_context = PartitionedEligibilityContext(
                binding=self.plan.binding,
                result_digest=self.plan.result_digest,
                activation_generation=accepted.generation,
                owned_root_id=actual_id,
                visible_instances=self.plan.visible_instances,
                budget=self.budget,
                max_root_bytes=self.plan.allocation.max_root_bytes,
                framing_bytes=self.plan.allocation.framing_bytes,
                future_lifecycle_bytes=self.plan.allocation.future_lifecycle_bytes,
            )
            return self.owned_context


class Rich250PublicWriterTests(unittest.TestCase):
    def fixture(self, directory, detail=1, *, budget=None):
        state = RichState()
        comments = varied_comments(250, detail=detail, unique_paths=250)
        result = human_result(comments)
        policy = ReviewConvergencePolicy(enforcement="publication")
        configuration = dict(CONFIGURATION, publication_mode=policy.mode)
        host = LocalPublicationHost(
            directory, state, result, policy, configuration, budget=budget
        )
        return state, comments, result, policy, configuration, host

    def publish(self, values, **overrides):
        state, comments, result, policy, configuration, host = values
        return ReviewPublisher(http=state.http).publish_partitioned(
            token="synthetic",
            host=host,
            result=result,
            diff=publication_diff(comments),
            configuration_context=configuration,
            convergence_policy=policy,
            app_slug=APP,
            check_token="synthetic-checks",
            **overrides,
        )

    def test_complete_rich250_public_write_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            receipt = self.publish(values)
            state, comments, _, _, _, host = values
            self.assertEqual(len(receipt.eligibility.human_review.pending), 250)
            self.assertEqual(
                {item.body for item in receipt.eligibility.human_review.findings},
                {item.body for item in comments},
            )
            self.assertEqual(receipt.eligibility.facts.provider, "fixture")
            self.assertEqual(receipt.eligibility.facts.qualification, "not-required")
            self.assertLessEqual(host.budget.calls, 60)
            self.assertEqual(host.budget.calls, 49)
            self.assertEqual(host.budget.calls, len(state.calls) + 13)
            self.assertNotIn("APPROVE", state.events())
            self.assertTrue(receipt.eligibility.facts.check_published)
            self.assertEqual(state.check_runs[-1]["conclusion"], "action_required")
            visible = "\n".join(item["body"] for item in state.reviews[:-1])
            for comment in comments:
                self.assertIn(comment.body, visible)
            validate_public_document(receipt.eligibility.to_dict(), "eligibility-v4")
            complete = replace(
                receipt.eligibility,
                human_review=replace(
                    receipt.eligibility.human_review,
                    resolved=tuple(
                        item.fingerprint
                        for item in receipt.eligibility.human_review.findings
                    ),
                ),
                facts=replace(
                    receipt.eligibility.facts, has_human_adjudication_findings=False
                ),
            )
            self.assertEqual(
                len(complete.to_dict()["human_resolution"]["resolved"]), 250
            )
            self.assertEqual(len(complete.human_review.findings), 250)

    def test_every_live_pr_dispatch_receives_original_remaining_timeout(self):
        clock = [0.0]
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(
                directory, budget=EvidenceReadBudget(clock=lambda: clock[0])
            )
            state, _, _, _, _, host = values
            fence = host.fence

            def late_fence():
                fence()
                clock[0] = 59.0

            host.fence = late_fence
            opener = state.http.opener
            timeouts = []

            def capture(request, timeout):
                if request.method == "GET" and urlparse(request.full_url).path.endswith(
                    "/pulls/1"
                ):
                    timeouts.append(timeout)
                return opener(request, timeout)

            state.http.opener = capture
            self.publish(values)
            self.assertEqual(len(timeouts), 3)
            self.assertTrue(all(0 < value <= 1 for value in timeouts), timeouts)

    def test_actual_lost_root_post_or_readback_never_retries_the_original_activation(
        self,
    ):
        for phase in ("POST", "GET"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                values = self.fixture(directory)
                state, _, _, _, _, host = values
                original = host.request

                def lost(method, path, body=None):
                    value = original(method, path, body)
                    if method == phase and (
                        path.endswith("/pulls/1/reviews")
                        if phase == "POST"
                        else "/pulls/1/reviews/" in path
                    ):
                        raise GitHubPublicationError("synthetic lost root response")
                    return value

                host.request = lost
                with self.assertRaisesRegex(
                    GitHubPublicationError, "lost root response"
                ):
                    self.publish(values)
                self.assertTrue(host.unknown)
                self.assertEqual(
                    LocalSessionLedger(Path(directory))
                    .load(host.identity)
                    .record.generation,
                    1,
                )
                self.assertFalse(hasattr(host, "owned_context"))
                roots = len(state.reviews)
                calls = len(state.calls)
                with self.assertRaisesRegex(
                    ReviewInputError, "original operation changed"
                ):
                    host.activate(host.last_request)
                self.assertEqual((len(state.reviews), len(state.calls)), (roots, calls))
                self.assertNotIn("APPROVE", state.events())

    def test_operator_optouts_remain_persisted_without_qualification_promotion(self):
        for policy in ({"auto_approve": False}, {"reviews_policy": "advisory"}):
            with (
                self.subTest(policy=policy),
                tempfile.TemporaryDirectory() as directory,
            ):
                values = self.fixture(directory)
                receipt = self.publish(values, **policy)
                self.assertFalse(receipt.eligibility.facts.enabled)
                self.assertEqual(
                    receipt.eligibility.facts.qualification, "not-required"
                )
                self.assertNotIn("APPROVE", values[0].events())

    def test_stage_cannot_substitute_self_consistent_foreign_inventory_before_root(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, _, _, _, _, host = values
            stage = host.stage

            def foreign(document, **arguments):
                document = copy.deepcopy(document)
                if arguments["binding"]["purpose"] == "human-inventory":
                    document["inventory"]["findings"][0]["body"] += " changed"
                    document["inventory_digest"] = hashlib.sha256(
                        canonical_bytes(document["inventory"])
                    ).hexdigest()
                return stage(document, **arguments)

            host.stage = foreign
            with self.assertRaisesRegex(ReviewInputError, "manifest content"):
                self.publish(values)
            self.assertFalse(
                any(
                    "<!-- reviewsensei:eligibility:" in item["body"]
                    for item in state.reviews
                )
            )
            self.assertFalse(hasattr(host, "owned_context"))

    def test_actual_owned_root_readback_identity_body_snapshot_and_association_are_required(
        self,
    ):
        for fault in (
            "id",
            "owner",
            "kind",
            "login",
            "body",
            "head",
            "state",
            "association",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                values = self.fixture(directory)
                state, _, _, _, _, host = values
                request = state.http.request

                def changed(method, path, **arguments):
                    status, value = request(method, path, **arguments)
                    if (
                        method == "GET"
                        and "/reviews/" in path
                        and isinstance(value, dict)
                        and "<!-- reviewsensei:eligibility:" in value.get("body", "")
                    ):
                        value = copy.deepcopy(value)
                        if fault == "id":
                            value["id"] = str(value["id"])
                        elif fault == "owner":
                            value["user"]["id"] += 1
                        elif fault == "kind":
                            value["user"]["type"] = "User"
                        elif fault == "login":
                            value["user"]["login"] = "foreign[bot]"
                        elif fault == "body":
                            value["body"] += " changed"
                        elif fault == "head":
                            value["commit_id"] = "e" * 40
                        elif fault == "state":
                            value["state"] = "PENDING"
                        else:
                            value["pull_request_url"] += "0"
                    return status, value

                state.http.request = changed
                with self.assertRaisesRegex(
                    GitHubPublicationError, "owned root readback"
                ):
                    self.publish(values)
                self.assertNotIn("APPROVE", state.events())

    def test_untrusted_expected_context_and_complete_future_growth_refuse_before_io(
        self,
    ):
        for change in ("policy", "configuration", "result", "visible", "allocation"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                values = self.fixture(directory)
                state, _, _, _, _, host = values
                if change in {"policy", "configuration"}:
                    host.plan = replace(
                        host.plan,
                        binding=dict(
                            host.plan.binding, **{change + "_digest": "e" * 64}
                        ),
                    )
                elif change == "result":
                    host.plan = replace(host.plan, result_digest="e" * 64)
                elif change == "visible":
                    host.plan = replace(
                        host.plan, visible_instances=host.plan.visible_instances[:-1]
                    )
                else:
                    host.plan = replace(
                        host.plan,
                        allocation=PartitionedEligibilityAllocation(8192, 2000, 1000),
                    )
                with self.assertRaises(ReviewInputError):
                    self.publish(values)
                self.assertEqual(state.calls, [])
                self.assertEqual(state.reviews, [])
                self.assertEqual(state.parts, {})

    def test_unknown_activation_preserves_original_reservation_and_never_retries_fresh(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, _, _, _, _, host = values
            original = host.request

            def lost(method, path, body=None):
                value = original(method, path, body)
                if method == "POST" and path.endswith("/pulls/1/reviews"):
                    raise GitHubPublicationError("synthetic lost root response")
                return value

            host.request = lost
            deadline = host.budget.wall_deadline_ms
            with self.assertRaisesRegex(GitHubPublicationError, "lost root response"):
                self.publish(values)
            observer = LocalSessionLedger(Path(directory)).load(host.identity).record
            self.assertEqual(observer.generation, 1)
            self.assertEqual(observer.reservation_id, host.reservation)
            self.assertFalse(hasattr(host, "owned_context"))
            retained = len(state.reviews), len(state.parts)
            charged = host.budget.calls
            with self.assertRaisesRegex(ReviewInputError, "original operation changed"):
                self.publish(values)
            self.assertEqual((len(state.reviews), len(state.parts)), retained)
            self.assertGreater(host.budget.calls, charged)
            self.assertEqual(host.budget.wall_deadline_ms, deadline)
            self.assertNotIn("APPROVE", state.events())

    def test_competing_unserialized_generation_cannot_return_accepted_context(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, _, _, _, _, host = values

            def compete():
                LocalSessionLedger(Path(directory)).replace(
                    host.identity,
                    lambda record: record.evolve(generation=record.generation + 1),
                )

            host.before_activation = compete
            with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
                self.publish(values)
            self.assertFalse(hasattr(host, "owned_context"))
            self.assertEqual(
                LocalSessionLedger(Path(directory))
                .load(host.identity)
                .record.generation,
                2,
            )
            self.assertNotIn("APPROVE", state.events())

    def test_serialized_competing_original_writers_activate_exactly_one_root(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self.fixture(directory)
            state, _, _, _, _, host = values
            original_activate = host.activate
            captured = []

            def stop(request):
                captured.append(request)
                raise RuntimeError("interrupted before activation")

            host.activate = stop
            with self.assertRaisesRegex(RuntimeError, "before activation"):
                self.publish(values)
            before = len(state.reviews)
            start = threading.Barrier(2)
            results = []

            def writer():
                start.wait()
                try:
                    results.append(original_activate(captured[0]))
                except ReviewInputError as exc:
                    results.append(exc)

            workers = [threading.Thread(target=writer) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(
                sum(
                    isinstance(item, PartitionedEligibilityContext) for item in results
                ),
                1,
            )
            self.assertEqual(
                sum(isinstance(item, ReviewInputError) for item in results), 1
            )
            self.assertEqual(len(state.reviews), before + 1)
            self.assertEqual(
                LocalSessionLedger(Path(directory))
                .load(host.identity)
                .record.generation,
                2,
            )
            self.assertLessEqual(host.budget.calls, 60)

    def test_changed_current_head_and_missing_complete_parts_never_return_receipt(self):
        for change in ("head", "part", "prose"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                values = self.fixture(directory)
                state, _, _, _, _, host = values
                if change == "head":

                    def mutate(_state, method, path, body):
                        if method == "POST" and path.endswith("/pulls/1/reviews"):
                            state.head = "e" * 40

                    state.hook = mutate
                elif change == "part":
                    host.before_activation = lambda: state.parts.clear()
                else:
                    host.before_activation = lambda: state.reviews[0].update(
                        body="changed complete prose"
                    )
                with self.assertRaises((ReviewInputError, GitHubPublicationError)):
                    self.publish(values)
                self.assertNotIn("APPROVE", state.events())


if __name__ == "__main__":
    unittest.main()
