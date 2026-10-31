"""Prior semantic decisions are review evidence, never successor dispositions."""

import hashlib
import unittest

from review_sensei_github import controlled_restart_review as handoff
from review_sensei_github.publication import IndexedFinding, finding_index_marker
from test_controlled_restart import registration

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


class HandoffTests(unittest.TestCase):
    def test_matching_concern_proposes_current_finding_without_transferring_decision(
        self,
    ):
        row = registration()
        archive = canonical_bytes(
            dict(
                schema_version="controlled-restart-archive-v1",
                status="abandoned",
                historical_cost="unknown",
                historical_effects="unsettled",
                registration=row,
                comments=[],
                runs=[],
            )
        )

        def body(head, instance):
            return finding_index_marker(
                repository_id=row["repository_id"],
                pull_request=row["pull_request"],
                head_sha=head,
                base_sha=row["base_sha"],
                result_digest="f" * 64,
                entries=[IndexedFinding(instance, "c" * 64, True, True, True)],
                complete_inventory=True,
            )

        old = body("e" * 40, "a" * 64)
        current = (
            body(row["head_sha"], "b" * 64)
            + "\n"
            + f"<!-- reviewsensei:controlled-restart:v1 request={row['request_id']} archive={hashlib.sha256(archive).hexdigest()} -->"
        )
        note = dict(
            instance="a" * 64,
            decision="not-applicable",
            reference="https://github.com/owner/repo/pull/7#issuecomment-321",
            rationale="Earlier head already guarded this branch; recheck the actual successor.",
        )
        result = handoff.review_handoff(
            archive=archive,
            prior_body=old,
            prior_head="e" * 40,
            current_body=current,
            notes=[note],
        )
        self.assertFalse(result["approval_allowed"])
        self.assertFalse(result["authenticated_live_inventory"])
        self.assertEqual(
            result["mappings"][0]["relationship"], "same-concern-requires-review"
        )
        self.assertEqual(result["mappings"][0]["current_instance"], "b" * 64)
        self.assertEqual(result["mappings"][0]["prior_decision"], note)
        self.assertEqual(result["mappings"][0]["successor_disposition"], None)
        self.assertEqual(result["next_action"], "qualified-current-head-human-review")
        for changes in (
            {"current_body": current.replace(row["head_sha"], "1" * 40)},
            {"archive": archive + b" "},
            {"notes": [{**note, "instance": "d" * 64}]},
        ):
            with (
                self.subTest(changes=list(changes)),
                self.assertRaises(ReviewInputError),
            ):
                handoff.review_handoff(
                    **dict(
                        archive=archive,
                        prior_body=old,
                        prior_head="e" * 40,
                        current_body=current,
                        notes=[note],
                    )
                    | changes
                )


if __name__ == "__main__":
    unittest.main()
