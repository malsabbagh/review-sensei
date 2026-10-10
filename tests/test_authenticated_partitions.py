"""Authenticated whole inventories, distinct from inline/capacity fixtures."""

import copy
import hashlib
import json
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from review_sensei.baseline import (
    admission_context_document,
    admission_context_from_document,
    baseline_complete_document,
    baseline_from_history_document,
)
from review_sensei.bounded_evidence import (
    MAX_PARTITION_DECODED_BYTES,
    PART_READ_SECONDS,
    PARTITION_ENCODING,
    AuthenticatedPart,
    EvidenceReadBudget,
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
    read_partitioned_evidence,
    stage_partitioned_evidence,
    validate_manifest,
)
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import GitHubHttp
from review_sensei.hosting.github.session_ledger import GitHubIssueCommentSessionLedger
from review_sensei.session import (
    LocalSessionLedger,
    checkpoint_review_analysis,
    complete_review_publication,
    prepare_review_transaction,
    read_session_baseline,
)
from tests import test_review_transaction as fixture
from tests.fake_github_http import json_response
from tests.test_evidence_capacity import varied_baseline


def binding(*, producer="local-ledger", generation=2, purpose="baseline"):
    return {
        "repository": fixture.IDENTITY.repository,
        "repository_id": fixture.IDENTITY.repository_id,
        "pull_request": fixture.IDENTITY.pull_request,
        "base_sha": fixture.BASE_SHA,
        "head_sha": fixture.HEAD_SHA,
        "policy_digest": fixture.POLICY.digest(),
        "configuration_digest": fixture.CONFIGURATION_DIGEST,
        "generation": generation,
        "producer": producer,
        "purpose": purpose,
        "schema_version": "1.0",
    }


def packed(document, context=None):
    context = context or binding()
    parts = partition_evidence(document, binding=context, item_count=1)
    store = {hashlib.sha256(canonical_bytes(part)).hexdigest(): part for part in parts}
    manifest = evidence_manifest(
        document, binding=context, item_count=1, parts=parts, storage_ids=tuple(store)
    )
    return manifest, store


