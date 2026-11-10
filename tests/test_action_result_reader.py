"""Prospective result reads authenticate a current root; they never settle work."""

from __future__ import annotations

import base64
import copy
import hashlib
import tempfile
import unittest
import zlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import urlsplit

import test_api_work_actions as native_actions
from fake_github_http import make_http
from review_sensei_github import api_run_history
from review_sensei_github.errors import (
    GitHubHTTPTransientError,
    GitHubPublicationTransientError,
)
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    render_session_comment,
)
from test_api_work_persistence import IDENTITY, json_response

from review_sensei.api_work import (
    ACCOUNTING_VERSION,
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
)
from review_sensei.bounded_evidence import (
    MAX_MANIFEST_BYTES,
    MAX_PART_READS,
    MAX_PARTITION_DECODED_BYTES,
    PART_ENCODING,
    EvidenceReadBudget,
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
)
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.session import SessionRecord
from tests.test_api_work import accounting

PROSPECTIVE_VERSION = "caller-requests-v3"


class ActionResultReaderTests(unittest.TestCase):
    def partitioned(
        self,
        directory,
        *,
        envelope=None,
        payload_builder=None,
        outcome=native_actions.OUTCOME,
    ):
        helper = native_actions.NativeActionTests()
        fixture, ledger = helper.prepare(directory)
        _intent, _boundary, _proof, record = helper.complete(
            fixture, ledger, outcome=outcome
        )
        original = api_run_history.read_action_result(ledger, IDENTITY, record)
        envelope = (
            envelope(original)
            if callable(envelope)
            else original
            if envelope is None
            else envelope
        )
        state = ApiRunAccounting.from_document(record.api_run_accounting)
        binding = api_run_history.action_result_binding(
            IDENTITY, state, ledger.evidence_producer()
        )
        if payload_builder is None:
            parts = partition_evidence(envelope, binding=binding, item_count=1)
        else:
            payload = payload_builder(envelope)
            parts = (
                {
                    "encoding": PART_ENCODING,
                    "binding_sha256": hashlib.sha256(
                        canonical_bytes(binding)
                    ).hexdigest(),
                    "inventory_sha256": payload["sha256"],
                    "index": 0,
                    "count": 1,
                    "data": payload["data"],
                },
            )
        self.assertEqual(len(parts), 1)
        storage_id = "7000000"
        manifest = evidence_manifest(
            envelope,
            binding=binding,
            item_count=1,
            parts=parts,
            storage_ids=(storage_id,),
        )
        item = copy.deepcopy(fixture.comment)
        item.update(
            id=int(storage_id),
            body="ReviewSensei immutable evidence part v1\n```json\n"
            + canonical_bytes(parts[0]).decode()
            + "\n```\n<!-- reviewsensei:evidence-part:v1 -->",
        )
        fixture.extra_comments[int(storage_id)] = item
        prospective = replace(
            state,
            version=PROSPECTIVE_VERSION,
            action={**state.action, "result": manifest},
        )
        # A new synthetic stored document, never an upgrade of a V2 run.
        record = SessionRecord.create(IDENTITY).evolve(
            operator_paused=record.operator_paused,
            api_run_accounting=prospective.to_document(),
        )
        fixture.comment["body"] = render_session_comment(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            record=record,
        )
        ledger._list_refresh = True
        return fixture, ledger, record, envelope

    def publish(self, fixture, ledger, state):
        record = SessionRecord.create(IDENTITY).evolve(
            api_run_accounting=state.to_document()
        )
        fixture.comment["body"] = render_session_comment(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            record=record,
        )
        ledger._list_refresh = True
        return record

    def read(self, ledger, *, context=None):
        return api_run_history.read_current_action_result(
            ledger,
            IDENTITY,
            context=context
            or api_run_history.ActionResultReadContext(
                IDENTITY, ledger.evidence_budget
            ),
        )

    def test_prospective_one_part_reads_owned_current_root_with_unchanged_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger, record, expected = self.partitioned(directory)
            before = fixture.accounted.accounting
            context = api_run_history.ActionResultReadContext(
                IDENTITY, ledger.evidence_budget
            )
            actual = api_run_history.read_current_action_result(
                ledger, IDENTITY, context=context
            )
            self.assertEqual(actual, expected)
            before.require_successor(fixture.accounted.accounting)
            self.assertEqual(
                ApiRunAccounting.from_document(record.api_run_accounting).version,
                PROSPECTIVE_VERSION,
            )

    def test_full_summary_domain_is_read_losslessly(self):
        def full_summary(value):
            return {**value, "result": {**value["result"], "summary": "x" * 4094}}

        with tempfile.TemporaryDirectory() as directory:
            _fixture, ledger, _record, expected = self.partitioned(
                directory, envelope=full_summary
            )
            self.assertEqual(len(canonical_bytes(expected["result"]["summary"])), 4096)
            self.assertEqual(self.read(ledger), expected)

    def test_binding_substitutions_refuse_before_part_read(self):
        changes = {
            "repository": "other/repo",
            "repository_id": 999,
            "pull_request": 99,
            "head_sha": "f" * 40,
            "configuration_digest": "f" * 64,
            "policy_digest": "f" * 64,
            "generation": 99,
            "producer": "github-bot:888888",
        }
        for field, changed in changes.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture, ledger, record, _ = self.partitioned(directory)
                state = ApiRunAccounting.from_document(record.api_run_accounting)
                action = copy.deepcopy(state.action)
                action["result"]["binding"][field] = changed
                self.publish(fixture, ledger, replace(state, action=action))
                with (
                    patch.object(ledger, "_read_part", wraps=ledger._read_part) as read,
                    self.assertRaises(ReviewInputError),
                ):
                    self.read(ledger)
                read.assert_not_called()

    def test_changed_authenticated_intent_config_owner_or_run_cannot_reuse_part(self):
        for change in ("run", "source", "config", "owner"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, ledger, record, _ = self.partitioned(directory)
                state = ApiRunAccounting.from_document(record.api_run_accounting)
                action = copy.deepcopy(state.action)
                arguments = {"action": action}
                if change == "run":
                    arguments["identity"] = ApiRunIdentity(IDENTITY.repository_id, 457)
                    action["intent"]["run_id"] = 457
                elif change == "source":
                    action["intent"]["source"]["body_sha256"] = "e" * 64
                elif change == "config":
                    arguments["configuration"] = FrozenApiConfiguration(
                        state.configuration.commit, "e" * 64, state.configuration.limit
                    )
                    action["intent"]["configuration_digest"] = "e" * 64
                else:
                    owner = ApiExecutionOwner(
                        state.owner.run_attempt,
                        state.owner.job,
                        state.owner.generation + 1,
                    )
                    arguments["owner"] = owner
                    action["intent"]["owner"]["generation"] = owner.generation
                self.publish(fixture, ledger, replace(state, **arguments))
                with self.assertRaises(ReviewInputError):
                    self.read(ledger)

    def test_codec_integrity_does_not_authorize_wrong_intent_outcome_or_envelope(self):
        for change in ("intent", "outcome", "extra", "nested_reference"):

            def alter(value):
                if change == "intent":
                    return {**value, "intent_sha256": "e" * 64}
                if change == "outcome":
                    return {
                        **value,
                        "outcome": {"kind": "already_applied", "reason": None},
                    }
                if change == "extra":
                    return {**value, "unexpected": True}
                return {
                    "encoding": "partitioned-json-v1",
                    "parts": [{"storage_id": "7000000"}],
                }

            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                _fixture, ledger, _record, _ = self.partitioned(
                    directory, envelope=alter
                )
                with (
                    patch.object(ledger, "_read_part", wraps=ledger._read_part) as read,
                    self.assertRaises(ReviewInputError),
                ):
                    self.read(ledger)
                self.assertEqual(read.call_count, 1)

    def test_part_missing_foreign_corrupt_or_wrong_pr_never_returns_a_result(self):
        for change in ("missing", "foreign", "corrupt", "wrong_pr"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, ledger, _record, _ = self.partitioned(directory)
                if change == "missing":
                    del fixture.extra_comments[7000000]
                elif change == "foreign":
                    fixture.extra_comments[7000000]["user"] = {
                        "id": 888,
                        "login": "other",
                        "type": "Bot",
                    }
                elif change == "wrong_pr":
                    fixture.extra_comments[7000000]["issue_url"] = (
                        "https://api.github.test/repos/owner/repo/issues/99"
                    )
                else:
                    item = fixture.extra_comments[7000000]
                    item["body"] = item["body"].replace('"data":"', '"data":"AAAA', 1)
                original = fixture.accounted._opener

                def external(request, *, timeout):
                    if change == "missing" and urlsplit(request.full_url).path.endswith(
                        "/issues/comments/7000000"
                    ):
                        fixture.trace.append(
                            (
                                fixture.attempt,
                                request.get_method(),
                                urlsplit(request.full_url).path,
                            )
                        )
                        return json_response({}, status=404)
                    return original(request, timeout=timeout)

                with (
                    patch.object(fixture.accounted, "_opener", external),
                    self.assertRaises(ReviewInputError),
                ):
                    self.read(ledger)

    def test_one_item_one_part_and_legacy_inline_only_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            _fixture, _ledger, record, _ = self.partitioned(directory)
            document = copy.deepcopy(record.api_run_accounting)
            for change in ("items", "parts", "legacy1", "legacy2"):
                broken = copy.deepcopy(document)
                if change == "items":
                    broken["action"]["result"]["item_count"] = 2
                elif change == "parts":
                    broken["action"]["result"]["parts"] *= 2
                else:
                    broken["version"] = (
                        "caller-requests-v1"
                        if change == "legacy1"
                        else "caller-requests-v2"
                    )
                    if change == "legacy1":
                        broken["completed"] = [
                            {"request_id": p[0], "deadline_unix_ms": p[1]}
                            for p in broken["completed"]
                        ]
                with self.subTest(change=change), self.assertRaises(ReviewInputError):
                    ApiRunAccounting.from_document(broken)

    def test_cycle_depth_and_budget_scope_are_downward_only(self):
        with tempfile.TemporaryDirectory() as directory:
            _fixture, ledger, _record, _ = self.partitioned(directory)
            cycle = api_run_history.ActionResultReadContext(
                IDENTITY, ledger.evidence_budget, max_depth=2
            )
            self.read(ledger, context=cycle)
            with self.assertRaisesRegex(ReviewInputError, "cycle"):
                self.read(ledger, context=cycle)
            depth = api_run_history.ActionResultReadContext(
                IDENTITY, ledger.evidence_budget
            )
            self.read(ledger, context=depth)
            with (
                patch.object(ledger, "load", wraps=ledger.load) as load,
                self.assertRaisesRegex(ReviewInputError, "depth exhausted"),
            ):
                self.read(ledger, context=depth)
            load.assert_not_called()
            other = api_run_history.ActionResultReadContext(
                IDENTITY, EvidenceReadBudget()
            )
            with self.assertRaisesRegex(ReviewInputError, "scope changed"):
                self.read(ledger, context=other)
            for invalid in (0, True, MAX_PART_READS + 1):
                with self.subTest(invalid=invalid), self.assertRaises(ReviewInputError):
                    api_run_history.ActionResultReadContext(
                        IDENTITY, ledger.evidence_budget, max_depth=invalid
                    )

    def test_lost_part_get_preserves_pending_request_and_cannot_retry_in_context(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger, _record, _ = self.partitioned(directory)
            caller = fixture.accounted
            original = caller._opener
            lost = []

            def opener(request, *, timeout):
                if urlsplit(request.full_url).path.endswith("/issues/comments/7000000"):
                    fixture.trace.append(
                        (
                            fixture.attempt,
                            request.get_method(),
                            urlsplit(request.full_url).path,
                        )
                    )
                    lost.append(caller.observations[-1]["request_id"])
                    raise URLError("synthetic lost read response")
                return original(request, timeout=timeout)

            context = api_run_history.ActionResultReadContext(
                IDENTITY, ledger.evidence_budget, max_depth=2
            )
            before = caller.accounting
            with (
                patch.object(caller, "_opener", opener),
                self.assertRaises(GitHubPublicationTransientError),
            ):
                self.read(ledger, context=context)
            self.assertEqual(len(lost), 1)
            self.assertEqual(caller.accounting.uncertain, before.uncertain + 1)
            self.assertTrue(
                any(
                    p.request_id == lost[0]
                    for b in caller.accounting.blocks
                    for p in b.pending
                )
            )
            with self.assertRaisesRegex(ReviewInputError, "cycle"):
                self.read(ledger, context=context)
            self.assertEqual(len(lost), 1)

    def test_pending_or_orphan_part_does_not_supply_terminal_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger, record, _ = self.partitioned(directory)
            state = ApiRunAccounting.from_document(record.api_run_accounting)
            pending = replace(
                state,
                action={
                    **state.action,
                    "phase": "pending",
                    "result": None,
                    "outcome": None,
                },
            )
            self.publish(fixture, ledger, pending)
            with (
                patch.object(ledger, "_read_part", wraps=ledger._read_part) as read,
                self.assertRaisesRegex(ReviewInputError, "no terminal"),
            ):
                self.read(ledger)
            read.assert_not_called()

    def test_original_encoded_payload_limit_is_checked_before_inflation(self):
        # A valid stored zlib block makes the adjacent payload sizes independent
        # of compression heuristics. Base64 advances in four-byte steps.
        measured = []
        for extra in (0, 3):
            payload_sizes = []

            def document(value):
                value = {**value, "result": {"blob": ""}}
                base = len(canonical_bytes(value))
                value["result"]["blob"] = "x" * (6031 + extra - base)
                return value

            def payload(value):
                raw = canonical_bytes(value)
                result = {
                    "encoding": "zlib-json-v1",
                    "decoded_bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "data": base64.b64encode(zlib.compress(raw, level=0)).decode(),
                }
                payload_sizes.append(len(canonical_bytes(result)))
                return result

            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                _fixture, ledger, _record, expected = self.partitioned(
                    directory, envelope=document, payload_builder=payload
                )
                size = payload_sizes[0]
                measured.append(size)
                if size <= MAX_MANIFEST_BYTES:
                    self.assertEqual(self.read(ledger), expected)
                else:
                    with self.assertRaisesRegex(ReviewInputError, "encoded limit"):
                        self.read(ledger)
        self.assertEqual(measured, [8190, 8194])

    def test_original_decoded_limit_and_overdeclared_stream_refusal(self):
        def maximum(value):
            value = {**value, "result": {"blob": ""}}
            value["result"]["blob"] = "x" * (
                MAX_PARTITION_DECODED_BYTES - len(canonical_bytes(value))
            )
            return value

        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger, record, expected = self.partitioned(
                directory, envelope=maximum
            )
            self.assertEqual(
                len(canonical_bytes(expected)), MAX_PARTITION_DECODED_BYTES
            )
            self.assertEqual(self.read(ledger), expected)
            document = copy.deepcopy(record.api_run_accounting)
            document["action"]["result"]["decoded_bytes"] += 1
            with self.assertRaises(ReviewInputError):
                ApiRunAccounting.from_document(document)

    def test_failed_root_read_consumes_the_context_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            _fixture, ledger, _record, _ = self.partitioned(directory)
            context = api_run_history.ActionResultReadContext(
                IDENTITY, ledger.evidence_budget
            )
            with patch.object(
                ledger, "load", side_effect=GitHubHTTPTransientError("lost root read")
            ) as load:
                with self.assertRaises(GitHubHTTPTransientError):
                    self.read(ledger, context=context)
                with self.assertRaisesRegex(ReviewInputError, "depth exhausted"):
                    self.read(ledger, context=context)
            self.assertEqual(load.call_count, 1)

    def test_new_read_reauthenticates_current_root_instead_of_reusing_cached_terminal(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger, record, _ = self.partitioned(directory)
            self.read(ledger)
            state = ApiRunAccounting.from_document(record.api_run_accounting)
            pending = replace(
                state,
                action={
                    **state.action,
                    "phase": "pending",
                    "result": None,
                    "outcome": None,
                },
            )
            changed = SessionRecord.create(IDENTITY).evolve(
                api_run_accounting=pending.to_document()
            )
            fixture.comment["body"] = render_session_comment(
                repository_id=IDENTITY.repository_id,
                pull_request=IDENTITY.pull_request,
                record=changed,
            )
            with self.assertRaisesRegex(ReviewInputError, "no terminal"):
                self.read(ledger)

    def test_terminal_classes_and_mixed_current_versions_share_original_context(self):
        for outcome in (
            native_actions.OUTCOME,
            {"kind": "already_applied", "reason": None},
            {"kind": "explicit_noop", "reason": "status"},
        ):
            with (
                self.subTest(outcome=outcome),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture, ledger, record, expected = self.partitioned(
                    directory, outcome=outcome
                )
                context = api_run_history.ActionResultReadContext(
                    IDENTITY, ledger.evidence_budget, max_depth=3
                )
                for version in ("caller-requests-v1", "caller-requests-v2"):
                    self.publish(
                        fixture,
                        ledger,
                        replace(fixture.accounted.accounting, version=version),
                    )
                    self.assertEqual(self.read(ledger, context=context), expected)
                self.publish(
                    fixture,
                    ledger,
                    ApiRunAccounting.from_document(record.api_run_accounting),
                )
                self.assertEqual(self.read(ledger, context=context), expected)
                with self.assertRaisesRegex(ReviewInputError, "depth exhausted"):
                    self.read(ledger, context=context)

    def test_v3_inline_is_explicit_and_native_activation_remains_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger, record, expected = self.partitioned(directory)
            inline_state = replace(
                fixture.accounted.accounting, version=PROSPECTIVE_VERSION
            )
            inline_root = self.publish(fixture, ledger, inline_state)
            self.assertEqual(self.read(ledger), expected)
            with self.assertRaisesRegex(ReviewInputError, "explicit read-only reader"):
                api_run_history.read_action_result(ledger, IDENTITY, inline_root)
            caller = fixture.accounted
            # Simulate an unactivated reader of this prospective stored root.
            caller._state = None
            caller._activation = record, "hosted-review"
            with self.assertRaisesRegex(ReviewInputError, "reader-only"):
                caller._accept_activation(ledger, record)
            self.assertIsNone(caller._state)

    def test_exhausted_original_tranche_refuses_without_physical_read(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, ledger, _record, _ = self.partitioned(directory)
            caller = fixture.accounted
            for index in range(caller.usable):
                caller._state = caller.accounting.dispatch(
                    caller._block, PendingApiRequest(f"reserved-unknown-{index}", 123)
                )
            before = caller.accounting
            with (
                patch.object(caller, "_opener", wraps=caller._opener) as opener,
                self.assertRaises(EvidenceBudgetExhausted),
            ):
                self.read(ledger)
            opener.assert_not_called()
            self.assertEqual(caller.accounting, before)


class ProspectiveAccountingTests(unittest.TestCase):
    def test_plain_native_reenroll_refuses_expired_v3_before_patch(self):
        state = replace(accounting(), version=PROSPECTIVE_VERSION)
        now = datetime.now(timezone.utc)
        record = SessionRecord.create(
            IDENTITY,
            now=now - timedelta(days=2),
            expires_at=now - timedelta(days=1),
        ).evolve(api_run_accounting=state.to_document(), now=now - timedelta(days=2))
        body = render_session_comment(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            record=record,
        )
        http, calls = make_http(
            [json_response([{"id": 7, "body": body}]) for _ in range(2)]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="synthetic")
        with self.assertRaisesRegex(ReviewInputError, "reader-only"):
            ledger.reenroll(IDENTITY, now=now)
        self.assertEqual([method for method, _, _ in calls], ["GET", "GET"])

    def test_plain_native_replace_refuses_v3_before_mutation_or_write(self):
        state = replace(accounting(), version=PROSPECTIVE_VERSION)
        record = SessionRecord.create(IDENTITY).evolve(
            api_run_accounting=state.to_document()
        )
        body = render_session_comment(
            repository_id=IDENTITY.repository_id,
            pull_request=IDENTITY.pull_request,
            record=record,
        )
        http, calls = make_http(
            [json_response([{"id": 7, "body": body}]) for _ in range(2)]
        )
        ledger = GitHubIssueCommentSessionLedger(http, token="synthetic")
        mutate = Mock(side_effect=lambda current: current.evolve(operator_paused=True))
        with self.assertRaisesRegex(ReviewInputError, "reader-only"):
            ledger.replace(IDENTITY, mutate)
        mutate.assert_not_called()
        self.assertEqual([method for method, _, _ in calls], ["GET", "GET"])

    def test_plain_native_initialization_refuses_outgoing_v3_before_post(self):
        state = replace(accounting(), version=PROSPECTIVE_VERSION)
        http, calls = make_http([json_response([])])
        ledger = GitHubIssueCommentSessionLedger(http, token="synthetic")
        with self.assertRaisesRegex(ReviewInputError, "reader-only"):
            ledger.initialize_with_mutation(
                IDENTITY,
                lambda record: record.evolve(api_run_accounting=state.to_document()),
            )
        self.assertEqual([method for method, _, _ in calls], ["GET"])

    def test_v1_v2_wire_and_writer_default_stay_exact_while_v3_is_read_only(self):
        self.assertEqual(ACCOUNTING_VERSION, "caller-requests-v2")
        state = accounting().reserve("work", 3)
        done = PendingApiRequest("done", 123)
        unknown = PendingApiRequest("unknown", 456)
        state = state.dispatch("work", done).confirm(done).dispatch("work", unknown)
        for version in (
            "caller-requests-v1",
            "caller-requests-v2",
            PROSPECTIVE_VERSION,
        ):
            with self.subTest(version=version):
                selected = replace(state, version=version)
                wire = selected.to_document()
                expected = (
                    {"request_id": "done", "deadline_unix_ms": 123}
                    if version.endswith("v1")
                    else ["done", 123]
                )
                self.assertEqual(wire["completed"], [expected])
                self.assertEqual(
                    wire["blocks"][0]["pending"],
                    [{"request_id": "unknown", "deadline_unix_ms": 456}],
                )
                self.assertEqual(
                    ApiRunAccounting.from_document(wire).to_document(), wire
                )
        prospective = replace(state, version=PROSPECTIVE_VERSION)
        for operation in (
            lambda: prospective.reserve("new", 1),
            lambda: prospective.dispatch("work", PendingApiRequest("new", 123)),
            lambda: prospective.confirm(unknown),
            lambda: prospective.claim_startup(ApiExecutionOwner(2, "analyze", 1)),
        ):
            with self.assertRaisesRegex(ReviewInputError, "reader-only"):
                operation()
        with self.assertRaisesRegex(ReviewInputError, "cannot change within a run"):
            state.require_successor(prospective)

    def test_explicit_reader_preserves_legacy_inline_result_and_original_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = native_actions.NativeActionTests()
            fixture, ledger = helper.prepare(directory)
            _intent, _boundary, _proof, record = helper.complete(fixture, ledger)
            before = fixture.accounted.accounting
            context = api_run_history.ActionResultReadContext(
                IDENTITY, ledger.evidence_budget
            )
            value = api_run_history.read_current_action_result(
                ledger, IDENTITY, context=context
            )
            self.assertEqual(value["outcome"], native_actions.OUTCOME)
            self.assertEqual(value["result"], {"action": "pause", "applied": True})
            self.assertIs(ledger.evidence_budget, context.budget)
            self.assertEqual(
                api_run_history.read_action_result(ledger, IDENTITY, record), value
            )
            self.assertEqual(
                fixture.accounted.accounting.configuration, before.configuration
            )
            before.require_successor(fixture.accounted.accounting)


if __name__ == "__main__":
    unittest.main()
