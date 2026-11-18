"""Selected-root custody survives preparation and journal checkpoints."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import ReviewPublisher
from review_sensei_github.finding_lifecycle import _pack_journal, _unpack_journal

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import (
    decode_lifecycle_state,
    encode_lifecycle_state,
)
from review_sensei.finding_lifecycle_store import (
    read_finding_lifecycle,
    stage_finding_lifecycle,
)
from review_sensei.session import LocalSessionLedger, PreparedSessionRound
from tests import test_durable_finding_lifecycle_restart as restart
from tests.test_finding_lifecycle_journal import HEAD, IDENTITY, journal


class FindingLifecycleCustodyTests(unittest.TestCase):
    def test_prepared_completion_requires_its_original_root_and_no_foreign_changes(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            ledger = LocalSessionLedger(Path(directory))
            initial = ledger.initialize(IDENTITY)
            before = ledger.reserve(
                IDENTITY,
                slot="initial",
                reservation_id="a" * 64,
                expected_generation=initial.generation,
                head_sha=HEAD,
            )
            prepared = PreparedSessionRound(before, None, "a" * 64)
            value = journal()
            value.operation_id = prepared.reservation_id
            value._initial_record = before
            value._record = before.evolve(generation=before.generation + 1)
            self.assertEqual(value.prepared_completion(prepared).record, value._record)
            for changed in (
                before.evolve(generation=before.generation + 1),
                None,
            ):
                value._initial_record = changed
                with self.assertRaisesRegex(ReviewInputError, "authority conflicts"):
                    value.prepared_completion(prepared)
            value._initial_record = before
            value._record = value._record.evolve(
                failed_attempts=before.failed_attempts + 1
            )
            with self.assertRaisesRegex(ReviewInputError, "authority conflicts"):
                value.prepared_completion(prepared)

    def test_unbound_writer_missing_configuration_refuses_before_reservation(self):
        from review_sensei_github.errors import GitHubPublicationError
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.session import SessionIdentity
        from tests.test_artifact_mutation_resilience_g14 import (
            BASE_SHA,
            HEAD_SHA,
            OrdinaryPublisherArtifactResilienceTests,
            _ArtifactGitHubHost,
        )

        fixture = OrdinaryPublisherArtifactResilienceTests()
        host = _ArtifactGitHubHost(head_sha=HEAD_SHA)
        app, _, options = fixture._make_app(host, github_session_ledger=True)
        ledger = GitHubIssueCommentSessionLedger(
            host.http, token="fixture", app_slug="reviewsensei[bot]"
        )
        ledger.initialize(SessionIdentity("owner/repo", 1, 1))
        before = copy.deepcopy(host.issue_comments)
        result, blockers = fixture._blocking_result()
        with self.assertRaisesRegex(
            GitHubPublicationError, "requires trusted configuration"
        ):
            app.publish_review(
                options=options,
                oidc_token="fixture",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=HEAD_SHA,
                base_branch="main",
                base_sha=BASE_SHA,
                result=result,
                diff=fixture.DIFF_1,
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(mode="strict"),
                blocker_candidates=blockers,
                current_key=fixture._cache_key(HEAD_SHA),
            )
        self.assertEqual(host.issue_comments, before)
        self.assertEqual(host.reviews, [])
        self.assertEqual(host.check_runs, [])

    def test_newer_lifecycle_between_selection_and_journal_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = restart.DurableFindingRestartTests.run_phase(
                self, root, "window", "initial"
            )
            self.assertIsNone(first["error"])
            service_before = json.loads((root / "service.json").read_text())
            injected = []

            def concurrent_copy(owner):
                if isinstance(owner, ReviewPublisher) and not injected:
                    # A second writer advances only lifecycle metadata after the
                    # application's reader selected its prior full inventory.
                    ledger = LocalSessionLedger(
                        root / "ledger", enable_finding_lifecycle_writes=True
                    )
                    current = ledger.load(restart.IDENTITY).record
                    document = _unpack_journal(read_finding_lifecycle(ledger, current))
                    restored = decode_lifecycle_state(document["lifecycle"])
                    transaction = current.transaction
                    state = replace(restored.state, head_sha=transaction.head_sha)
                    document["lifecycle"] = encode_lifecycle_state(
                        state,
                        input_identity=(
                            transaction.head_sha,
                            transaction.base_sha,
                            "b" * 64,
                        ),
                    )
                    document["effects"] = []
                    document["unknown_trust_effect"] = True
                    changed = stage_finding_lifecycle(
                        ledger,
                        current,
                        document=_pack_journal(document),
                        operation_id=transaction.transaction_id,
                        base_sha=transaction.base_sha,
                        head_sha=transaction.head_sha,
                        policy_digest=transaction.policy_digest,
                        configuration_digest=transaction.configuration_digest,
                        item_count=1,
                    )
                    injected.append(changed.finding_lifecycle)
                return copy.copy(owner)

            output = io.StringIO()
            with (
                patch("review_sensei_github.application.copy", concurrent_copy),
                contextlib.redirect_stdout(output),
            ):
                restart._worker(root, "window", "omitted", {})
            outcome = json.loads(output.getvalue())
            self.assertEqual(len(injected), 1, outcome)
            self.assertIn("selected state changed before publication", outcome["error"])
            after = LocalSessionLedger(root / "ledger").load(restart.IDENTITY).record
            self.assertEqual(after.finding_lifecycle, injected[0])
            service_after = json.loads((root / "service.json").read_text())
            self.assertEqual(service_after["reviews"], service_before["reviews"])
            self.assertEqual(service_after["check_runs"], service_before["check_runs"])

    def test_new_transaction_with_same_lifecycle_cannot_adopt_old_result(self):
        from review_sensei.models import ReviewTransaction

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = restart.DurableFindingRestartTests.run_phase(
                self, root, "window", "initial"
            )
            self.assertIsNone(first["error"])
            service_before = json.loads((root / "service.json").read_text())
            injected = []

            def concurrent_copy(owner):
                if isinstance(owner, ReviewPublisher) and not injected:
                    ledger = LocalSessionLedger(
                        root / "ledger", enable_finding_lifecycle_writes=True
                    )

                    def replace_owner(current):
                        transaction = current.transaction
                        reservation = "9" * 64
                        changed = replace(
                            transaction,
                            reservation_id=reservation,
                            transaction_id=ReviewTransaction.derive_id(
                                repository=transaction.repository,
                                pull_request=transaction.pull_request,
                                base_sha=transaction.base_sha,
                                head_sha=transaction.head_sha,
                                policy_digest=transaction.policy_digest,
                                configuration_digest=transaction.configuration_digest,
                                evidence_digest=transaction.evidence_digest,
                                reservation_id=reservation,
                            ),
                        )
                        return current.evolve(
                            generation=current.generation + 1,
                            transaction=changed,
                            reservation_id=(
                                reservation
                                if current.reservation_id is not None
                                else None
                            ),
                            last_committed_reservation_id=(
                                reservation
                                if current.last_committed_reservation_id
                                == transaction.reservation_id
                                else current.last_committed_reservation_id
                            ),
                        )

                    changed = ledger.replace(restart.IDENTITY, replace_owner)
                    injected.append(changed)
                return copy.copy(owner)

            output = io.StringIO()
            with (
                patch("review_sensei_github.application.copy", concurrent_copy),
                contextlib.redirect_stdout(output),
            ):
                restart._worker(root, "window", "omitted", {})
            outcome = json.loads(output.getvalue())
            self.assertEqual(len(injected), 1, outcome)
            self.assertIn(
                "transaction owner changed before publication", outcome["error"]
            )
            after = LocalSessionLedger(root / "ledger").load(restart.IDENTITY).record
            self.assertEqual(after, injected[0])
            service_after = json.loads((root / "service.json").read_text())
            self.assertEqual(service_after["reviews"], service_before["reviews"])
            self.assertEqual(service_after["check_runs"], service_before["check_runs"])