class CodecTests(unittest.TestCase):
    def read(
        self, manifest, store, *, context=None, producer="local-ledger", budget=None
    ):
        budget = budget or EvidenceReadBudget()

        def reader(identity):
            budget.consume()
            if identity not in store:
                raise ReviewInputError("missing part")
            return AuthenticatedPart(store[identity], producer)

        return read_partitioned_evidence(
            manifest,
            reader=reader,
            expected_binding=context or binding(),
            budget=budget,
        )

    def test_high_entropy_document_round_trip_and_manifest_schema(self):
        source = {
            "text": random.Random(240).randbytes(80_000).hex(),
            "unicode": '深/🙂/é\\"',
        }
        manifest, store = packed(source)
        self.assertGreater(len(store), 1)
        self.assertEqual(self.read(manifest, store), source)
        from review_sensei.schemas import validate_public_document

        validate_public_document(manifest, "evidence-manifest")

    def test_missing_duplicate_reordered_mutated_and_foreign_parts_fail_closed(self):
        manifest, store = packed({"text": random.Random(240).randbytes(80_000).hex()})
        for kind in (
            "missing",
            "duplicate",
            "reorder",
            "mutate",
            "foreign",
            "snapshot",
            "domain",
            "schema",
            "length",
            "truncated",
            "extra-stream",
            "noncanonical",
        ):
            with self.subTest(kind=kind):
                root = copy.deepcopy(manifest)
                objects = copy.deepcopy(store)
                context = binding()
                producer = "local-ledger"
                first_id = root["parts"][0]["storage_id"]
                if kind == "missing":
                    del objects[first_id]
                elif kind == "duplicate":
                    root["parts"][1] = root["parts"][0]
                elif kind == "reorder":
                    root["parts"].reverse()
                elif kind == "mutate":
                    objects[first_id]["inventory_sha256"] = "f" * 64
                elif kind == "foreign":
                    producer = "github-bot:99"
                elif kind in ("snapshot", "domain", "schema"):
                    context[
                        {
                            "snapshot": "head_sha",
                            "domain": "purpose",
                            "schema": "schema_version",
                        }[kind]
                    ] = {"snapshot": "f" * 40, "domain": "feedback", "schema": "2.0"}[
                        kind
                    ]
                elif kind == "length":
                    root["decoded_bytes"] -= 1
                else:
                    import base64
                    import zlib

                    raw = canonical_bytes({"x": 1})
                    if kind == "noncanonical":
                        raw = b'{"x": 1}'
                    compressed = zlib.compress(raw)
                    if kind == "truncated":
                        compressed = compressed[:-1]
                    elif kind == "extra-stream":
                        compressed += zlib.compress(b"{}")
                    root, objects = packed({"x": 1})
                    storage_id = root["parts"][0]["storage_id"]
                    objects[storage_id]["data"] = base64.b64encode(compressed).decode()
                    wire = canonical_bytes(objects[storage_id])
                    root["parts"][0].update(
                        bytes=len(wire), sha256=hashlib.sha256(wire).hexdigest()
                    )
                    root["encoded_bytes"] = len(wire) + 512
                    root["decoded_bytes"] = len(raw)
                    root["sha256"] = hashlib.sha256(raw).hexdigest()
                    objects[storage_id]["inventory_sha256"] = root["sha256"]
                    wire = canonical_bytes(objects[storage_id])
                    root["parts"][0]["sha256"] = hashlib.sha256(wire).hexdigest()
                with self.assertRaises(ReviewInputError):
                    self.read(root, objects, context=context, producer=producer)

    def test_absolute_shared_deadline_and_reserved_fences(self):
        now = [0.0]
        budget = EvidenceReadBudget(clock=lambda: now[0])
        manifest, store = packed({"x": 1})
        budget.calls = 59
        self.read(manifest, store, budget=budget)
        with self.assertRaises(ReviewInputError):
            self.read(manifest, store, budget=budget)
        for _ in range(4):
            budget.consume(fence=True)
        with self.assertRaises(ReviewInputError):
            budget.consume(fence=True)
        expired = EvidenceReadBudget(clock=lambda: now[0])
        now[0] = 60
        with self.assertRaises(ReviewInputError):
            self.read(manifest, store, budget=expired)

    def test_restart_restores_original_calls_and_wall_deadline(self):
        original = EvidenceReadBudget(clock=lambda: 10, wall_clock=lambda: 1000)
        original.consume()
        restored = EvidenceReadBudget(
            clock=lambda: 500, wall_clock=lambda: 1020, snapshot=original.snapshot()
        )
        self.assertEqual(restored.calls, 1)
        self.assertEqual(restored.deadline, 540)
        self.assertEqual(restored.snapshot(), original.snapshot())
        with self.assertRaises(ReviewInputError):
            EvidenceReadBudget(wall_clock=lambda: 1060, snapshot=original.snapshot())

    def test_high_uptime_timeout_is_bounded_without_refilling_deadline(self):
        # Crossing a float exponent boundary rounds the sum upward by one ULP.
        start = float.fromhex("0x1.ffff100000003p+22")
        self.assertGreater(start + PART_READ_SECONDS - start, PART_READ_SECONDS)
        for restored in (False, True):
            with self.subTest(restored=restored):
                now = [start]
                snapshot = (
                    {"schema_version": "1.0", "calls": 7, "deadline_unix_ms": 1060000}
                    if restored
                    else None
                )
                budget = EvidenceReadBudget(
                    clock=lambda: now[0], wall_clock=lambda: 1000, snapshot=snapshot
                )
                deadline, wall_deadline = budget.deadline, budget.wall_deadline_ms
                original_calls = budget.calls
                self.assertEqual(budget.consume(), PART_READ_SECONDS)
                now[0] += 1
                self.assertEqual(budget.consume(), deadline - now[0])
                self.assertEqual(budget.deadline, deadline)
                self.assertEqual(budget.wall_deadline_ms, wall_deadline)
                self.assertEqual(budget.calls, original_calls + 2)
                now[0] = deadline
                with self.assertRaisesRegex(ReviewInputError, "deadline"):
                    budget.consume()
                self.assertEqual(budget.calls, original_calls + 2)

    def test_timeout_admission_uses_one_positive_deadline_relative_sample(self):
        samples = iter((0.0, 59.5, 60.0))
        budget = EvidenceReadBudget(
            clock=lambda: next(samples), wall_clock=lambda: 1000
        )
        self.assertEqual(budget.consume(), 0.5)
        self.assertEqual(budget.calls, 1)
        with self.assertRaisesRegex(ReviewInputError, "deadline"):
            budget.consume()
        self.assertEqual(budget.calls, 1)
        self.assertEqual(budget.deadline, 60.0)
        self.assertEqual(budget.wall_deadline_ms, 1060000)

    def test_whole_resource_preflight_has_no_writes_on_refusal(self):
        calls = []
        for source, limit, used in (
            ({"text": "x" * MAX_PARTITION_DECODED_BYTES}, 8192, 0),
            ({"x": 1}, 5, 0),
            ({"x": 1}, 8192, 55),
        ):
            budget = EvidenceReadBudget()
            budget.calls = used
            with self.assertRaises(ReviewInputError):
                stage_partitioned_evidence(
                    source,
                    binding=binding(),
                    item_count=1,
                    max_manifest_bytes=limit,
                    writer=lambda part: calls.append(part),
                    reader=lambda identity: None,
                    budget=budget,
                )
        self.assertEqual(calls, [])

    def test_no_paths_urls_booleans_or_unknown_shapes_as_authority(self):
        manifest, _ = packed({"x": 1})
        for identity in ("../x", "https://example.test/x", "", "a" * 65):
            root = copy.deepcopy(manifest)
            root["parts"][0]["storage_id"] = identity
            with self.assertRaises(ReviewInputError):
                validate_manifest(root)
        root = copy.deepcopy(manifest)
        root["item_count"] = True
        with self.assertRaises(ReviewInputError):
            validate_manifest(root)


