"""Immutable settlements distinguish conflict from same-action idempotence."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from review_sensei_github.application import _command_bytes, _command_from_bytes
from review_sensei_github.quarantine_action import (
    QuarantineCommand,
    apply_quarantine_target,
)


class QuarantineDecisionConflictTests(unittest.TestCase):
    def test_both_action_orders_and_actors_preserve_state_charges_and_prior_receipt(
        self,
    ):
        for prior, requested in (
            ("dismiss", "accept-risk"),
            ("accept-risk", "dismiss"),
            ("dismiss", "dismiss"),
            ("accept-risk", "accept-risk"),
        ):
            for actor in ("alice", "bob"):
                with self.subTest(prior=prior, requested=requested, actor=actor):
                    decision = {
                        "origin_scope_sha256": "a" * 64,
                        "instance": "b" * 64,
                        "action": prior,
                        "actor": "alice",
                    }
                    state = {"settlements": [decision]}
                    before = copy.deepcopy(state)
                    accounting = SimpleNamespace(
                        configuration=SimpleNamespace(digest="c" * 64),
                        confirmed=21,
                        uncertain=3,
                        remaining=84,
                    )
                    ledger = SimpleNamespace(
                        load=lambda _: SimpleNamespace(
                            record=SimpleNamespace(operator_paused=False)
                        )
                    )
                    run = SimpleNamespace(
                        ledger=ledger,
                        identity=object(),
                        http=object(),
                        head_sha="d" * 40,
                        oidc="synthetic",
                        caller=SimpleNamespace(
                            accounting=accounting, budget_view=object()
                        ),
                    )
                    run.identity = SimpleNamespace(
                        repository="owner/repo", repository_id=123, pull_request=7
                    )
                    target = SimpleNamespace(
                        authenticated=SimpleNamespace(state=state), instance="b" * 64
                    )
                    authority = SimpleNamespace(
                        document={"source": {}, "actor": {"login": actor}}
                    )
                    command = QuarantineCommand(
                        requested,
                        "a" * 64,
                        "RS-" + "B" * 64,
                        "Exact historical decision only.",
                        "quarantine:" + "a" * 64 + " Exact historical decision only.",
                    )
                    app = SimpleNamespace(
                        _refinalize_command=lambda result, **_: result
                    )
                    with (
                        patch(
                            "review_sensei_github.quarantine_action.decision_authority",
                            return_value="maintain",
                        ),
                        patch(
                            "review_sensei_github.quarantine_action._stage_decision_event",
                            new=Mock(
                                side_effect=AssertionError("no replacement settlement")
                            ),
                        ),
                    ):
                        result = apply_quarantine_target(
                            run, authority, command, target, app
                        )
                    same = prior == requested and actor == "alice"
                    self.assertFalse(result.applied)
                    self.assertEqual(
                        result.receipts[0].outcome,
                        "already-recorded" if same else "not-applied",
                    )
                    self.assertIn(
                        f"prior action={prior}; actor=alice", result.receipts[0].detail
                    )
                    self.assertIn(
                        f"immutable prior action: {prior} by alice", result.summary
                    )
                    self.assertEqual(
                        _command_from_bytes(_command_bytes(result)), result
                    )
                    self.assertEqual(state, before)
                    self.assertEqual(
                        (
                            accounting.confirmed,
                            accounting.uncertain,
                            accounting.remaining,
                        ),
                        (21, 3, 84),
                    )
