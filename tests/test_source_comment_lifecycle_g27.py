"""Qualification for five-phase source-comment edit/delete lifecycle classification (G27).

Verifies that:
1. ``observe_live_source_revision`` classifies unchanged, edited, restored-body
   (``edit_restored_body``), and deleted source comments, plus current, duplicate,
   and late/stale webhook deliveries across every lifecycle phase while enforcing
   native-ledger caller ownership.
2. ``classify_source_comment_lifecycle`` deterministically maps every
   ``(phase, mutation_state, delivery_disposition)`` combination across all five
   phases (``before_admission``, ``unstarted_intent``, ``started_work``,
   ``pending_receipt``, ``terminal_completed``) with exact ``g32_eligible``,
   ``replay_permitted``, and ``prior_charges_preserved`` invariants.
3. Editing a comment and restoring its exact original body bytes at a newer
   ``updated_at`` classifies as ``edit_restored_body`` (``mutated_revision``),
   never replays the original revision, and cleanly supersedes unstarted intent
   via G32 before allowing the restored revision as a distinct event.
4. ``execute_source_lifecycle_route`` routes admit/execute/preserve decisions
   without side effects, holds started/pending-receipt work fail-closed, settles
   unstarted mutations via ``UnstartedCancellationWitness`` (both ``same_run``
   and ``cross_run`` under Actions cessation and canonical epoch fencing), and
   enables deterministic end-to-end subsequent command and PR review recovery
   without stranding the pull request or touching ``controlled_restart``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from fake_github_http import json_response
from review_sensei_github import controlled_restart
from review_sensei_github.api_run_history import (
    read_action_result,
    read_previous_run,
)
from review_sensei_github.api_run_owner import ApiRunActionsEvidence
from review_sensei_github.api_run_plan import owner_allocations
from review_sensei_github.api_run_requests import (
    ApiRunRequestTransport,
    ApiRunSessionLedger,
)
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)
from review_sensei_github.source_comment_lifecycle import (
    DELIVERY_DISPOSITIONS,
    LIFECYCLE_PHASES,
    RECOVERY_ROUTES,
    SOURCE_MUTATION_STATES,
    SourceRevisionObservation,
    _validate_source_descriptor,
    classify_source_comment_lifecycle,
    execute_source_lifecycle_route,
    observe_live_source_revision,
)
from test_action_run import CommandEntryFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_unstarted_action_recovery_g32 import (
    G32CommandEntryFixture,
    G32LifecycleFixture,
    _make_canonical_coordinator,
)

from review_sensei.api_work import (
    PARTITIONED_ACTION_ACCOUNTING_VERSION,
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
)
from review_sensei.bounded_evidence import inline_manifest
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.publication_staging import (
    PublicationStaging,
)
from review_sensei.session import SessionIdentity, SessionRecord

SOURCE_ID = 77
SOURCE_STAMP = "2026-10-30T08:00:00Z"
INITIAL_BODY = "@reviewsensei review pause"
BODY_SHA = hashlib.sha256(INITIAL_BODY.encode("utf-8")).hexdigest()
SOURCE = {
    "kind": "issue_comment",
    "id": SOURCE_ID,
    "updated_at": SOURCE_STAMP,
    "body_sha256": BODY_SHA,
}


class G27LifecycleFixture(G32LifecycleFixture):
    """Extends ``G32LifecycleFixture`` with configurable comment ID 77 endpoint."""

    def __init__(self, directory: str, *, limit: int = 1024) -> None:
        Path(directory).mkdir(parents=True, exist_ok=True)
        super().__init__(directory, limit=limit)
        self.source_status = 200
        self.source_payload: object = {
            "id": SOURCE_ID,
            "updated_at": SOURCE_STAMP,
            "body": INITIAL_BODY,
        }

    def _external_open(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if method == "GET" and path in {
            f"/repos/owner/repo/issues/comments/{SOURCE_ID}",
            f"/repos/owner/repo/pulls/comments/{SOURCE_ID}",
        }:
            self.trace.append((self.attempt, method, path))
            return json_response(
                copy.deepcopy(self.source_payload), status=self.source_status
            )
        return super()._external_open(request, timeout)


def _prepare_fixture(
    directory: str,
    *,
    purpose: str = "command",
    kind: str = "issue_comment",
    limit: int = 1024,
):
    """Set up a fixture with an admitted action for lifecycle testing."""
    f = G27LifecycleFixture(directory, limit=limit)
    native, configuration, evidence, owner = f.prelude()
    f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
    ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
    source = {**SOURCE, "kind": kind}
    intent = f.accounted.admit_action(
        ledger,
        IDENTITY,
        purpose=purpose,
        source=source,
        head_sha=HEAD,
        operation_key="d" * 64,
    )
    record = parse_session_comment(f.comment["body"], identity=IDENTITY)
    assert record is not None
    return f, native, ledger, evidence, configuration, intent, record


def _queue_root_document(
    *, item_count: int, with_active_operation: bool = False
) -> dict[str, object]:
    doc = {"pending": item_count}
    active_op = (
        {
            "operation_id": "1" * 64,
            "source_digest": "2" * 64,
            "authority_digest": "3" * 64,
            "execution_identity": "4" * 32,
            "deadline_at": "2026-10-30T08:10:00Z",
            "expires_at": "2026-10-30T08:20:00Z",
            "provider_calls": 1,
            "transport_retries": 0,
            "structural_retries": 0,
            "prompt_bytes": 128,
            "response_bytes": 64,
            "response_bytes_reserved": 128,
            "read_accounting": {"calls": 1, "deadline_at_ms": 1_000_000},
        }
        if with_active_operation
        else None
    )
    binding = {
        "repository": IDENTITY.repository,
        "repository_id": IDENTITY.repository_id,
        "pull_request": IDENTITY.pull_request,
        "base_sha": BASE,
        "head_sha": HEAD,
        "policy_digest": "e" * 64,
        "configuration_digest": "f" * 64,
        "generation": 1,
        "producer": "github-bot:999",
        "purpose": "queue",
        "schema_version": "1.0",
    }
    manifest = inline_manifest(doc, binding=binding, item_count=item_count)
    assert manifest is not None
    return {
        "schema_version": "1.0",
        "inventory_digest": "a" * 64,
        "inventory_generation": 1,
        "state_manifest": manifest,
        "active_operation": active_op,
    }


class LifecycleConstantsTests(unittest.TestCase):
    """Verify public enum frozensets match the G27 specification."""

    def test_lifecycle_phases_are_exhaustive(self) -> None:
        self.assertEqual(
            LIFECYCLE_PHASES,
            {
                "before_admission",
                "unstarted_intent",
                "started_work",
                "pending_receipt",
                "terminal_completed",
            },
        )

    def test_source_mutation_states_are_exhaustive(self) -> None:
        self.assertEqual(
            SOURCE_MUTATION_STATES,
            {
                "unchanged",
                "source_edited",
                "edit_restored_body",
                "source_deleted",
            },
        )

    def test_delivery_dispositions_are_exhaustive(self) -> None:
        self.assertEqual(
            DELIVERY_DISPOSITIONS,
            {
                "current_revision",
                "duplicate_delivery",
                "late_stale_delivery",
                "mutated_revision",
                "deleted_source",
            },
        )

    def test_recovery_routes_are_exhaustive(self) -> None:
        self.assertEqual(
            RECOVERY_ROUTES,
            {
                "admit_current_revision",
                "refuse_before_admission",
                "execute_same_source",
                "refuse_stale_delivery",
                "g32_unstarted_cancellation",
                "hold_started_work",
                "hold_pending_receipt",
                "preserve_terminal_no_replay",
            },
        )


class SourceDescriptorValidationTests(unittest.TestCase):
    """Verify _validate_source_descriptor accepts valid and refuses invalid shapes."""

    def test_valid_issue_and_review_comment_descriptors(self) -> None:
        self.assertEqual(_validate_source_descriptor(SOURCE), SOURCE)
        rev = {**SOURCE, "kind": "review_comment"}
        self.assertEqual(_validate_source_descriptor(rev)["kind"], "review_comment")

    def test_rejects_invalid_shapes_and_fields(self) -> None:
        for bad, pattern in (
            ("not-a-dict", "descriptor shape is invalid"),
            (
                {"kind": "issue_comment", "id": 1, "updated_at": SOURCE_STAMP},
                "descriptor shape is invalid",
            ),
            ({**SOURCE, "extra": True}, "descriptor shape is invalid"),
            ({**SOURCE, "kind": "commit_comment"}, "descriptor fields are invalid"),
            ({**SOURCE, "id": 0}, "descriptor fields are invalid"),
            ({**SOURCE, "id": True}, "descriptor fields are invalid"),
            ({**SOURCE, "id": "77"}, "descriptor fields are invalid"),
            ({**SOURCE, "updated_at": "2026-10-30"}, "descriptor fields are invalid"),
            ({**SOURCE, "body_sha256": "tooshort"}, "descriptor fields are invalid"),
            ({**SOURCE, "body_sha256": "A" * 64}, "descriptor fields are invalid"),
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ReviewInputError, pattern),
            ):
                _validate_source_descriptor(bad)


class ObserveLiveSourceRevisionTests(unittest.TestCase):
    """Test mutation_state and delivery_disposition classification."""

    def _observe(
        self,
        directory: str,
        *,
        source_status: int = 200,
        source_payload: object | None = None,
        phase: str = "unstarted_intent",
        event_delivery: dict[str, object] | None = None,
    ) -> SourceRevisionObservation:
        f = G27LifecycleFixture(directory, limit=1024)
        native, configuration, _evidence, owner = f.prelude()
        f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
        ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
        f.source_status = source_status
        if source_payload is not None:
            f.source_payload = source_payload
        return observe_live_source_revision(
            caller=f.accounted,
            ledger=ledger,
            identity=IDENTITY,
            original_source=SOURCE,
            phase=phase,
            event_delivery=event_delivery,
        )

    def test_unchanged_source_dispositions_across_phases_and_deliveries(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            obs_cur = self._observe(str(Path(d) / "cur"), phase="unstarted_intent")
            self.assertEqual(obs_cur.mutation_state, "unchanged")
            self.assertEqual(obs_cur.delivery_disposition, "current_revision")
            self.assertEqual(obs_cur.status, 200)
            self.assertEqual(obs_cur.observed_updated_at, SOURCE_STAMP)
            self.assertEqual(obs_cur.observed_body_sha256, BODY_SHA)

            obs_term = self._observe(str(Path(d) / "term"), phase="terminal_completed")
            self.assertEqual(obs_term.mutation_state, "unchanged")
            self.assertEqual(obs_term.delivery_disposition, "duplicate_delivery")

            obs_pre_match = self._observe(
                str(Path(d) / "pre_match"),
                phase="before_admission",
                event_delivery={**SOURCE},
            )
            self.assertEqual(obs_pre_match.delivery_disposition, "current_revision")

            obs_post_dup = self._observe(
                str(Path(d) / "post_dup"),
                phase="unstarted_intent",
                event_delivery={**SOURCE},
            )
            self.assertEqual(obs_post_dup.delivery_disposition, "duplicate_delivery")

            stale_delivery = {
                **SOURCE,
                "updated_at": "2026-10-29T12:00:00Z",
                "body_sha256": "e" * 64,
            }
            obs_stale = self._observe(
                str(Path(d) / "stale"),
                phase="unstarted_intent",
                event_delivery=stale_delivery,
            )
            self.assertEqual(obs_stale.mutation_state, "unchanged")
            self.assertEqual(obs_stale.delivery_disposition, "late_stale_delivery")

    def test_edited_restored_and_deleted_observations(self) -> None:
        edited_payload = {
            "id": SOURCE_ID,
            "updated_at": "2026-10-30T09:00:00Z",
            "body": "@reviewsensei review status",
        }
        edited_sha = hashlib.sha256(b"@reviewsensei review status").hexdigest()
        restored_payload = {
            "id": SOURCE_ID,
            "updated_at": "2026-10-30T10:00:00Z",
            "body": INITIAL_BODY,
        }
        with tempfile.TemporaryDirectory() as d:
            obs_ed = self._observe(str(Path(d) / "ed"), source_payload=edited_payload)
            self.assertEqual(obs_ed.mutation_state, "source_edited")
            self.assertEqual(obs_ed.delivery_disposition, "mutated_revision")

            obs_ed_match = self._observe(
                str(Path(d) / "ed_match"),
                source_payload=edited_payload,
                event_delivery={
                    **SOURCE,
                    "updated_at": "2026-10-30T09:00:00Z",
                    "body_sha256": edited_sha,
                },
            )
            self.assertEqual(obs_ed_match.delivery_disposition, "mutated_revision")

            obs_ed_stale = self._observe(
                str(Path(d) / "ed_stale"),
                source_payload=edited_payload,
                event_delivery={**SOURCE},
            )
            self.assertEqual(obs_ed_stale.delivery_disposition, "late_stale_delivery")

            obs_rest = self._observe(
                str(Path(d) / "rest"), source_payload=restored_payload
            )
            self.assertEqual(obs_rest.mutation_state, "edit_restored_body")
            self.assertEqual(obs_rest.delivery_disposition, "mutated_revision")
            self.assertEqual(obs_rest.observed_body_sha256, BODY_SHA)

            obs_rest_match = self._observe(
                str(Path(d) / "rest_match"),
                source_payload=restored_payload,
                event_delivery={
                    **SOURCE,
                    "updated_at": "2026-10-30T10:00:00Z",
                    "body_sha256": BODY_SHA,
                },
            )
            self.assertEqual(obs_rest_match.delivery_disposition, "mutated_revision")

            obs_rest_stale = self._observe(
                str(Path(d) / "rest_stale"),
                source_payload=restored_payload,
                event_delivery={**SOURCE},
            )
            self.assertEqual(obs_rest_stale.delivery_disposition, "late_stale_delivery")

            obs_del = self._observe(str(Path(d) / "del"), source_status=404)
            self.assertEqual(obs_del.mutation_state, "source_deleted")
            self.assertEqual(obs_del.delivery_disposition, "deleted_source")
            self.assertEqual(obs_del.status, 404)
            self.assertIsNone(obs_del.observed_updated_at)
            self.assertIsNone(obs_del.observed_body_sha256)

    def test_observation_refuses_unowned_ledger_and_protocol_faults(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = G27LifecycleFixture(str(Path(d) / "main"), limit=1024)
            native, configuration, _ev, owner = f.prelude()
            f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)

            # Unowned ledger whose HTTP opener is not f.accounted.open is refused.
            rogue_http = GitHubHttp(
                api_url="https://api.github.test", opener=f._external_open
            )
            rogue_ledger = GitHubIssueCommentSessionLedger(
                rogue_http, token="synthetic", app_slug="reviewsensei[bot]"
            )
            with self.assertRaisesRegex(
                ReviewInputError, "not the owning native ledger"
            ):
                observe_live_source_revision(
                    caller=f.accounted,
                    ledger=rogue_ledger,
                    identity=IDENTITY,
                    original_source=SOURCE,
                    phase="unstarted_intent",
                )

            with self.assertRaisesRegex(ReviewInputError, "phase is unsupported"):
                self._observe(str(Path(d) / "bad_phase"), phase="invalid_phase")

            with self.assertRaisesRegex(
                ReviewInputError, "delivery comment identity conflicts"
            ):
                self._observe(
                    str(Path(d) / "bad_id"), event_delivery={**SOURCE, "id": 999}
                )

            with self.assertRaisesRegex(
                ReviewInputError, "delivery comment identity conflicts"
            ):
                self._observe(
                    str(Path(d) / "bad_kind"),
                    event_delivery={**SOURCE, "kind": "review_comment"},
                )

            with self.assertRaisesRegex(
                ReviewInputError, "observation failed with HTTP 500"
            ):
                self._observe(str(Path(d) / "http500"), source_status=500)

            for idx, bad_payload in enumerate(
                (
                    [1, 2, 3],
                    {"id": SOURCE_ID, "updated_at": "bad-stamp", "body": "x"},
                    {"id": SOURCE_ID, "updated_at": SOURCE_STAMP},
                    {"id": 999, "updated_at": SOURCE_STAMP, "body": INITIAL_BODY},
                )
            ):
                with (
                    self.subTest(idx=idx),
                    self.assertRaisesRegex(
                        ReviewInputError, "lifecycle payload is malformed"
                    ),
                ):
                    self._observe(
                        str(Path(d) / f"mal_{idx}"), source_payload=bad_payload
                    )


class ClassifyLifecycleMatrixTests(unittest.TestCase):
    """Exhaustively qualify all five phases and guard conditions."""

    def test_before_admission_with_none_record_and_actionless_record(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = G27LifecycleFixture(d, limit=1024)
            native, configuration, _ev, owner = f.prelude()
            f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
            ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
            actionless_record = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            )
            assert actionless_record is not None

            # Both record=None and record with action=None require candidate_source.
            for rec in (None, actionless_record):
                with self.assertRaisesRegex(
                    ReviewInputError, "requires candidate_source"
                ):
                    classify_source_comment_lifecycle(
                        caller=f.accounted,
                        ledger=ledger,
                        identity=IDENTITY,
                        record=rec,
                        candidate_source=None,
                    )

            # Unchanged current revision admits cleanly on both record=None and action=None.
            for rec in (None, actionless_record):
                cls = classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=rec,
                    candidate_source=SOURCE,
                )
                self.assertEqual(cls.phase, "before_admission")
                self.assertEqual(cls.route, "admit_current_revision")
                self.assertFalse(cls.g32_eligible)
                self.assertFalse(cls.replay_permitted)
                self.assertTrue(cls.prior_charges_preserved)

            # Stale delivery before admission refuses even if live source matches candidate.
            stale = {
                **SOURCE,
                "updated_at": "2026-10-29T00:00:00Z",
                "body_sha256": "1" * 64,
            }
            cls_stale = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=actionless_record,
                candidate_source=SOURCE,
                event_delivery=stale,
            )
            self.assertEqual(cls_stale.route, "refuse_before_admission")
            with self.assertRaisesRegex(ReviewInputError, "pre-admission source check"):
                execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=actionless_record,
                    candidate_source=SOURCE,
                    event_delivery=stale,
                )

            # Edited, restored-body, and deleted all refuse before admission with zero writes.
            patches_before = sum(m == "PATCH" for _, m, _ in f.trace)
            for setup, expected_mut in (
                (
                    {
                        "id": SOURCE_ID,
                        "updated_at": "2026-10-30T09:00:00Z",
                        "body": "@reviewsensei review status",
                    },
                    "source_edited",
                ),
                (
                    {
                        "id": SOURCE_ID,
                        "updated_at": "2026-10-30T10:00:00Z",
                        "body": INITIAL_BODY,
                    },
                    "edit_restored_body",
                ),
                ("delete", "source_deleted"),
            ):
                if setup == "delete":
                    f.source_status = 404
                else:
                    f.source_status = 200
                    f.source_payload = setup
                cls_ref = classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=None,
                    candidate_source=SOURCE,
                )
                self.assertEqual(cls_ref.route, "refuse_before_admission")
                self.assertEqual(cls_ref.observation.mutation_state, expected_mut)
            self.assertEqual(sum(m == "PATCH" for _, m, _ in f.trace), patches_before)

    def test_record_scope_accounting_and_candidate_conflict_guards(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, _, record = _prepare_fixture(d)

            # Scope conflict
            wrong_id = SessionIdentity("other/repo", 7, repository_id=123)
            with self.assertRaisesRegex(ReviewInputError, "record scope conflicts"):
                classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=wrong_id,
                    record=record,
                )

            # Missing api_run_accounting
            no_acc = SessionRecord.create(IDENTITY)
            with self.assertRaisesRegex(ReviewInputError, "lacks caller accounting"):
                classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=no_acc,
                )

            # Partitioned reader-only version (caller-requests-v3) refused
            v3_doc = {
                **dict(record.api_run_accounting),  # type: ignore[arg-type]
                "version": PARTITIONED_ACTION_ACCOUNTING_VERSION,
            }
            v3_rec = SessionRecord.create(IDENTITY).evolve(api_run_accounting=v3_doc)
            with self.assertRaisesRegex(ReviewInputError, "reader-only"):
                classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=v3_rec,
                )

            # Conflicting candidate_source alongside admitted action refused
            with self.assertRaisesRegex(
                ReviewInputError, "candidate source comment identity conflicts"
            ):
                classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=record,
                    candidate_source={**SOURCE, "id": 999},
                )

            # Matching candidate_source comment identity is accepted
            cls_ok = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=record,
                candidate_source={**SOURCE},
            )
            self.assertEqual(cls_ok.route, "execute_same_source")

    def test_unstarted_intent_routes_across_duplicates_stale_and_mutations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, _, record = _prepare_fixture(d)

            # Exact duplicate webhook delivery routes to execute_same_source
            cls_dup = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=record,
                event_delivery={**SOURCE},
            )
            self.assertEqual(cls_dup.phase, "unstarted_intent")
            self.assertEqual(
                cls_dup.observation.delivery_disposition, "duplicate_delivery"
            )
            self.assertEqual(cls_dup.route, "execute_same_source")
            self.assertFalse(cls_dup.g32_eligible)

            # Late/stale delivery refuses without G32 cancellation
            stale = {
                **SOURCE,
                "updated_at": "2026-10-29T12:00:00Z",
                "body_sha256": "e" * 64,
            }
            cls_stale = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=record,
                event_delivery=stale,
            )
            self.assertEqual(cls_stale.route, "refuse_stale_delivery")
            self.assertFalse(cls_stale.g32_eligible)
            with self.assertRaisesRegex(ReviewInputError, "late or forged delivery"):
                execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=record,
                    event_delivery=stale,
                )

            # Edited, restored-body, and deleted route to g32_unstarted_cancellation
            for setup, expected_mut in (
                (
                    {
                        "id": SOURCE_ID,
                        "updated_at": "2026-10-30T09:00:00Z",
                        "body": "@reviewsensei review status",
                    },
                    "source_edited",
                ),
                (
                    {
                        "id": SOURCE_ID,
                        "updated_at": "2026-10-30T10:00:00Z",
                        "body": INITIAL_BODY,
                    },
                    "edit_restored_body",
                ),
                ("delete", "source_deleted"),
            ):
                if setup == "delete":
                    f.source_status = 404
                else:
                    f.source_status = 200
                    f.source_payload = setup
                cls_mut = classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=record,
                )
                self.assertEqual(cls_mut.phase, "unstarted_intent")
                self.assertEqual(cls_mut.route, "g32_unstarted_cancellation")
                self.assertTrue(cls_mut.g32_eligible)
                self.assertEqual(cls_mut.observation.mutation_state, expected_mut)

    def test_started_work_covers_execution_started_uncertain_and_review_conflicts(
        self,
    ) -> None:
        # 1. Durable execution_started=True holds across unchanged, edited, restored, deleted
        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, intent, _ = _prepare_fixture(d)
            f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            started_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert started_rec is not None
            for setup in (
                None,
                {
                    "id": SOURCE_ID,
                    "updated_at": "2026-10-30T09:00:00Z",
                    "body": "edited",
                },
                {
                    "id": SOURCE_ID,
                    "updated_at": "2026-10-30T10:00:00Z",
                    "body": INITIAL_BODY,
                },
                "delete",
            ):
                if setup == "delete":
                    f.source_status = 404
                elif setup is not None:
                    f.source_status = 200
                    f.source_payload = setup
                cls = classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=started_rec,
                )
                self.assertEqual(cls.phase, "started_work")
                self.assertEqual(cls.route, "hold_started_work")
                self.assertFalse(cls.g32_eligible)

        # 2. In-memory unresolved mutation on caller transport routes to started_work
        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, _, record = _prepare_fixture(d)
            f.source_status = 404
            f.fault = "unknown-absent"
            with self.assertRaises(TimeoutError):
                f.accounted.open(
                    Request(
                        "https://api.github.test/repos/owner/repo/issues/comments/1000000",
                        data=json.dumps({"body": f.comment["body"]}).encode(),
                        method="PATCH",
                    ),
                    timeout=5,
                )
            cls_unres = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=record,
            )
            self.assertEqual(cls_unres.phase, "started_work")
            self.assertEqual(cls_unres.route, "hold_started_work")
            self.assertFalse(cls_unres.g32_eligible)

        # 3. Conflicting review reservation on record routes to started_work (never G32)
        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, _, record = _prepare_fixture(d)
            f.source_status = 404
            reserved = record.evolve(reservation_id="b" * 64, reserved_slot="initial")
            cls_res = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=reserved,
            )
            self.assertEqual(cls_res.phase, "started_work")
            self.assertFalse(cls_res.g32_eligible)

    def test_pending_receipt_from_explicit_flag_queue_and_publication_staging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, _, record = _prepare_fixture(d)
            f.source_status = 404

            # Empty queue (item_count=0, active_operation=None) stays unstarted_intent
            empty_q_rec = record.evolve(
                assessment_queue=_queue_root_document(item_count=0)
            )
            cls_empty_q = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=empty_q_rec,
            )
            self.assertEqual(cls_empty_q.phase, "unstarted_intent")

            # Queue with pending item_count > 0 enters pending_receipt
            pending_q_rec = record.evolve(
                assessment_queue=_queue_root_document(item_count=2)
            )
            cls_q = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=pending_q_rec,
            )
            self.assertEqual(cls_q.phase, "pending_receipt")
            self.assertEqual(cls_q.route, "hold_pending_receipt")
            self.assertFalse(cls_q.g32_eligible)

            # Queue with active_operation enters pending_receipt even if item_count == 0
            active_op_rec = record.evolve(
                assessment_queue=_queue_root_document(
                    item_count=0, with_active_operation=True
                )
            )
            cls_op = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=active_op_rec,
            )
            self.assertEqual(cls_op.phase, "pending_receipt")

            # Durable publication_staging on record enters pending_receipt
            tx = replace(
                ReviewTransaction.create(
                    repository=IDENTITY.repository,
                    pull_request=IDENTITY.pull_request,
                    base_sha=BASE,
                    head_sha=HEAD,
                    policy_digest="e" * 64,
                    configuration_digest="f" * 64,
                    evidence_digest="a" * 64,
                    reservation_id="b" * 64,
                    generation=1,
                ),
                phase="publication_pending",
                result_sha256="c" * 64,
            )
            staging = PublicationStaging(
                transaction_id=tx.transaction_id,
                result_sha256="c" * 64,
                plan_sha256="9" * 64,
                part_count=1,
            )
            staged_rec = record.evolve(
                transaction=tx,
                last_committed_reservation_id="b" * 64,
                publication_staging=staging.to_document(),
            )
            cls_staged = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=staged_rec,
            )
            self.assertEqual(cls_staged.phase, "pending_receipt")
            self.assertEqual(cls_staged.route, "hold_pending_receipt")

            # Explicit has_pending_receipt=True also holds fail-closed
            cls_exp = classify_source_comment_lifecycle(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=record,
                has_pending_receipt=True,
            )
            self.assertEqual(cls_exp.phase, "pending_receipt")

    def test_terminal_completed_preserves_immutable_receipt_and_verifies_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, intent, pending_record = _prepare_fixture(d)
            boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            proof = f.accounted.settle_action_effect(
                ledger,
                IDENTITY,
                boundary,
                outcome={"kind": "explicit_noop", "reason": "status"},
                result_document={"action": "status", "applied": False},
            )
            closed = f.accounted.close_action(ledger, IDENTITY, proof)
            original_receipt = read_action_result(ledger, IDENTITY, closed)

            for setup, expected_mut, expected_disp in (
                (None, "unchanged", "duplicate_delivery"),
                (
                    {
                        "id": SOURCE_ID,
                        "updated_at": "2026-10-30T09:00:00Z",
                        "body": "edited after terminal",
                    },
                    "source_edited",
                    "mutated_revision",
                ),
                (
                    {
                        "id": SOURCE_ID,
                        "updated_at": "2026-10-30T10:00:00Z",
                        "body": INITIAL_BODY,
                    },
                    "edit_restored_body",
                    "mutated_revision",
                ),
                ("delete", "source_deleted", "deleted_source"),
            ):
                if setup == "delete":
                    f.source_status = 404
                elif setup is not None:
                    f.source_status = 200
                    f.source_payload = setup
                patches_before = sum(m == "PATCH" for _, m, _ in f.trace)
                cls, preserved = execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=closed,
                )
                self.assertEqual(cls.phase, "terminal_completed")
                self.assertEqual(cls.route, "preserve_terminal_no_replay")
                self.assertEqual(cls.observation.mutation_state, expected_mut)
                self.assertEqual(cls.observation.delivery_disposition, expected_disp)
                self.assertFalse(cls.g32_eligible)
                self.assertFalse(cls.replay_permitted)
                self.assertEqual(preserved, closed)
                self.assertEqual(
                    sum(m == "PATCH" for _, m, _ in f.trace), patches_before
                )
                self.assertEqual(
                    read_action_result(ledger, IDENTITY, closed), original_receipt
                )

            # Corrupted producer binding on terminal result manifest is refused fail-closed.
            tampered_acc = copy.deepcopy(dict(closed.api_run_accounting))  # type: ignore[arg-type]
            tampered_acc["action"]["result"]["binding"]["producer"] = "github-bot:1"
            tampered_closed = pending_record.evolve(api_run_accounting=tampered_acc)
            with self.assertRaises(ReviewInputError):
                classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=tampered_closed,
                )


class ExecuteLifecycleRouteAndReviewRecoveryTests(unittest.TestCase):
    """Test route execution, canonical fencing, review_comment kind, and PR recovery."""

    def test_execute_pass_through_and_hold_routes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = G27LifecycleFixture(str(Path(d) / "pre"), limit=1024)
            native, config, _ev, owner = f.prelude()
            f.accounted.initialize_and_activate(native, IDENTITY, config, owner)
            ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
            cls_adm, res_adm = execute_source_lifecycle_route(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=None,
                candidate_source=SOURCE,
            )
            self.assertEqual(cls_adm.route, "admit_current_revision")
            self.assertIsNone(res_adm)

        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, intent, record = _prepare_fixture(
                d, purpose="reply", kind="review_comment"
            )
            cls_same, res_same = execute_source_lifecycle_route(
                caller=f.accounted,
                ledger=ledger,
                identity=IDENTITY,
                record=record,
            )
            self.assertEqual(cls_same.route, "execute_same_source")
            self.assertEqual(cls_same.observation.kind, "review_comment")
            self.assertEqual(res_same, record)
            self.assertTrue(
                any(m == "GET" and "/pulls/comments/" in p for _, m, p in f.trace)
            )

            # Started and pending_receipt raise bounded guidance on execute
            f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            started = parse_session_comment(f.comment["body"], identity=IDENTITY)
            f.source_status = 404
            with self.assertRaisesRegex(ReviewInputError, "started or uncertain"):
                execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=started,
                )
            with self.assertRaisesRegex(ReviewInputError, "durable pending receipt"):
                execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=started,
                    has_pending_receipt=True,
                )

    def test_execute_g32_routes_in_run_and_cross_run_with_actions_and_canonical_fence(
        self,
    ) -> None:
        # 1. Without witness proof, g32_unstarted_cancellation holds fail-closed
        with tempfile.TemporaryDirectory() as d:
            f, _, ledger, _, _, _, record = _prepare_fixture(d)
            f.source_status = 404
            with self.assertRaisesRegex(
                ReviewInputError, "UnstartedCancellationWitness"
            ):
                execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=record,
                )

        # 2. Same-run edit_restored_body cancellation via Actions cessation
        with tempfile.TemporaryDirectory() as d:
            f, _, _ledger, _, _cfg, _intent, _rec = _prepare_fixture(d)
            f.source_payload = {
                "id": SOURCE_ID,
                "updated_at": "2026-10-30T10:00:00Z",
                "body": INITIAL_BODY,
            }
            native_2 = f.recover_lifecycle()
            successor = ApiRunSessionLedger(native_2, f.accounted, IDENTITY)
            ev_2 = ApiRunActionsEvidence(
                successor.http,
                token="synthetic-actions",
                command_run_id="456",
                job="hosted",
            )
            current = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert current is not None

            with self.assertRaisesRegex(ReviewInputError, "mode is unsupported"):
                execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=successor,
                    identity=IDENTITY,
                    record=current,
                    actions_evidence=ev_2,
                    mode="invalid",
                )
            with self.assertRaisesRegex(
                ReviewInputError, "requires configuration and owner"
            ):
                execute_source_lifecycle_route(
                    caller=f.accounted,
                    ledger=successor,
                    identity=IDENTITY,
                    record=current,
                    actions_evidence=ev_2,
                    mode="cross_run",
                )

            cls, settled = execute_source_lifecycle_route(
                caller=f.accounted,
                ledger=successor,
                identity=IDENTITY,
                record=current,
                actions_evidence=ev_2,
                mode="same_run",
            )
            self.assertEqual(cls.observation.mutation_state, "edit_restored_body")
            self.assertEqual(cls.route, "g32_unstarted_cancellation")
            assert isinstance(settled, type(current))
            settled_state = ApiRunAccounting.from_document(settled.api_run_accounting)
            self.assertEqual(settled_state.action["phase"], "terminal")
            self.assertEqual(
                settled_state.action["outcome"],
                {"kind": "canceled_unstarted", "reason": "source_edited"},
            )

        # 3. Cross-run cancellation via both Actions evidence and canonical epoch fence
        for auth_kind in ("actions", "canonical"):
            with (
                self.subTest(auth_kind=auth_kind),
                tempfile.TemporaryDirectory() as d,
            ):
                f, _, _ledger, _, config, _intent, _ = _prepare_fixture(d)
                f.recover_lifecycle()
                stranded = parse_session_comment(f.comment["body"], identity=IDENTITY)
                assert stranded is not None
                f.source_status = 404
                f.past_runs[456] = (2, "hosted", "completed")
                f.past_runs[457] = (1, "hosted", "in_progress")
                f.run_id = 457
                f.attempt = 1
                f.trace = []

                run457_dir = Path(d) / "run457"
                run457_dir.mkdir()
                caller_457 = ApiRunRequestTransport(
                    directory=run457_dir,
                    run=ApiRunIdentity(123, 457),
                    plan=f.plan,
                    attempt=1,
                    opener=f._external_open,
                )
                f.accounted = caller_457
                http_457 = GitHubHttp(
                    api_url="https://api.github.test", opener=caller_457.open
                )
                native_457 = GitHubIssueCommentSessionLedger(
                    http_457, token="synthetic", app_slug="reviewsensei[bot]"
                )
                native_457.load(IDENTITY)
                coord, tok = (
                    _make_canonical_coordinator(Path(d) / "coord", epoch=2)
                    if auth_kind == "canonical"
                    else (None, None)
                )
                ev_457 = (
                    ApiRunActionsEvidence(
                        http_457,
                        token="synthetic-actions",
                        command_run_id="457",
                        job="hosted",
                    )
                    if auth_kind == "actions"
                    else None
                )
                cls, activated = execute_source_lifecycle_route(
                    caller=caller_457,
                    ledger=native_457,
                    identity=IDENTITY,
                    record=stranded,
                    actions_evidence=ev_457,
                    canonical_coordinator=coord,
                    canonical_token=tok,  # type: ignore[arg-type]
                    mode="cross_run",
                    rollover_configuration=config,
                    rollover_owner=ApiExecutionOwner(1, "hosted", 0),
                )
                self.assertEqual(cls.route, "g32_unstarted_cancellation")
                self.assertIsInstance(activated, ApiRunAccounting)
                ledger_457 = ApiRunSessionLedger(native_457, caller_457, IDENTITY)
                archived = read_previous_run(
                    ledger_457, IDENTITY, caller_457.accounting.previous_record
                )
                archived_state = ApiRunAccounting.from_document(
                    archived.api_run_accounting
                )
                self.assertEqual(
                    archived_state.action["outcome"],
                    {"kind": "canceled_unstarted", "reason": "source_deleted"},
                )

    def test_end_to_end_edit_then_restore_recovers_command_and_pr_review_without_replay(
        self,
    ) -> None:
        """Prove edit->restore never replays T1 and never strands subsequent reviews."""
        with tempfile.TemporaryDirectory() as directory:
            f = G32CommandEntryFixture(directory)
            # 1. Complete an initial PR review to establish durable transaction & history.
            self.assertEqual(f.invoke(), 0, f.error)
            initial_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert initial_record is not None and initial_record.transaction is not None
            initial_reviews = len(f.reviews)

            # 2. Dispatch a command at T1; edit at T2 and restore original body at T3
            #    before the effect-entry source fence reads the comment.
            f.SOURCE_ID = 401
            f.select_command("@reviewsensei review pause", new_run=True)

            def edit_then_restore_before_fence(current: CommandEntryFixture) -> None:
                if current.source_reads == 1:
                    # T2: edited, then T3: exact original body restored at newer updated_at!
                    current.source["body"] = "@reviewsensei review pause"
                    current.source["updated_at"] = "2026-10-30T12:15:00Z"

            f.before_source_read = edit_then_restore_before_fence
            self.assertNotEqual(f.invoke_command(), 0)
            f.before_source_read = None

            stranded = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert stranded is not None
            self.assertEqual(
                stranded.api_run_accounting["action"]["phase"],  # type: ignore[index]
                "pending",
            )
            self.assertFalse(
                stranded.api_run_accounting["action"]["execution_started"]  # type: ignore[index]
            )
            # Pause was NEVER applied — edit_restored_body did NOT replay T1!
            self.assertFalse(stranded.operator_paused)

            # 3. Recover on attempt 2 via execute_source_lifecycle_route -> classifies
            #    edit_restored_body and settles T1 to canceled_unstarted(source_edited).
            stranded_run_id = f.run_id
            restored_source_401 = copy.deepcopy(f.source)
            f.past_jobs.pop(stranded_run_id, None)
            f.run_id = stranded_run_id
            f.attempt = 2
            f.owner_job = "command"
            f.command_active = True
            f.SOURCE_ID = 401
            f.source = restored_source_401
            f.trace.clear()

            rec_dir = Path(directory) / "recover_g27_attempt2"
            rec_dir.mkdir()
            stranded_state = ApiRunAccounting.from_document(stranded.api_run_accounting)
            caller_rec = ApiRunRequestTransport(
                directory=rec_dir,
                run=stranded_state.identity,
                plan=stranded_state.startup,
                job="command",
                attempt=2,
                opener=f.external,
            )
            f.accounted = caller_rec
            f.transports.append(caller_rec)
            http_rec = GitHubHttp(
                api_url="https://api.github.test", opener=caller_rec.open
            )
            native_rec = GitHubIssueCommentSessionLedger(
                http_rec,
                token="synthetic-review_session",
                app_slug="reviewsensei[bot]",
            )
            native_rec.load(IDENTITY)
            ev_rec = ApiRunActionsEvidence(
                http_rec,
                token="synthetic-review_actions",
                command_run_id=str(stranded_run_id),
                job="command",
            )
            alloc = owner_allocations(
                stranded_state.configuration.limit,
                stranded_state.startup.liability,
            )

            def claim_attempt2(rec):
                ev_rec.ceased_owner(IDENTITY, stranded_state.owner)
                prev = ApiRunAccounting.from_document(rec.api_run_accounting)
                claimed = (
                    prev.claim_startup(
                        ApiExecutionOwner(2, "command", prev.owner.generation + 1)
                    )
                    .reserve("command-work-2", alloc.successor_work)
                    .reserve("command-terminal-2", alloc.successor_terminal)
                )
                return rec.evolve(
                    generation=rec.generation + 1,
                    api_run_accounting=claimed.to_document(),
                )

            caller_rec.reserve_and_activate(
                native_rec, IDENTITY, claim_attempt2, block_id="command-work-2"
            )
            caller_rec.bind_terminal_reservation("command-terminal-2")
            ledger_rec = ApiRunSessionLedger(native_rec, caller_rec, IDENTITY)
            active_record = parse_session_comment(f.comment["body"], identity=IDENTITY)
            assert active_record is not None

            cls, settled_root = execute_source_lifecycle_route(
                caller=caller_rec,
                ledger=ledger_rec,
                identity=IDENTITY,
                record=active_record,
                actions_evidence=ev_rec,
                mode="same_run",
            )
            self.assertEqual(cls.phase, "unstarted_intent")
            self.assertEqual(cls.observation.mutation_state, "edit_restored_body")
            self.assertEqual(cls.route, "g32_unstarted_cancellation")
            assert settled_root is not None and not isinstance(
                settled_root, ApiRunAccounting
            )
            self.assertEqual(
                settled_root.transaction.transaction_id,  # type: ignore[union-attr]
                initial_record.transaction.transaction_id,
            )

            # 4. Now the restored T3 revision on comment 401 can be admitted ONLY as a
            #    fresh distinct event on a new run — and succeeds cleanly!
            f.attempt = 1
            f.past_jobs[stranded_run_id] = "command"
            f.past_run_attempts[stranded_run_id] = 2
            f.run_id = stranded_run_id + 1
            f.SOURCE_ID = 401
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error)

            # 5. A subsequent PR review on a new commit SHA completes end-to-end!
            f.command_active = False
            f.past_jobs[f.run_id] = "command"
            f.run_id += 1
            f.owner_job = "hosted"
            f.event_name = "pull_request"
            f.requested_head = f.live_head = "8" * 40
            f.trace.clear()
            f.transports.clear()
            f.worker_observations.clear()
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertGreater(len(f.reviews), initial_reviews)
            self.assertEqual(controlled_restart.registrations(), [])


if __name__ == "__main__":
    unittest.main()