class GitHubState:
    def __init__(self):
        self.comments = {}
        self.calls = []
        self.fail_part_get = False
        self.fail_root_patch = False
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def open(self, request, timeout):
        self.calls.append((request.method, request.full_url))
        parsed = urlparse(request.full_url)
        path = parsed.path
        if path.endswith(f"/issues/{fixture.IDENTITY.pull_request}/comments"):
            if request.method == "GET":
                query = parse_qs(parsed.query)
                per_page = int(query["per_page"][0])
                page = int(query["page"][0])
                items = list(self.comments.values())
                return json_response(items[(page - 1) * per_page : page * per_page])
            body = json.loads(request.data)["body"]
            identity = len(self.comments) + 1000
            item = {
                "id": identity,
                "body": body,
                "user": {"id": 55, "type": "Bot", "login": "sensei[bot]"},
                "issue_url": f"https://api.github.test/repos/{fixture.IDENTITY.repository}/issues/{fixture.IDENTITY.pull_request}",
            }
            self.comments[identity] = item
            return json_response(item, 201)
        if "/issues/comments/" in path:
            identity = int(path.rsplit("/", 1)[-1])
            if identity not in self.comments:
                return json_response({}, 404)
            item = self.comments[identity]
            if request.method == "GET":
                if self.fail_part_get and "evidence-part:v1" in item["body"]:
                    return json_response({}, 404)
                return json_response(item)
            if self.fail_root_patch:
                return json_response({}, 503)
            item["body"] = json.loads(request.data)["body"]
            return json_response(item)
        raise AssertionError((request.method, request.full_url))

    def ledger(self):
        return GitHubIssueCommentSessionLedger(
            self.http,
            token="synthetic",
            app_slug="sensei[bot]",
            evidence_budget=EvidenceReadBudget(),
            enable_partition_writes=True,
        )


def checkpoint(ledger, count):
    prepared = prepare_review_transaction(
        ledger,
        fixture.IDENTITY,
        fixture.POLICY,
        reservation_id="f" * 64,
        base_sha=fixture.BASE_SHA,
        head_sha=fixture.HEAD_SHA,
        configuration_digest=fixture.CONFIGURATION_DIGEST,
        evidence_digest=fixture.EVIDENCE_DIGEST,
        now=fixture.NOW,
    )
    baseline = replace(
        varied_baseline(count), generation=prepared.record.generation + 1
    )
    result = checkpoint_review_analysis(
        ledger,
        fixture.IDENTITY,
        prepared,
        fixture._result(),
        baseline=baseline,
        now=fixture.NOW,
    )
    return baseline, result


