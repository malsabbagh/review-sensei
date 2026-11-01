"""Proposal-only comment interpretation: no authority or live provider fixtures."""

import json
import unittest
from dataclasses import replace

from review_sensei_github.comment_intent import (
    complete_comment_proposal,
    prepare_comment_proposal,
)

from review_sensei.comment_intent import (
    AnalysisScope,
    CommentObservation,
    CommitReference,
    FindingTarget,
    ResolvedActionFacts,
)
from review_sensei.decision_provider import parse_decision_response
from review_sensei.errors import ReviewInputError

INSTANCE = "abc123" + "1" * 58
OTHER_INSTANCE = "def456" + "4" * 58


def observation(body="@reviewsensei addressed RS-ABC123 in commit abcdef0", **kwargs):
    scope = AnalysisScope(123, 7, "a" * 40, "b" * 40, "c" * 64, "d" * 64, "e" * 64)
    return CommentObservation(
        scope,
        kwargs.pop("surface", "issue_comment"),
        42,
        "2026-11-01T00:00:00Z",
        9,
        "alice",
        "User",
        body,
        **kwargs,
    )


def facts(source, **kwargs):
    values = dict(
        source_fingerprint=source.fingerprint(),
        scope=source.scope,
        actor_permission="maintain",
        extraction_admitted=True,
        invocation_authorized=True,
        pr_open=True,
        inventory_complete=True,
        session_terminal=True,
        provider="fixture",
        model="fixture-choice-v1",
        findings=(
            FindingTarget("RS-ABC123", INSTANCE, "2" * 64, source.scope.head_sha),
        ),
        commits=(CommitReference("abcdef0", "abcdef0" + "3" * 33, True, True),),
    )
    values.update(kwargs)
    return ResolvedActionFacts(**values)


def decision(
    prepared, intent="assess_claim", stance="fixed", polarity="affirmative", **overrides
):
    request = prepared.request
    answers = {q.name: "use" for q in request.questions}
    answers.update(intent=intent, stance=stance, polarity=polarity)
    answers.update(overrides)
    return parse_decision_response(
        json.dumps(
            {
                "schema_version": "1.0",
                "request_sha256": request.digest(),
                "provider": request.provider,
                "model": request.model,
                "completion": "complete",
                "answers": answers,
                "usage": {"input_tokens": 11, "output_tokens": None},
            }
        ),
        request,
    )


