"""The inactive proposal API and schema are available from installed wheels."""

import json
import sys
import unittest
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei_github.comment_intent import (  # noqa: E402
    complete_comment_batch,
    complete_comment_proposal,
    prepare_comment_batch,
    prepare_comment_proposal,
)

from review_sensei.comment_intent import (  # noqa: E402
    AnalysisScope,
    CommentObservation,
    FindingTarget,
    ResolvedActionFacts,
)
from review_sensei.comment_intent_batch import normalize_decision_groups  # noqa: E402
from review_sensei.decision_provider import (  # noqa: E402
    DecisionQuestion,
    DecisionRequest,
    parse_decision_response,
)
from review_sensei.schemas import validate_public_document  # noqa: E402


class InstalledCommentIntentTests(unittest.TestCase):
    def test_installed_completion_cannot_retarget_old_response_into_explicit_source(
        self,
    ):
        scope = AnalysisScope(123, 7, "a" * 40, "b" * 40, "c" * 64, "d" * 64, "e" * 64)
        original = CommentObservation(
            scope,
            "issue_comment",
            42,
            "revision1",
            9,
            "alice",
            "User",
            "Please check status",
        )
        facts = ResolvedActionFacts(
            original.fingerprint(),
            scope,
            "maintain",
            True,
            True,
            True,
            True,
            True,
            "fixture",
            "choice-fixture-v1",
        )
        prepared = prepare_comment_proposal(original, facts)
        request = prepared.request
        response = parse_decision_response(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "request_sha256": request.digest(),
                    "provider": request.provider,
                    "model": request.model,
                    "completion": "complete",
                    "answers": {
                        "intent": "status",
                        "stance": "context",
                        "polarity": "affirmative",
                    },
                    "usage": {"input_tokens": None, "output_tokens": None},
                }
            ),
            request,
        )
        edited = replace(original, revision="revision2", body="@reviewsensei verify")
        result = complete_comment_proposal(
            edited, replace(facts, source_fingerprint=edited.fingerprint()), response
        )
        self.assertEqual(result.status, "refused")
        self.assertIsNone(result.action)
        self.assertFalse(result.executable)

    def test_closed_choice_schema_and_unknown_usage_from_installed_core(self):
        request = DecisionRequest(
            "a" * 64,
            "b" * 64,
            "fixture",
            "choice-fixture-v1",
            "bounded comment",
            (DecisionQuestion("intent", ("status", "unknown")),),
        )
        value = {
            "schema_version": "1.0",
            "request_sha256": request.digest(),
            "provider": "fixture",
            "model": "choice-fixture-v1",
            "completion": "complete",
            "answers": {"intent": "status"},
            "usage": {"input_tokens": 1, "output_tokens": None},
        }
        validate_public_document(value, "decision-response")
        response = parse_decision_response(json.dumps(value), request)
        self.assertFalse(response.usage_known)
        self.assertTrue(
            files("review_sensei")
            .joinpath("schemas/decision-response.schema.json")
            .is_file()
        )

    def test_installed_explicit_route_never_needs_or_grants_extraction(self):
        scope = AnalysisScope(123, 7, "a" * 40, "b" * 40, "c" * 64, "d" * 64, "e" * 64)
        source = CommentObservation(
            scope,
            "issue_comment",
            42,
            "revision1",
            9,
            "alice",
            "User",
            "@reviewsensei verify",
        )
        facts = ResolvedActionFacts(
            source.fingerprint(),
            scope,
            "none",
            False,
            False,
            True,
            False,
            False,
            "fixture",
            "choice-fixture-v1",
        )
        prepared = prepare_comment_proposal(source, facts)
        self.assertIsNone(prepared.request)
        self.assertEqual(prepared.proposal.action, "verify")
        self.assertFalse(prepared.proposal.executable)

    def _batch_inputs(self, body):
        scope = AnalysisScope(123, 7, "a" * 40, "b" * 40, "c" * 64, "d" * 64, "e" * 64)
        source = CommentObservation(
            scope, "issue_comment", 42, "v1", 9, "alice", "User", body
        )
        facts = ResolvedActionFacts(
            source.fingerprint(),
            scope,
            "maintain",
            True,
            True,
            True,
            True,
            True,
            "fixture",
            "choice-fixture-v1",
            findings=(
                FindingTarget(
                    "RS-ABC123", "abc123" + "1" * 58, "2" * 64, scope.head_sha
                ),
                FindingTarget(
                    "RS-DEF456", "def456" + "4" * 58, "5" * 64, scope.head_sha
                ),
            ),
        )
        return source, facts

    def test_installed_batch_keeps_original_provenance_and_item_ambiguity_separate(
        self,
    ):
        source, facts = self._batch_inputs("RS-ABC123 fixed\nRS-DEF456 unclear")
        prepared = prepare_comment_batch(source, facts)
        responses = {}
        for request in prepared.requests:
            answers = {}
            for q in request.questions:
                item, field = q.name.split("_", 1)
                answers[q.name] = (
                    {
                        "intent": "assess_claim",
                        "stance": "fixed",
                        "polarity": "affirmative",
                    }.get(field, "use")
                    if item == "i0"
                    else {
                        "intent": "unknown",
                        "stance": "unknown",
                        "polarity": "uncertain",
                    }.get(field, "use")
                )
            responses[request.digest()] = json.dumps(
                {
                    "schema_version": "1.0",
                    "request_sha256": request.digest(),
                    "provider": request.provider,
                    "model": request.model,
                    "completion": "complete",
                    "answers": answers,
                    "usage": {"input_tokens": None, "output_tokens": None},
                }
            )
        extraction = normalize_decision_groups(prepared, responses)
        result = complete_comment_batch(source, facts, prepared, extraction)
        self.assertEqual(
            [i.proposal.action for i in result.items], ["assess_claim", None]
        )
        self.assertEqual(result.items[1].proposal.status, "clarify")
        self.assertTrue(
            all(i.proposal.source_sha256 == source.fingerprint() for i in result.items)
        )
        self.assertTrue(all(not i.executable for i in result.items))
        self.assertFalse(result.executable)

    def test_installed_missing_batch_groups_are_pending_not_noaction(self):
        source, facts = self._batch_inputs(
            "\n".join("Please check status" for _ in range(10))
        )
        prepared = prepare_comment_batch(source, facts)
        extraction = normalize_decision_groups(prepared, {})
        result = complete_comment_batch(source, facts, prepared, extraction)
        self.assertFalse(result.extraction_complete)
        self.assertTrue(all(i.extraction_status == "pending" for i in result.items))
        self.assertTrue(all(i.proposal.action is None for i in result.items))


if __name__ == "__main__":
    unittest.main()