class AdapterTests(unittest.TestCase):
    def test_varied_12_21_50_100_250_round_trip_both_adapters_restart(self):
        for count in (12, 21, 50, 100, 250):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                local = LocalSessionLedger(
                    Path(directory), enable_partition_writes=True
                )
                baseline, result = checkpoint(local, count)
                restarted = LocalSessionLedger(Path(directory))
                record = restarted.load(fixture.IDENTITY, now=fixture.NOW).record
                self.assertEqual(read_session_baseline(restarted, record), baseline)
                state = GitHubState()
                hosted_baseline, hosted_result = checkpoint(state.ledger(), count)
                hosted = state.ledger()
                remote = hosted.load(fixture.IDENTITY, now=fixture.NOW).record
                self.assertEqual(read_session_baseline(hosted, remote), hosted_baseline)
                if count >= 100:
                    self.assertEqual(
                        record.convergence_history["baseline"]["encoding"],
                        PARTITION_ENCODING,
                    )
                    with self.assertRaises(ReviewInputError):
                        baseline_from_history_document(
                            record.convergence_history["baseline"]
                        )
                self.assertEqual(
                    admission_context_from_document(
                        admission_context_document(baseline, baseline.cache_key)
                    )[0],
                    baseline,
                )
                completed = complete_review_publication(
                    hosted,
                    fixture.IDENTITY,
                    hosted_result.transaction,
                    published=True,
                    now=fixture.NOW,
                )
                self.assertEqual(
                    read_session_baseline(hosted, completed), hosted_baseline
                )

    def test_missing_parts_are_integrity_failure_never_empty_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            checkpoint(ledger, 250)
            part = next(Path(directory).glob(".evidence/*/*/*"))
            part.unlink()
            restarted = LocalSessionLedger(Path(directory))
            self.assertEqual(
                restarted.load(fixture.IDENTITY, now=fixture.NOW).status,
                "integrity-failed",
            )
            with self.assertRaises(ReviewInputError):
                restarted.initialize(fixture.IDENTITY, now=fixture.NOW)
        state = GitHubState()
        checkpoint(state.ledger(), 250)
        part_id = next(
            identity
            for identity, item in state.comments.items()
            if "evidence-part:v1" in item["body"]
        )
        del state.comments[part_id]
        self.assertEqual(
            state.ledger().load(fixture.IDENTITY, now=fixture.NOW).status,
            "integrity-failed",
        )

    def test_github_wrong_author_wrong_pr_and_unsupported_reader_withhold(self):
        for changed in ("author", "association", "no-budget"):
            with self.subTest(changed=changed):
                state = GitHubState()
                checkpoint(state.ledger(), 250)
                part = next(
                    item
                    for item in state.comments.values()
                    if "evidence-part:v1" in item["body"]
                )
                if changed == "author":
                    part["user"]["id"] = 56
                elif changed == "association":
                    part["issue_url"] = (
                        "https://api.github.test/repos/foreign/repo/issues/1"
                    )
                ledger = (
                    state.ledger()
                    if changed != "no-budget"
                    else GitHubIssueCommentSessionLedger(
                        state.http, token="synthetic", app_slug="sensei[bot]"
                    )
                )
                self.assertEqual(
                    ledger.load(fixture.IDENTITY, now=fixture.NOW).status,
                    "integrity-failed",
                )

    def test_part_readback_failure_and_root_failure_leave_prior_authority_and_reuse_orphans(
        self,
    ):
        for fault in ("readback", "activation"):
            with self.subTest(fault=fault):
                state = GitHubState()
                ledger = state.ledger()
                prepared = prepare_review_transaction(
                    ledger,
                    fixture.IDENTITY,
                    fixture.POLICY,
                    reservation_id="f" * 64,
                    base_sha=fixture.BASE_SHA,
                    head_sha=fixture.HEAD_SHA,
                    configuration_digest=fixture.CONFIGURATION_DIGEST,
                    evidence_digest=fixture.EVIDENCE_DIGEST,
                    now=fixture.NOW,
                )
                baseline = replace(
                    varied_baseline(250), generation=prepared.record.generation + 1
                )
                old_root = copy.deepcopy(next(iter(state.comments.values())))
                state.fail_part_get = fault == "readback"
                state.fail_root_patch = fault == "activation"
                with self.assertRaises(Exception):
                    checkpoint_review_analysis(
                        ledger,
                        fixture.IDENTITY,
                        prepared,
                        fixture._result(),
                        baseline=baseline,
                        now=fixture.NOW,
                    )
                self.assertEqual(state.comments[old_root["id"]], old_root)
                staged = len(state.comments)
                state.fail_part_get = state.fail_root_patch = False
                resumed = state.ledger()
                checkpoint_review_analysis(
                    resumed,
                    fixture.IDENTITY,
                    prepared,
                    fixture._result(),
                    baseline=baseline,
                    now=fixture.NOW,
                )
                self.assertEqual(len(state.comments), staged)
                reader = state.ledger()
                record = reader.load(fixture.IDENTITY, now=fixture.NOW).record
                self.assertEqual(read_session_baseline(reader, record), baseline)

    def test_complete_document_preserves_500_paths_and_escaped_unicode(self):
        original = replace(
            varied_baseline(250),
            reviewed_paths=tuple(
                sorted(
                    f"src/深🙂/{index}/" + "long" * 20 + "/worker.py"
                    for index in range(500)
                )
            ),
        )
        document = baseline_complete_document(original)
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            root = ledger.stage_evidence(
                fixture.IDENTITY,
                binding=binding(generation=original.generation),
                document=document,
                item_count=250,
                max_manifest_bytes=8192,
            )
            self.assertEqual(
                baseline_from_history_document(
                    root,
                    reader=lambda manifest: ledger.read_evidence(
                        fixture.IDENTITY,
                        manifest,
                        expected_binding=binding(generation=original.generation),
                    ),
                ),
                original,
            )

    def test_local_symlink_and_retention_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory), enable_partition_writes=True)
            outside = Path(directory) / "outside"
            outside.mkdir()
            (Path(directory) / ".evidence").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaises(ReviewInputError):
                ledger.stage_evidence(
                    fixture.IDENTITY,
                    binding=binding(),
                    document={"x": 1},
                    item_count=1,
                    max_manifest_bytes=8192,
                )
