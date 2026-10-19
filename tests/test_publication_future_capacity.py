"""A published human inventory must leave room for its resolved authority."""

import unittest
from dataclasses import replace

from review_sensei_github.publication import (
    MAX_PUBLISHED_REVIEW_BODY_BYTES,
    approval_eligibility_from_body,
    approval_eligibility_marker,
    override_provenance_marker,
    parse_finding_index,
)

from review_sensei.convergence import BlockerCandidate
from review_sensei.presentation import assign_finding_identifiers
from tests.test_bounded_v1_publication import comments, publish
from tests.test_human_assessment import HEAD, State


class FutureDispositionFrameTests(unittest.TestCase):
    def test_legacy_and_complete_publish_a_frame_that_can_resolve_every_decision(self):
        humans = tuple(
            replace(item, needs_human=True)
            for item in comments(21, body_placed=True, detail=200)
        )
        optional = tuple(
            replace(
                item,
                blocking=False,
                body=f"Optional {i} " + "z" * 11_200,
                symbol=f"optional_{i}",
            )
            for i, item in enumerate(comments(4, body_placed=True))
        )
        findings = humans + optional
        for complete in (False, True):
            with self.subTest(complete_inventory=complete):
                state = State()
                state.reviews = []
                result = publish(
                    state,
                    findings,
                    complete_inventory=complete,
                    blocker_candidates=tuple(
                        BlockerCandidate(
                            needs_human=bool(item.needs_human),
                            path=item.path,
                            line=item.line,
                            side=item.side,
                        )
                        for item in findings
                    ),
                )
                self.assertEqual(result.status, "published")
                body = state.reviews[0]["body"]
                eligibility = approval_eligibility_from_body(body)
                human = eligibility.human_review
                self.assertEqual(len(human.pending), 21)
                future = replace(
                    eligibility,
                    human_review=replace(
                        human,
                        resolved=tuple(item.fingerprint for item in human.findings),
                    ),
                    facts=replace(
                        eligibility.facts,
                        check_published=False,
                        has_human_adjudication_findings=False,
                    ),
                )
                index = parse_finding_index(
                    body, repository_id=1, pull_request=1, head_sha=HEAD
                )
                ids = assign_finding_identifiers(
                    entry.instance for entry in index.entries
                )
                provenance = override_provenance_marker(
                    actor="a" * 39,
                    head_sha=HEAD,
                    finding_ids=tuple(sorted(ids.values()))[:10],
                )
                future_body = (
                    body.replace(
                        approval_eligibility_marker(eligibility),
                        approval_eligibility_marker(future),
                    )
                    + "\n\n"
                    + provenance
                )
                self.assertLessEqual(
                    len(future_body.encode()), MAX_PUBLISHED_REVIEW_BODY_BYTES
                )
                self.assertEqual(len(index.entries), len(findings))
                for item in findings:
                    self.assertIn(item.body, body)
                self.assertNotIn("APPROVED", [r["state"] for r in state.reviews])


if __name__ == "__main__":
    unittest.main()