class CommentIntentTests(unittest.TestCase):
    def test_display_reference_must_match_actual_instance_prefix(self):
        with self.assertRaises(ReviewInputError):
            FindingTarget("RS-ABC123", "1" * 64, "2" * 64, "b" * 40)

    def test_global_fixed_claim_retains_current_whole_pr_and_never_becomes_override(
        self,
    ):
        source = observation()
        current = facts(source)
        prepared = prepare_comment_proposal(source, current)
        proposal = complete_comment_proposal(source, current, decision(prepared))
        self.assertEqual(proposal.status, "proposed")
        self.assertEqual(proposal.action, "assess_claim")
        self.assertEqual(proposal.scope, source.scope)
        self.assertEqual(proposal.targets, (INSTANCE,))
        self.assertEqual(proposal.commits, ("abcdef0" + "3" * 33,))
        self.assertIn("whole-pr-current-evidence", proposal.requirements)
        self.assertIn("independent-criterion-assessment", proposal.requirements)
        bindings = {(e.kind, e.identity): e.binding_sha256 for e in proposal.evidence}
        self.assertEqual(bindings[("criterion", INSTANCE)], "2" * 64)
        self.assertEqual(
            bindings[("pr-inventory", source.scope.head_sha)],
            source.scope.inventory_sha256,
        )
        self.assertEqual(
            bindings[("pr-diff", source.scope.head_sha)], source.scope.diff_sha256
        )
        self.assertEqual(
            bindings[("context-policy", source.scope.head_sha)],
            source.scope.context_policy_sha256,
        )
        self.assertFalse(proposal.executable)

    def test_inline_negative_bare_commit_keeps_exact_root_and_utf8_offsets(self):
        source = observation(
            "🙂 abcdef0 did not fix it",
            surface="review_comment",
            root_id=30,
            root_instance=INSTANCE,
        )
        current = facts(source)
        prepared = prepare_comment_proposal(source, current)
        candidates = json.loads(prepared.request.text)["references"]
        self.assertEqual(len(candidates), 1)
        kind, literal, start, end = candidates[0]
        self.assertEqual((kind, literal), ("commit", "abcdef0"))
        self.assertEqual(source.body.encode("utf-8")[start:end], b"abcdef0")
        proposal = complete_comment_proposal(
            source, current, decision(prepared, stance="not_fixed")
        )
        self.assertEqual(proposal.targets, (INSTANCE,))
        self.assertEqual(proposal.commits, ("abcdef0" + "3" * 33,))
        self.assertEqual(proposal.action, "assess_claim")
        self.assertEqual(proposal.claim.kind, "not_fixed")
        self.assertEqual(
            proposal.claim.source_span_utf8, (0, len(source.body.encode("utf-8")))
        )
        self.assertFalse(proposal.executable)

    def test_negated_assessment_request_is_evidence_only(self):
        source = observation("Do not assess RS-ABC123")
        current = facts(source)
        prepared = prepare_comment_proposal(source, current)
        result = complete_comment_proposal(
            source, current, decision(prepared, stance="context", polarity="negated")
        )
        self.assertEqual(result.status, "evidence_only")
        self.assertIsNone(result.action)

    def test_explicit_review_of_older_commit_cannot_be_retargeted(self):
        for command in ("rescan", "review remaining"):
            with self.subTest(command=command):
                source = observation("@reviewsensei " + command + " commit " + "a" * 40)
                prepared = prepare_comment_proposal(source, facts(source))
                self.assertIsNone(prepared.request)
                self.assertEqual(prepared.proposal.status, "refused")

    def test_explicit_commands_bypass_extraction_without_minting_authority(self):
        for text, action in (
            ("verify", "verify"),
            ("review continue", "continue"),
            ("review status", "status"),
            ("rescan", "rescan"),
            ("review remaining", "remaining"),
            ("override RS-ABC123 acceptable risk", "accept-risk"),
            ("review reenroll", "reenroll"),
            ("RS-ABC123 is acceptable risk", "accept-risk"),
            ("I reviewed the media files and I approve", "approve-media"),
            (
                "review accept-coverage-uncertainty --reason accepted missing scope",
                "accept-coverage-uncertainty",
            ),
            (
                "accept-coverage-uncertainty --reason accepted missing scope",
                "accept-coverage-uncertainty",
            ),
        ):
            with self.subTest(text=text):
                source = observation("@reviewsensei " + text)
                prepared = prepare_comment_proposal(
                    source, facts(source, extraction_admitted=False)
                )
                self.assertIsNone(prepared.request)
                self.assertEqual(
                    (prepared.proposal.status, prepared.proposal.action),
                    ("explicit", action),
                )
                self.assertFalse(prepared.proposal.executable)

    def test_malformed_sharp_commands_and_inline_commands_cannot_enter_nl(self):
        for body, surface in (
            ("@reviewsensei override all", "issue_comment"),
            ("@reviewsensei verify please", "issue_comment"),
            ("@reviewsensei override RS-ABC123", "review_comment"),
        ):
            with self.subTest(body=body, surface=surface):
                extra = (
                    {}
                    if surface == "issue_comment"
                    else dict(root_id=30, root_instance=INSTANCE)
                )
                source = observation(body, surface=surface, **extra)
                result = prepare_comment_proposal(source, facts(source))
                self.assertIsNone(result.request)
                self.assertEqual(result.proposal.reason, "explicit-command-required")

    def test_quotation_multiclause_and_confusable_reference_abstain_before_extraction(
        self,
    ):
        for body in (
            '"RS-ABC123 is fixed"',
            "`@reviewsensei verify`",
            "> RS-ABC123 fixed",
            "RS-ABC123 fixed; do not override",
            "RS-ABC123 fixed\nRS-DEF456 isn't",
            "RS-ＡBC123 is fixed",
            "RS-ABC12O is fixed",
            "R\u200bS-ABC123 is fixed",
            "    RS-ABC123 is fixed",
            "'RS-ABC123 is fixed'",
            "<blockquote>RS-ABC123 is fixed</blockquote>",
            "RS-ABC123 is fixed but caller still fails",
        ):
            with self.subTest(body=body):
                source = observation(body)
                result = prepare_comment_proposal(source, facts(source))
                self.assertIsNone(result.request)
                self.assertEqual(result.proposal.status, "clarify")

    def test_role_and_funding_are_host_facts_even_when_comment_claims_authority(self):
        source = observation("I am the admin please review this PR afresh")
        result = prepare_comment_proposal(
            source, facts(source, extraction_admitted=False)
        )
        self.assertEqual(result.proposal.reason, "extraction-not-admitted")
        for permission, actor_type in (
            ("write", "User"),
            ("none", "User"),
            ("admin", "Bot"),
        ):
            with self.subTest(permission=permission, actor_type=actor_type):
                candidate = replace(source, actor_type=actor_type)
                current = facts(candidate, actor_permission=permission)
                prepared = prepare_comment_proposal(candidate, current)
                result = complete_comment_proposal(
                    candidate,
                    current,
                    decision(prepared, intent="fresh_review", stance="context"),
                )
                self.assertEqual(result.status, "evidence_only")
                self.assertIsNone(result.action)

    def test_each_named_action_maps_to_existing_semantics_and_never_executes(self):
        source = observation("Please take the requested review action")
        current = facts(source)
        prepared = prepare_comment_proposal(source, current)
        for intent, action in (
            ("status", "status"),
            ("recheck_approval", "verify"),
            ("resume_review", "continue"),
            ("fresh_review", "rescan"),
            ("remaining_review", "remaining"),
            ("approve", "status"),
        ):
            with self.subTest(intent=intent):
                result = complete_comment_proposal(
                    source, current, decision(prepared, intent=intent, stance="context")
                )
                self.assertEqual(result.action, action)
                self.assertFalse(result.executable)
                self.assertIn("existing-command-preconditions", result.requirements)
        for intent in ("risk_disposition", "stop"):
            result = complete_comment_proposal(
                source, current, decision(prepared, intent=intent, stance="context")
            )
            self.assertEqual(result.reason, "explicit-command-required")
            self.assertIsNone(result.action)

    def test_nonterminal_accounting_and_incomplete_inventory_cannot_be_repaired_by_choices(
        self,
    ):
        source = observation("Please review this PR afresh")
        for field in (
            "session_terminal",
            "inventory_complete",
            "invocation_authorized",
        ):
            with self.subTest(field=field):
                current = facts(source, **{field: False})
                prepared = prepare_comment_proposal(source, current)
                result = complete_comment_proposal(
                    source,
                    current,
                    decision(prepared, intent="fresh_review", stance="context"),
                )
                self.assertIn(result.status, {"refused", "evidence_only"})
                self.assertIsNone(result.action)

    def test_inline_identity_cannot_widen_to_other_finding_or_outdated_root(self):
        source = observation(
            "RS-DEF456 is fixed",
            surface="review_comment",
            root_id=30,
            root_instance=INSTANCE,
        )
        current = facts(
            source,
            findings=(
                FindingTarget("RS-ABC123", INSTANCE, "2" * 64, source.scope.head_sha),
                FindingTarget(
                    "RS-DEF456", OTHER_INSTANCE, "5" * 64, source.scope.head_sha
                ),
            ),
        )
        result = complete_comment_proposal(
            source, current, decision(prepare_comment_proposal(source, current))
        )
        self.assertEqual(result.reason, "inline-root-identity-conflicts")
        outdated = replace(source, body="Fixed", root_instance="9" * 64)
        current = facts(outdated)
        result = complete_comment_proposal(
            outdated, current, decision(prepare_comment_proposal(outdated, current))
        )
        self.assertEqual(result.reason, "inline-root-identity-conflicts")

    def test_unknown_ambiguous_or_stale_identity_and_unrelated_commit_refuse(self):
        source = observation()
        original = facts(source)
        alternatives = (
            replace(original, findings=()),
            replace(original, findings=original.findings * 2),
            replace(
                original, findings=(replace(original.findings[0], head_sha="a" * 40),)
            ),
            replace(original, commits=()),
            replace(original, commits=original.commits * 2),
            replace(original, commits=(replace(original.commits[0], in_pr=False),)),
            replace(
                original,
                commits=(replace(original.commits[0], ancestor_of_head=False),),
            ),
        )
        for current in alternatives:
            with self.subTest(current=current):
                result = complete_comment_proposal(
                    source, current, decision(prepare_comment_proposal(source, current))
                )
                self.assertEqual(result.status, "clarify")
                self.assertIsNone(result.action)

    def test_new_head_source_edit_actor_or_state_invalidates_old_response(self):
        source = observation()
        current = facts(source)
        response = decision(prepare_comment_proposal(source, current))
        for changed in (
            replace(source, body=source.body + " now"),
            replace(source, actor_id=10),
            replace(source, revision="new-revision"),
            replace(source, scope=replace(source.scope, head_sha="f" * 40)),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(
                    complete_comment_proposal(changed, current, response).status,
                    "refused",
                )
        closed = replace(current, pr_open=False)
        self.assertEqual(
            complete_comment_proposal(source, closed, response).reason,
            "pull-request-closed",
        )
        for changed in (
            replace(current, actor_permission="write"),
            replace(current, model="other"),
            replace(current, session_terminal=False),
        ):
            with self.subTest(changed=changed), self.assertRaises(ReviewInputError):
                complete_comment_proposal(source, changed, response)

    def test_idempotency_binds_delivery_and_decision_not_just_text(self):
        source = observation()
        current = facts(source)
        response = decision(prepare_comment_proposal(source, current))
        first = complete_comment_proposal(source, current, response)
        self.assertEqual(
            first.digest(),
            complete_comment_proposal(source, current, response).digest(),
        )
        changed = replace(source, source_id=43)
        new_facts = facts(changed)
        second = complete_comment_proposal(
            changed, new_facts, decision(prepare_comment_proposal(changed, new_facts))
        )
        self.assertNotEqual(first.digest(), second.digest())
        self.assertNotEqual(
            first.digest(),
            complete_comment_proposal(
                source,
                current,
                decision(prepare_comment_proposal(source, current), stance="not_fixed"),
            ).digest(),
        )

    def test_response_must_be_complete_closed_and_exactly_bound_with_unknown_usage_retained(
        self,
    ):
        source = observation()
        prepared = prepare_comment_proposal(source, facts(source))
        response = decision(prepared)
        self.assertFalse(response.usage_known)
        good = response.to_dict()
        alternatives = []
        for field, value in (
            ("completion", "partial"),
            ("model", "fallback"),
            ("request_sha256", "0" * 64),
            ("schema_version", "2.0"),
        ):
            alternatives.append({**good, field: value})
        alternatives.extend(
            (
                {**good, "command": "override all"},
                {**good, "answers": {**good["answers"], "intent": "execute"}},
                {**good, "answers": {}},
                {**good, "usage": {"input_tokens": True, "output_tokens": 0}},
                {**good, "usage": {"input_tokens": float("nan"), "output_tokens": 0}},
            )
        )
        for value in alternatives:
            with self.subTest(value=value), self.assertRaises(ReviewInputError):
                parse_decision_response(json.dumps(value), prepared.request)
        with self.assertRaises(ReviewInputError):
            parse_decision_response(
                '{"schema_version":"1.0","schema_version":"1.0"}', prepared.request
            )
        with self.assertRaises(ReviewInputError):
            parse_decision_response("x" * 16385, prepared.request)

    def test_comment_and_reference_bounds_never_truncate(self):
        with self.assertRaises(ReviewInputError):
            source = observation("🙂" * 1025)
            prepare_comment_proposal(source, facts(source))
        source = observation(" ".join("RS-%06X" % i for i in range(11)))
        self.assertEqual(
            prepare_comment_proposal(source, facts(source)).proposal.reason,
            "reference-count-limit",
        )
        with self.assertRaises(ReviewInputError):
            observation("\ud800")

    def test_ignored_reference_uncertain_polarity_and_positive_negative_conflict_never_propose(
        self,
    ):
        source = observation()
        current = facts(source)
        prepared = prepare_comment_proposal(source, current)
        for changes in (
            {"reference_0": "ignore"},
            {"reference_0": "uncertain"},
            {"polarity": "uncertain"},
            {"polarity": "conditional"},
            {"intent": "unknown"},
        ):
            with self.subTest(changes=changes):
                result = complete_comment_proposal(
                    source, current, decision(prepared, **changes)
                )
                self.assertEqual(result.status, "clarify")
                self.assertIsNone(result.action)
        negative = observation("RS-ABC123 is not fixed")
        current = facts(negative)
        result = complete_comment_proposal(
            negative,
            current,
            decision(prepare_comment_proposal(negative, current), stance="fixed"),
        )
        self.assertEqual(result.reason, "claim-polarity-conflicts")


class ProposalRemediationTests(unittest.TestCase):
    def test_claim_stance_cannot_be_replaced_by_another_action(self):
        source = observation("RS-ABC123 is fixed")
        current = facts(source)
        prepared = prepare_comment_proposal(source, current)
        for stance in ("fixed", "not_fixed", "disputed"):
            for intent in (
                "status",
                "recheck_approval",
                "resume_review",
                "fresh_review",
                "remaining_review",
                "approve",
            ):
                with self.subTest(stance=stance, intent=intent):
                    result = complete_comment_proposal(
                        source,
                        current,
                        decision(prepared, intent=intent, stance=stance),
                    )
                    self.assertEqual(result.status, "clarify")
                    self.assertIsNone(result.action)

    def test_incorrect_affirmative_choice_cannot_erase_request_negation_or_condition(
        self,
    ):
        for body, stance in (
            ("Do not assess RS-ABC123", "context"),
            ("Don't assess whether RS-ABC123 is fixed", "fixed"),
            ("If needed assess RS-ABC123", "context"),
            ("Maybe assess RS-ABC123", "context"),
            ("No need to assess RS-ABC123", "context"),
        ):
            with self.subTest(body=body):
                source = observation(body)
                current = facts(source)
                result = complete_comment_proposal(
                    source,
                    current,
                    decision(prepare_comment_proposal(source, current), stance=stance),
                )
                self.assertIsNone(result.action)
                self.assertIn(result.status, {"evidence_only", "clarify"})

    def test_conjunctions_and_mixed_requests_abstain_before_extraction(self):
        for body in (
            "RS-ABC123 is fixed and RS-DEF456 is still broken",
            "RS-ABC123 is fixed and please review this PR afresh",
            "RS-ABC123 is fixed, please recheck approval",
            "RS-ABC123 is fixed yet the caller fails",
            "RS-ABC123 is fixed, RS-DEF456 is still broken",
            "RS-ABC123 is fixed: RS-DEF456 is still broken",
            "RS-ABC123 is fixed — RS-DEF456 is still broken",
            "RS-ABC123 is fixed - RS-DEF456 is still broken",
        ):
            with self.subTest(body=body):
                source = observation(body)
                prepared = prepare_comment_proposal(source, facts(source))
                self.assertIsNone(prepared.request)
                self.assertEqual(prepared.proposal.status, "clarify")

    def test_all_malformed_sharp_command_forms_stay_out_of_nl(self):
        for body in (
            "@reviewsensei review reenroll please",
            "@reviewsensei review accept-coverage-uncertainty --reason",
            "@reviewsensei I reviewed the media files and I approve please",
            "@reviewsensei RS-ABC123 is acceptable risk please",
        ):
            with self.subTest(body=body):
                source = observation(body)
                prepared = prepare_comment_proposal(source, facts(source))
                self.assertIsNone(prepared.request)
                self.assertEqual(prepared.proposal.reason, "explicit-command-required")

    def test_unsupported_shorthand_cannot_disappear_from_targets(self):
        for body in ("RS123 is fixed", "RS-ABC123 RS123 fixed", "RSABC123 is fixed"):
            with self.subTest(body=body):
                source = observation(body)
                prepared = prepare_comment_proposal(source, facts(source))
                self.assertIsNone(prepared.request)
                self.assertEqual(
                    prepared.proposal.reason, "unsupported-finding-reference"
                )


class CommitReferenceRemediationTests(unittest.TestCase):
    def test_explicit_commit_tokens_cannot_disappear_from_claimed_change(self):
        for token in (
            "abcdef",
            "a" * 41,
            "a" * 64,
            "abcdef0Z",
            "Zabcdef0",
            "/abcdef0",
            "",
        ):
            with self.subTest(token=token):
                source = observation("RS-ABC123 fixed in commit " + token)
                prepared = prepare_comment_proposal(source, facts(source))
                self.assertIsNone(prepared.request)
                self.assertEqual(prepared.proposal.status, "clarify")
                self.assertEqual(
                    prepared.proposal.reason, "unsupported-commit-reference"
                )
        source = observation("RS-ABC123 fixed in commit abcdef0 commit abcdef")
        self.assertIsNone(prepare_comment_proposal(source, facts(source)).request)

    def test_supported_commit_tokens_and_sentence_punctuation_keep_ancestry_checks(
        self,
    ):
        for literal in ("abcdef0", "abcdef0" + "3" * 33):
            for punctuation in ("", ".", "!", "?)"):
                with self.subTest(literal=literal, punctuation=punctuation):
                    source = observation(
                        "RS-ABC123 fixed in commit " + literal + punctuation
                    )
                    sha = "abcdef0" + "3" * 33
                    current = facts(
                        source, commits=(CommitReference(literal, sha, True, True),)
                    )
                    prepared = prepare_comment_proposal(source, current)
                    result = complete_comment_proposal(
                        source, current, decision(prepared)
                    )
                    self.assertEqual(result.action, "assess_claim")
                    self.assertEqual(result.commits, (sha,))
                    self.assertTrue(
                        any(e.kind == "claimed-change" for e in result.evidence)
                    )


if __name__ == "__main__":
    unittest.main()
