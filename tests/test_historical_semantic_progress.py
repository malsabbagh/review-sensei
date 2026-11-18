"""Historical progress must come from complete source-authenticated state."""

import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import test_quarantine_journey as journey
from review_sensei_github import accepted_semantic_history as semantics

from review_sensei.errors import ReviewInputError


class HistoricalSemanticProgressTests(unittest.TestCase):
    def test_collision_noop_then_authentic_disposition_survives_new_run_reload(self):
        observations = []
        receipt_paths = []
        original = semantics.historical_snapshot

        def observe(snapshot, ledger, record):
            caller = getattr(ledger, "caller", None)
            before_calls = len(caller.observations) if caller is not None else None
            result = original(snapshot, ledger, record)
            if caller is not None:
                self.assertEqual(len(caller.observations), before_calls)
            if record.quarantine_lineage is not None:
                authenticated = ledger._quarantine_authenticated
                if authenticated.record_sha256 != record.record_sha256:
                    receipt_paths.append(record.record_sha256)
                    with patch(
                        "review_sensei_github.quarantine_checkpoint.confirmed_checkpoint_capacity",
                        side_effect=ReviewInputError("missing exact receipt"),
                    ):
                        with self.assertRaisesRegex(
                            ReviewInputError, "missing exact receipt"
                        ):
                            original(snapshot, ledger, record)

                # Serializing/copying an otherwise identical assertion does not
                # issue source authentication; a cached different root cannot
                # stand in for the selected complete current state either.
                for forged in (
                    replace(authenticated),
                    replace(authenticated, record_sha256="f" * 64),
                    None,
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "authenticated_historical_inventory_required"
                    ):
                        original(
                            snapshot,
                            SimpleNamespace(_quarantine_authenticated=forged),
                            record,
                        )
                from review_sensei_github.quarantine_origin import _AUTHENTICATED

                stale = replace(authenticated, record_sha256="f" * 64)
                _AUTHENTICATED.add(stale)
                try:
                    with self.assertRaisesRegex(
                        ReviewInputError, "authenticated_historical_inventory_required"
                    ):
                        original(
                            snapshot,
                            SimpleNamespace(_quarantine_authenticated=stale),
                            record,
                        )
                finally:
                    _AUTHENTICATED.discard(stale)
                observations.append((snapshot, result))
            return result

        with patch.object(semantics, "historical_snapshot", side_effect=observe):
            journey.QuarantineConsumerFaultTests(
                "test_fresh_finding_collision_does_not_discharge_the_historical_obligation"
            ).test_fresh_finding_collision_does_not_discharge_the_historical_obligation()
        self.assertTrue(observations)
        self.assertTrue(receipt_paths)
        self.assertTrue(
            all(set(raw) == set(combined) for raw, combined in observations)
        )
        counts = [
            combined["unresolved_count"] - raw["unresolved_count"]
            for raw, combined in observations
        ]
        self.assertEqual(counts[0], 2)
        self.assertEqual(counts[-1], 1)
        pairs = list(zip(observations, observations[1:]))
        self.assertTrue(
            any(
                a[1]["unresolved_count"] == b[1]["unresolved_count"]
                and not semantics.semantic_advancement(a[1], b[1])
                for a, b in pairs
            )
        )
        self.assertTrue(
            any(
                a[1]["inventory_sha256"] == b[1]["inventory_sha256"]
                and b[1]["unresolved_count"] < a[1]["unresolved_count"]
                and semantics.semantic_advancement(a[1], b[1])
                for a, b in pairs
            )
        )
        for raw, combined in observations:
            self.assertNotEqual(raw["inventory_sha256"], combined["inventory_sha256"])
            self.assertFalse(semantics.semantic_advancement(combined, raw))
            self.assertEqual(raw["pending_count"], combined["pending_count"])
            self.assertEqual(
                raw["unknown_trust_effect"], combined["unknown_trust_effect"]
            )

        # Cold codec readers see the unchanged wire format. A legacy current-only
        # observation cannot discharge an aggregate historical obligation.
        raw, combined = observations[-1]
        history = semantics.append_history(
            None,
            SimpleNamespace(repository_id=123, pull_request=7),
            {
                "operation_key": "cold-observation",
                "purpose": "command",
                "source": {
                    "kind": "issue_comment",
                    "id": 1,
                    "updated_at": "2026-11-13T00:00:00Z",
                    "body_sha256": "a" * 64,
                },
            },
            combined,
            combined,
        )
        cold = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import json, sys
from types import SimpleNamespace
from review_sensei_github.accepted_semantic_history import validate_history, semantic_advancement
from review_sensei.finding_lifecycle_store import _validate_accepted_history_metadata
history, raw = json.load(sys.stdin)
binding = {"repository_id": 123, "pull_request": 7}
validate_history(history, SimpleNamespace(**binding))
_validate_accepted_history_metadata(history, binding)
assert not semantic_advancement(history["records"][-1]["snapshot"], raw)
assert history["streak"] == 1
""",
            ],
            input=json.dumps([history, raw]),
            text=True,
            capture_output=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            timeout=30,
        )
        self.assertEqual(cold.returncode, 0, cold.stderr)
