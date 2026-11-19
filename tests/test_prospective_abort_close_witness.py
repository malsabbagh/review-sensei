"""Offline source qualification for one prospective G29 close, not activation."""

from __future__ import annotations

import json
import unittest
from dataclasses import asdict, replace
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.errors import GitHubHTTPTransientError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.prospective_abort_close import (
    AbortCloseTarget,
    observe_exact_abort_close,
    prepare_abort_close,
)
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
    StartupPlan,
    StartupSlot,
    initial_accounting,
)
from review_sensei.bounded_evidence import (
    MAX_PART_BYTES,
    canonical_bytes,
    partition_evidence,
)
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import (
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_RECORD_BYTES,
    SessionIdentity,
    SessionRecord,
    mutate_abort,
    mutate_commit,
    mutate_reserved,
)

NOW = datetime(2026, 11, 18, 0, tzinfo=UTC)
DEADLINE = 1_795_000_000_000
IDENTITY = SessionIdentity("owner/repo", 7, 123)
TARGET = AbortCloseTarget(IDENTITY, "https://api.github.test", 76, "sensei[bot]", 98)
BLOCK = "hosted-terminal"


def precursor(*, terminal: int = 4, limit: int = 512):
    state = initial_accounting(
        ApiRunIdentity(123, 456),
        FrozenApiConfiguration("a" * 40, "a" * 64, limit),
        ApiExecutionOwner(1, "hosted", 0),
        StartupPlan(
            (StartupSlot("hosted", 1, ("s1",)), StartupSlot("hosted", 2, ("s2",)))
        ),
        prior_nonstartup_liability_upper_bound=0,
    ).reserve(BLOCK, terminal)
    tx = ReviewTransaction.create(
        repository=IDENTITY.repository,
        pull_request=IDENTITY.pull_request,
        base_sha="b" * 40,
        head_sha="c" * 40,
        policy_digest="d" * 64,
        configuration_digest="e" * 64,
        evidence_digest="f" * 64,
        reservation_id="c" * 64,
        generation=1,
    )
    source = SessionRecord.create(IDENTITY, now=NOW).evolve(
        now=NOW,
        generation=1,
        transaction=tx,
        reservation_id=tx.reservation_id,
        reserved_slot="initial",
        api_run_accounting=state.to_document(),
    )
    cleaned = mutate_abort(
        source, reservation_id=tx.reservation_id, expected_generation=1, now=NOW
    )
    closure = replace(
        state,
        analysis_abort={
            "run_id": state.identity.run_id,
            "owner": asdict(state.owner),
            "source_root": source.record_sha256,
            "cleanup_root": cleaned.record_sha256,
            "entry_state": "not_started",
            "transaction": tx.to_dict(),
        },
    )
    return cleaned, closure


def native_row(body, **changes):
    return {
        "id": 76,
        "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
        "user": {"id": 98, "login": "sensei[bot]", "type": "Bot"},
        "body": body,
        **changes,
    }


def render(record):
    return render_session_comment(repository_id=123, pull_request=7, record=record)


def prepare_fixture(cleanup, state, *, sequence=16, deadline=DEADLINE):
    return prepare_abort_close(
        TARGET,
        cleanup=cleanup,
        cleanup_row=native_row(render(cleanup)),
        accounting=state,
        sequence=sequence,
        deadline=deadline,
        now=NOW,
    )


class ProspectiveAbortCloseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch("socket.socket", side_effect=AssertionError("offline fixture"))
        )

    def prepare(self, *, terminal=4):
        cleanup, source = precursor(terminal=terminal)
        prepared = prepare_fixture(cleanup, source)
        return cleanup, source, prepared

    def test_exact_wire_echo_is_one_paid_effect_not_an_http_response_or_authority(self):
        _, source, prepared = self.prepare()
        pending = prepared.pending
        encoded = ApiRunAccounting.from_document(prepared.record.api_run_accounting)
        self.assertEqual(encoded, source.dispatch(BLOCK, pending))
        self.assertEqual(pending.deadline_unix_ms, DEADLINE)
        self.assertEqual(encoded.remaining, source.remaining)
        self.assertEqual(encoded.confirmed, source.confirmed)
        self.assertEqual(
            encoded.reserved_undispatched, source.reserved_undispatched - 1
        )
        self.assertEqual(encoded.uncertain, 1)
        trace = []
        storage = {}

        def native(request, timeout):
            method, path = request.get_method(), urlsplit(request.full_url).path
            trace.append((method, path))
            if method == "PATCH":
                self.assertEqual(urlsplit(request.full_url).netloc, "api.github.test")
                storage.update(native_row(json.loads(request.data)["body"]))
                raise TimeoutError("fixture lost PATCH acknowledgement")
            return json_response(storage)

        writer = GitHubHttp(api_url=TARGET.api_url, opener=native)
        with self.assertRaises(GitHubHTTPTransientError):
            writer.request(
                "PATCH",
                TARGET.patch_path,
                token="synthetic",
                body={"body": prepared.body},
            )
        reader = GitHubHttp(api_url=TARGET.api_url, opener=native)
        status, row = reader.request("GET", TARGET.patch_path, token="synthetic")
        self.assertEqual(status, 200)
        selected = parse_session_comment(row["body"], identity=IDENTITY)
        self.assertEqual(selected, prepared.record)
        self.assertIsNot(selected, prepared.record)
        proof = observe_exact_abort_close(TARGET, selected=selected, row=row)
        self.assertFalse(proof.authority)
        self.assertFalse(proof.confirms_original_response)
        self.assertEqual(proof.request_id, pending.request_id)
        self.assertEqual(proof.root_sha256, prepared.record.record_sha256)
        self.assertEqual(proof.run_id, 456)
        self.assertEqual(proof.original_deadline_unix_ms, DEADLINE)
        self.assertLessEqual(
            len(canonical_bytes(prepared.record.to_dict())), MAX_SESSION_RECORD_BYTES
        )
        self.assertLessEqual(len(prepared.body.encode()), MAX_SESSION_COMMENT_BYTES)
        self.assertLessEqual(prepared.archive_body_bytes, MAX_PART_BYTES)
        # Normalization must not change the selected record's own pending row.
        self.assertEqual(render(prepared.record), prepared.body)
        self.assertEqual(
            trace, [("PATCH", TARGET.patch_path), ("GET", TARGET.patch_path)]
        )
        self.assertEqual(
            ApiRunAccounting.from_document(
                prepared.record.api_run_accounting
            ).uncertain,
            1,
        )
        self.assertEqual(source.uncertain, 0)

    def test_absent_before_storage_supplies_no_close_effect_proof(self):
        cleanup, _, prepared = self.prepare()
        for visible in (None, native_row(render(cleanup))):
            with (
                self.subTest(visible=visible is not None),
                self.assertRaisesRegex(ReviewInputError, "native echo is not exact"),
            ):
                observe_exact_abort_close(TARGET, selected=prepared.record, row=visible)

    def test_exact_bot_pr_comment_origin_and_canonical_echo_are_independent(self):
        _, _, prepared = self.prepare()
        body = prepared.body
        reflowed = body.replace("```json\n{", "```json\n{ ", 1)
        self.assertEqual(
            parse_session_comment(reflowed, identity=IDENTITY), prepared.record
        )
        for wrong in (
            native_row(body, id=True),
            native_row(body, id=77),
            native_row(body, issue_url=TARGET.issue_url + "0"),
            native_row(body, user=None),
            native_row(body, user={"id": 98, "login": "sensei[bot]", "type": "User"}),
            native_row(body, user={"id": 97, "login": "sensei[bot]", "type": "Bot"}),
            native_row(body, user={"id": 98, "login": "other[bot]", "type": "Bot"}),
            native_row(reflowed),
            native_row("arbitrary prefix\n" + body),
        ):
            with (
                self.subTest(wrong=wrong.get("id")),
                self.assertRaisesRegex(ReviewInputError, "native echo is not exact"),
            ):
                observe_exact_abort_close(TARGET, selected=prepared.record, row=wrong)
        # Even a consistent target+echo choice cannot move the committed
        # operation to a different origin, Bot or physical comment ID.
        for foreign in (
            replace(TARGET, api_url="https://other.example"),
            replace(TARGET, comment_id=77),
            replace(TARGET, producer_id=97),
            replace(TARGET, app_login="other[bot]"),
            replace(TARGET, identity=replace(IDENTITY, pull_request=8)),
        ):
            forged = native_row(
                body,
                id=foreign.comment_id,
                issue_url=foreign.issue_url,
                user={
                    "id": foreign.producer_id,
                    "login": foreign.app_login,
                    "type": "Bot",
                },
            )
            with self.subTest(foreign=foreign), self.assertRaises(ReviewInputError):
                observe_exact_abort_close(foreign, selected=prepared.record, row=forged)

    def test_old_opaque_debits_and_follow_on_checkpoints_are_not_reclassified(self):
        cleanup, state, prepared = self.prepare()
        old_pending = PendingApiRequest("c-0-f", DEADLINE)
        legacy = cleanup.evolve(
            now=NOW,
            generation=3,
            api_run_accounting=state.dispatch(BLOCK, old_pending).to_document(),
        )
        with self.assertRaisesRegex(ReviewInputError, "opaque request"):
            observe_exact_abort_close(
                TARGET, selected=legacy, row=native_row(render(legacy))
            )
        carried_unknown = state.dispatch(BLOCK, old_pending)
        with self.assertRaisesRegex(ReviewInputError, "another charge"):
            prepare_fixture(cleanup, carried_unknown)
        persisted_state = ApiRunAccounting.from_document(
            prepared.record.api_run_accounting
        )
        additional = prepared.record.evolve(
            now=NOW,
            api_run_accounting=persisted_state.dispatch(
                BLOCK, PendingApiRequest("c-0-11", DEADLINE)
            ).to_document(),
        )
        with self.assertRaisesRegex(ReviewInputError, "unique paid root witness"):
            observe_exact_abort_close(
                TARGET, selected=additional, row=native_row(render(additional))
            )
        reserved = mutate_reserved(
            prepared.record,
            slot="failed-attempt",
            reservation_id="9" * 64,
            expected_generation=3,
            head_sha="c" * 40,
            now=NOW,
        )
        committed = mutate_commit(
            reserved, reservation_id="9" * 64, expected_generation=4, now=NOW
        )
        self.assertEqual(committed.failed_attempts, 1)
        for later in (reserved, committed):
            with (
                self.subTest(generation=later.generation),
                self.assertRaises(ReviewInputError),
            ):
                observe_exact_abort_close(
                    TARGET, selected=later, row=native_row(render(later))
                )
        self.assertEqual(
            ApiRunAccounting.from_document(committed.api_run_accounting).uncertain, 1
        )

    def test_commitment_covers_deadline_owner_history_and_exact_root(self):
        cleanup, state, prepared = self.prepare()
        pending = prepared.pending
        doc = prepared.record.to_dict()
        state_doc = ApiRunAccounting.from_document(doc["api_run_accounting"])
        for changed, alternative in (
            (replace(pending, deadline_unix_ms=DEADLINE - 1), cleanup),
            (pending, cleanup.evolve(now=NOW, operator_paused=True)),
        ):
            alternate = alternative.evolve(
                now=NOW,
                generation=3,
                api_run_accounting=state.dispatch(BLOCK, changed).to_document(),
            )
            self.assertNotEqual(alternate.record_sha256, prepared.record.record_sha256)
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(ReviewInputError, "binding changed"),
            ):
                observe_exact_abort_close(
                    TARGET, selected=alternate, row=native_row(render(alternate))
                )
        self.assertEqual(
            state_doc,
            ApiRunAccounting.from_document(prepared.record.api_run_accounting),
        )

    def test_preparation_needs_the_actual_cleanup_charge_number_and_full_tail(self):
        cleanup, state = precursor(terminal=2)
        with self.assertRaisesRegex(EvidenceBudgetExhausted, "unfunded"):
            prepare_fixture(cleanup, state)
        cleanup, state = precursor()
        prior = PendingApiRequest("c-0-10", DEADLINE)
        observed = state.dispatch(BLOCK, prior).confirm(prior)
        for pending_state, sequence, deadline in (
            (observed, 16, DEADLINE),
            (state, True, DEADLINE),
            (state, 16, 0),
            (replace(state, analysis_abort=None), 16, DEADLINE),
        ):
            with (
                self.subTest(sequence=sequence, deadline=deadline),
                self.assertRaises(ReviewInputError),
            ):
                prepare_fixture(
                    cleanup, pending_state, sequence=sequence, deadline=deadline
                )
        self.assertEqual(self.prepare(terminal=3)[2].pending.deadline_unix_ms, DEADLINE)
        self.assertEqual(state.uncertain, 0)
        self.assertEqual(state.reserved_undispatched, 4)

    def test_capacity_is_independent_of_a_small_canonical_root(self):
        import random
        import string

        cleanup, state = precursor()
        rng = random.Random(19)
        dense = "".join(
            rng.choices(
                string.ascii_letters + string.digits + string.punctuation, k=32700
            )
        )

        # Keep the fixed production ceilings. Force genuinely multi-part
        # archive data independently of the root/renderer's ordinary fit.
        def two_parts(_document, **kwargs):
            return partition_evidence({"archive_bytes": dense}, **kwargs)

        with (
            patch(
                "review_sensei_github.prospective_abort_close.partition_evidence",
                side_effect=two_parts,
            ),
            self.assertRaisesRegex(ReviewInputError, "one-part carrier"),
        ):
            prepare_fixture(cleanup, state)
        self.assertEqual(state.reserved_undispatched, 4)
        self.assertEqual(state.uncertain, 0)

    def test_cannot_refill_or_reassign_the_cleanup_block(self):
        cleanup, state = precursor(terminal=2)
        block = state.blocks[0]
        enlarged = replace(state, blocks=(replace(block, undispatched=3),))
        ApiRunAccounting.from_document(cleanup.api_run_accounting).require_successor(
            enlarged
        )
        for added in (enlarged, state.reserve("unrelated", 1)):
            with (
                self.subTest(added=added),
                self.assertRaisesRegex(
                    ReviewInputError, "changed its paid terminal allowance"
                ),
            ):
                prepare_fixture(cleanup, added)
        # Existing paid confirmations from the same reserved block monotonically
        # move the available capacity; they do not require all values to freeze.
        cleanup, state = precursor(terminal=4)
        prior = PendingApiRequest("c-0-f", DEADLINE)
        paid = state.dispatch(BLOCK, prior).confirm(prior)
        candidate = prepare_fixture(cleanup, paid)
        stored = ApiRunAccounting.from_document(candidate.record.api_run_accounting)
        self.assertEqual(stored.completed, (prior,))
        self.assertEqual(stored.reserved_undispatched, 2)
        self.assertEqual(stored.remaining, state.remaining)

    def test_cleanup_body_with_a_consumed_grant_is_not_plain(self):
        cleanup, state = precursor()
        annotated = render_session_comment(
            repository_id=123,
            pull_request=7,
            record=cleanup,
            consumed_grants=("0" * 64,),
        )
        self.assertEqual(parse_session_comment(annotated, identity=IDENTITY), cleanup)
        with self.assertRaisesRegex(ReviewInputError, "native echo is not exact"):
            prepare_abort_close(
                TARGET,
                cleanup=cleanup,
                cleanup_row=native_row(annotated),
                accounting=state,
                sequence=16,
                deadline=DEADLINE,
                now=NOW,
            )

    def test_echo_cannot_claim_a_close_that_spent_its_confirmation_tail(self):
        _, _, prepared = self.prepare(terminal=3)
        state = ApiRunAccounting.from_document(prepared.record.api_run_accounting)
        next_read = PendingApiRequest("c-0-11", DEADLINE)
        exhausted = state.dispatch(BLOCK, next_read).confirm(next_read)
        root = prepared.record.evolve(
            now=NOW, api_run_accounting=exhausted.to_document()
        )
        with self.assertRaisesRegex(ReviewInputError, "unique paid root witness"):
            observe_exact_abort_close(
                TARGET, selected=root, row=native_row(render(root))
            )
