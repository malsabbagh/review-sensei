"""Original-source multi-item proposals; no provider or broker execution."""

import json
import unittest
from dataclasses import replace

from review_sensei_github.comment_intent import (
    complete_comment_batch,
    prepare_comment_batch,
)
from test_comment_intent import INSTANCE, OTHER_INSTANCE, facts, observation

from review_sensei.comment_intent import FindingTarget
from review_sensei.comment_intent_batch import normalize_decision_groups
from review_sensei.errors import ReviewInputError


def current(source, **kwargs):
    return facts(
        source,
        findings=(
            FindingTarget("RS-ABC123", INSTANCE, "2" * 64, source.scope.head_sha),
            FindingTarget("RS-DEF456", OTHER_INSTANCE, "5" * 64, source.scope.head_sha),
        ),
        **kwargs,
    )


def responses(prepared, choices):
    result = {}
    for request in prepared.requests:
        answers = {}
        for q in request.questions:
            index, field = q.name.split("_", 1)
            index = int(index[1:])
            values = choices.get(index, ("assess_claim", "fixed", "affirmative"))
            answers[q.name] = dict(zip(("intent", "stance", "polarity"), values)).get(
                field, "use"
            )
        result[request.digest()] = json.dumps(
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
    return result


def run_batch(source, choices, **kwargs):
    live = current(source, **kwargs)
    prepared = prepare_comment_batch(source, live)
    extraction = normalize_decision_groups(prepared, responses(prepared, choices))
    return (
        prepared,
        extraction,
        complete_comment_batch(source, live, prepared, extraction),
    )


class CommentIntentBatchTests(unittest.TestCase):
    def test_distinct_assess_risk_noaction_and_negation_have_independent_results(self):
        source = observation(
            "RS-ABC123: fixed in commit abcdef0\nRS-DEF456: acceptable risk for this interface\nThanks\nDo not verify"
        )
        prepared, extracted, batch = run_batch(
            source,
            {
                0: ("assess_claim", "fixed", "affirmative"),
                1: ("risk_disposition", "risk_accepted", "affirmative"),
                2: ("none", "context", "affirmative"),
                3: ("recheck_approval", "context", "affirmative"),
            },
        )
        self.assertEqual(
            [i.proposal.action for i in batch.items], ["assess_claim", None, None, None]
        )
        self.assertEqual(
            [i.proposal.status for i in batch.items],
            ["proposed", "clarify", "no_action", "evidence_only"],
        )
        self.assertEqual(batch.items[0].proposal.targets, (INSTANCE,))
        self.assertEqual(batch.items[1].proposal.targets, (OTHER_INSTANCE,))
        self.assertTrue(batch.extraction_complete)
        for item, record in zip(batch.items, prepared.manifest.items):
            self.assertEqual(item.item_id, record.item_id)
            self.assertEqual(item.proposal.source_sha256, source.fingerprint())
            self.assertEqual(item.proposal.scope, source.scope)
            self.assertFalse(item.executable)
        self.assertFalse(batch.executable)
        self.assertFalse(extracted.usage_known)

    def test_ambiguous_or_bad_commit_item_does_not_reroute_a_valid_sibling(self):
        for first in ("RS-123456: fixed", "RS-ABC123 fixed in commit abcdef"):
            with self.subTest(first=first):
                source = observation(first + "\nRS-DEF456: fixed")
                _, _, batch = run_batch(source, {})
                self.assertEqual(batch.items[0].proposal.status, "clarify")
                self.assertEqual(batch.items[1].proposal.action, "assess_claim")
                self.assertEqual(batch.items[1].proposal.targets, (OTHER_INSTANCE,))

    def test_inline_root_conflict_is_per_item(self):
        source = observation(
            "RS-DEF456: fixed\nRS-ABC123: fixed",
            surface="review_comment",
            root_id=30,
            root_instance=INSTANCE,
        )
        _, _, batch = run_batch(source, {})
        self.assertEqual(
            batch.items[0].proposal.reason, "inline-root-identity-conflicts"
        )
        self.assertEqual(batch.items[1].proposal.targets, (INSTANCE,))
        self.assertEqual(batch.items[1].proposal.action, "assess_claim")

    def test_quotes_fences_and_unsupported_units_remain_visible(self):
        source = observation("```\nRS-ABC123 fixed\n```\nRS-DEF456: fixed")
        prepared, _, batch = run_batch(source, {})
        self.assertEqual(len(prepared.manifest.items), 2)
        self.assertEqual(batch.items[0].proposal.status, "evidence_only")
        self.assertEqual(batch.items[1].proposal.action, "assess_claim")
        self.assertEqual(batch.items[1].proposal.targets, (OTHER_INSTANCE,))

    def test_item_keys_survive_reordering_and_sibling_interpretation_changes(self):
        source = observation("RS-ABC123: fixed\nRS-DEF456: fixed")
        prepared, extracted, first = run_batch(source, {})
        reordered = replace(extracted, items=tuple(reversed(extracted.items)))
        again = complete_comment_batch(source, current(source), prepared, reordered)
        self.assertEqual(
            [x.idempotency_key for x in first.items],
            [x.idempotency_key for x in again.items],
        )
        _, _, changed = run_batch(source, {1: ("unknown", "unknown", "uncertain")})
        self.assertEqual(
            first.items[0].proposal.digest(), changed.items[0].proposal.digest()
        )
        self.assertEqual(first.items[1].item_id, changed.items[1].item_id)

    def test_opposite_and_repeated_claims_are_not_group_clearance(self):
        source = observation("RS-ABC123: fixed\nRS-ABC123: not fixed\nRS-ABC123: fixed")
        _, _, batch = run_batch(
            source, {1: ("assess_claim", "not_fixed", "affirmative")}
        )
        self.assertEqual(
            [x.proposal.claim.kind for x in batch.items],
            ["fixed", "not_fixed", "fixed"],
        )
        self.assertEqual(len({x.item_id for x in batch.items}), 3)
        self.assertEqual(len({x.operation_key for x in batch.items}), 1)
        self.assertTrue(
            all(
                "all-related-comment-items" in x.proposal.requirements
                for x in batch.items
            )
        )

    def test_native_group_integrity_failure_stays_pending_without_salvage(self):
        source = observation("\n".join("Please check status" for _ in range(10)))
        live = current(source)
        prepared = prepare_comment_batch(source, live)
        self.assertGreater(len(prepared.requests), 1)
        self.assertTrue(all(len(r.questions) <= 16 for r in prepared.requests))
        replies = responses(
            prepared, {i: ("status", "context", "affirmative") for i in range(10)}
        )
        replies[prepared.requests[0].digest()] = "{}"
        extracted = normalize_decision_groups(prepared, replies)
        batch = complete_comment_batch(source, live, prepared, extracted)
        self.assertFalse(batch.extraction_complete)
        self.assertTrue(any(x.extraction_status == "invalid" for x in batch.items))
        self.assertTrue(any(x.proposal.action == "status" for x in batch.items))
        self.assertTrue(
            all(
                x.proposal.action is None
                for x in batch.items
                if x.extraction_status == "invalid"
            )
        )

    def test_complete_original_command_and_embedded_command_are_distinct(self):
        source = observation("@reviewsensei verify")
        live = current(source, extraction_admitted=False)
        prepared = prepare_comment_batch(source, live)
        self.assertEqual(prepared.requests, ())
        batch = complete_comment_batch(source, live, prepared)
        self.assertEqual(batch.items[0].proposal.status, "explicit")
        source = observation(
            "RS-ABC123: fixed\n@reviewsensei override RS-DEF456 acceptable risk"
        )
        _, _, batch = run_batch(source, {})
        self.assertEqual(batch.items[0].proposal.action, "assess_claim")
        self.assertEqual(batch.items[1].proposal.reason, "explicit-command-required")
        self.assertTrue(all(x.proposal.status != "explicit" for x in batch.items))

    def test_cross_item_qualification_cannot_be_omitted(self):
        source = observation("RS-ABC123: fixed\nRS-DEF456: fixed\nDo not assess either")
        _, _, batch = run_batch(source, {})
        self.assertTrue(all(x.proposal.action is None for x in batch.items))
        self.assertEqual(batch.items[0].proposal.reason, "cross-item-qualification")

    def test_stale_shared_source_and_forged_manifest_or_item_ids_refuse(self):
        source = observation("RS-ABC123: fixed\nRS-DEF456: fixed")
        prepared, extracted, _ = run_batch(source, {})
        edited = replace(source, body=source.body + " now")
        self.assertEqual(
            complete_comment_batch(edited, current(source), prepared, extracted).status,
            "refused",
        )
        with self.assertRaises(ReviewInputError):
            complete_comment_batch(
                source,
                current(source),
                prepared,
                replace(extracted, items=extracted.items * 2),
            )
        with self.assertRaises(ReviewInputError):
            normalize_decision_groups(prepared, {"0" * 64: None})

    def test_manifest_utf8_inventory_and_bounds_never_truncate(self):
        source = observation("🙂 RS-ABC123 fixed; RS-DEF456 fixed")
        prepared, _, batch = run_batch(source, {})
        for item in prepared.manifest.items:
            literal = source.body.encode()[item.span[0] : item.span[1]].decode()
            self.assertTrue(literal.strip())
            for kind, token, start, end in item.references:
                self.assertEqual(source.body.encode()[start:end].decode(), token)
        self.assertEqual(len(batch.items), 2)
        source = observation("\n".join("status" for _ in range(11)))
        prepared = prepare_comment_batch(source, current(source))
        self.assertEqual(prepared.terminal.reason, "item-count-limit")
        self.assertEqual(prepared.requests, ())


class BatchConservationTests(unittest.TestCase):
    def test_exact_question_inventory_grouping_and_unknown_usage(self):
        source = observation("\n".join("RS-ABC123: fixed" for _ in range(10)))
        prepared, extracted, batch = run_batch(source, {})
        self.assertEqual([len(r.questions) for r in prepared.requests], [16, 16, 8])
        names = [q.name for r in prepared.requests for q in r.questions]
        self.assertEqual(len(names), 40)
        self.assertEqual(len(set(names)), 40)
        self.assertFalse(extracted.usage_known)
        self.assertEqual(len(batch.items), 10)
        self.assertEqual(len({i.idempotency_key for i in batch.items}), 10)
        self.assertEqual(len({i.operation_key for i in batch.items}), 1)
        for index in range(10):
            owners = [
                r.digest()
                for r in prepared.requests
                if f"i{index}_intent" in {q.name for q in r.questions}
            ]
            self.assertEqual(len(owners), 1)
            request = next(r for r in prepared.requests if r.digest() == owners[0])
            self.assertTrue(
                all(
                    f"i{index}_{name}" in {q.name for q in request.questions}
                    for name in ("intent", "stance", "polarity", "reference_0")
                )
            )
            self.assertEqual(json.loads(request.text)["comment"], source.body)
            self.assertEqual(
                json.loads(request.text)["manifest_sha256"], prepared.manifest.digest()
            )

    def test_exact_input_bytes_and_one_extra_byte_refuse_without_truncation(self):
        low, high = 1, 4096
        while low < high:
            count = (low + high + 1) // 2
            source = observation("a" * count)
            p = prepare_comment_batch(source, current(source))
            if p.terminal:
                high = count - 1
            else:
                low = count
        source = observation("a" * low)
        p = prepare_comment_batch(source, current(source))
        self.assertEqual(len(p.requests[0].text.encode()), 4096)
        source = observation("a" * (low + 1))
        p = prepare_comment_batch(source, current(source))
        self.assertEqual(p.terminal.reason, "decision-input-byte-limit")
        self.assertEqual(p.requests, ())

    def test_missing_native_groups_are_pending_not_noaction(self):
        source = observation("\n".join("Please check status" for _ in range(10)))
        live = current(source)
        prepared = prepare_comment_batch(source, live)
        extracted = normalize_decision_groups(prepared, {})
        result = complete_comment_batch(source, live, prepared, extracted)
        self.assertEqual(result.status, "extraction_pending")
        self.assertFalse(result.extraction_complete)
        self.assertFalse(extracted.usage_known)
        self.assertTrue(
            all(
                i.extraction_status == "pending" and i.proposal.action is None
                for i in result.items
            )
        )

    def test_wrong_native_model_question_or_completion_invalidates_entire_group(self):
        source = observation("\n".join("Please check status" for _ in range(10)))
        live = current(source)
        prepared = prepare_comment_batch(source, live)
        original = responses(
            prepared, {i: ("status", "context", "affirmative") for i in range(10)}
        )
        for field, value in (
            ("model", "fallback"),
            ("completion", "partial"),
            ("answers", {}),
            ("request_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                changed = dict(original)
                raw = json.loads(changed[prepared.requests[0].digest()])
                raw[field] = value
                changed[prepared.requests[0].digest()] = json.dumps(raw)
                extraction = normalize_decision_groups(prepared, changed)
                batch = complete_comment_batch(source, live, prepared, extraction)
                self.assertFalse(batch.extraction_complete)
                self.assertTrue(any(i.proposal.action == "status" for i in batch.items))
                self.assertTrue(
                    all(
                        i.proposal.action is None
                        for i in batch.items
                        if i.extraction_status == "invalid"
                    )
                )

    def test_manifest_omission_overlap_and_scalar_tampering_never_plan(self):
        source = observation("🙂 RS-ABC123 fixed\nRS-DEF456 fixed")
        prepared, extraction, _ = run_batch(source, {})
        manifest = prepared.manifest
        changed = replace(manifest, items=manifest.items[:1])
        forged = replace(prepared, manifest=changed)
        self.assertEqual(
            complete_comment_batch(source, current(source), forged, extraction).status,
            "refused",
        )
        first = replace(manifest.items[0], span=(1, manifest.items[0].span[1]))
        forged = replace(
            prepared, manifest=replace(manifest, items=(first, manifest.items[1]))
        )
        self.assertEqual(
            complete_comment_batch(source, current(source), forged, extraction).status,
            "refused",
        )
        with self.assertRaises(ReviewInputError):
            replace(manifest, items=(manifest.items[0], manifest.items[0]))
        with self.assertRaises(ReviewInputError):
            replace(manifest.items[0], span=(False, 10))
        with self.assertRaises(ReviewInputError):
            replace(manifest.items[0], references=(("finding", "RS-ABC123", 5, 13),))

    def test_unknown_and_missing_item_ids_reject_shared_extraction(self):
        from review_sensei.comment_intent_batch import ItemExtraction

        source = observation("RS-ABC123 fixed\nRS-DEF456 fixed")
        prepared, extracted, _ = run_batch(source, {})
        for items in (
            extracted.items[:1],
            (replace(extracted.items[0], item_id="0" * 64), extracted.items[1]),
        ):
            with self.subTest(items=items), self.assertRaises(ReviewInputError):
                complete_comment_batch(
                    source, current(source), prepared, replace(extracted, items=items)
                )
        with self.assertRaises(ReviewInputError):
            ItemExtraction("0" * 64, "accepted", (("intent", "execute"),))

    def test_changed_state_or_source_does_not_retarget_into_a_fresh_command(self):
        source = observation("RS-ABC123 fixed\nRS-DEF456 fixed")
        prepared, extracted, _ = run_batch(source, {})
        edited = replace(source, body="@reviewsensei verify", revision="edited")
        batch = complete_comment_batch(edited, current(edited), prepared, extracted)
        self.assertEqual(batch.status, "refused")
        self.assertTrue(all(i.proposal.status != "explicit" for i in batch.items))
        for updates in (
            {"actor_permission": "write"},
            {"model": "different"},
            {"session_terminal": False},
            {"invocation_authorized": False},
        ):
            batch = complete_comment_batch(
                source, current(source, **updates), prepared, extracted
            )
            self.assertEqual(batch.status, "refused")

    def test_actor_funding_and_historical_holds_remain_deterministic(self):
        source = observation("RS-ABC123 fixed\nRS-DEF456 fixed")
        p = prepare_comment_batch(source, current(source, extraction_admitted=False))
        self.assertEqual(p.terminal.reason, "extraction-not-admitted")
        for change in (
            {"actor_permission": "write"},
            {"session_terminal": False},
            {"inventory_complete": False},
            {"invocation_authorized": False},
        ):
            _, _, batch = run_batch(source, {}, **change)
            self.assertTrue(all(i.proposal.action is None for i in batch.items))
        bot = replace(source, actor_type="Bot")
        _, _, batch = run_batch(bot, {})
        self.assertTrue(all(i.proposal.action is None for i in batch.items))

    def test_positive_verify_stays_separate_from_pending_assessment(self):
        source = observation("RS-ABC123 fixed\nPlease recheck approval")
        _, _, batch = run_batch(
            source, {1: ("recheck_approval", "context", "affirmative")}
        )
        self.assertEqual(
            [i.proposal.action for i in batch.items], ["assess_claim", "verify"]
        )
        self.assertIn(
            "independent-criterion-assessment", batch.items[0].proposal.requirements
        )
        self.assertIn(
            "accounting-and-finalizer-gates", batch.items[1].proposal.requirements
        )
        self.assertFalse(batch.executable)

    def test_courtesy_quantifier_does_not_poison_independent_siblings(self):
        source = observation("RS-ABC123 fixed\nThanks both")
        _, _, batch = run_batch(source, {1: ("none", "context", "affirmative")})
        self.assertEqual(batch.items[0].proposal.action, "assess_claim")
        self.assertEqual(batch.items[1].proposal.status, "no_action")

    def test_targeted_negation_conflicts_only_with_related_assessment(self):
        source = observation(
            "RS-ABC123 fixed\nRS-DEF456 fixed\nDo not assess RS-ABC123"
        )
        _, _, batch = run_batch(source, {2: ("assess_claim", "context", "affirmative")})
        self.assertEqual(batch.items[0].proposal.reason, "cross-item-qualification")
        self.assertIsNone(batch.items[0].proposal.action)
        self.assertEqual(batch.items[1].proposal.action, "assess_claim")
        self.assertEqual(batch.items[1].proposal.targets, (OTHER_INSTANCE,))

    def test_global_qualifiers_leave_no_false_independence(self):
        for qualifier in (
            "Only the first",
            "Do not assess all of these",
            "Do not verify either",
        ):
            with self.subTest(qualifier=qualifier):
                source = observation(
                    "RS-ABC123 fixed\nPlease recheck approval\n" + qualifier
                )
                _, _, batch = run_batch(
                    source, {1: ("recheck_approval", "context", "affirmative")}
                )
                if "verify" in qualifier:
                    self.assertEqual(batch.items[0].proposal.action, "assess_claim")
                    self.assertIsNone(batch.items[1].proposal.action)
                elif "assess" in qualifier:
                    self.assertIsNone(batch.items[0].proposal.action)
                    self.assertEqual(batch.items[1].proposal.action, "verify")
                else:
                    self.assertTrue(all(i.proposal.action is None for i in batch.items))

    def test_unclosed_quotes_and_fences_are_not_fabricated_siblings(self):
        for text in (
            '"RS-ABC123 fixed\nRS-DEF456 fixed',
            "'do not don't stop\nRS-DEF456 fixed",
            "```\nRS-ABC123 fixed\nRS-DEF456 fixed",
        ):
            with self.subTest(text=text):
                source = observation(text)
                prepared, _, batch = run_batch(source, {})
                self.assertTrue(all(i.proposal.action is None for i in batch.items))
                self.assertTrue(
                    all(i.proposal.status == "evidence_only" for i in batch.items)
                )

    def test_many_refs_and_indivisible_multi_target_items_do_not_narrow(self):
        source = observation("RS-ABC123 and RS-DEF456 are fixed\nPlease check status")
        _, _, batch = run_batch(source, {1: ("status", "context", "affirmative")})
        self.assertEqual(batch.items[0].proposal.reason, "multiple-item-targets")
        self.assertEqual(batch.items[1].proposal.action, "status")
        source = observation("\n".join("RS-ABC123 RS-DEF456 fixed" for _ in range(6)))
        p = prepare_comment_batch(source, current(source))
        self.assertEqual(p.terminal.reason, "reference-count-limit")
        self.assertEqual(p.requests, ())


class BatchLiteralCompatibilityTests(unittest.TestCase):
    def test_wrong_head_standalone_rescan_preserves_refusal_before_extraction(self):
        for command in ("rescan", "review remaining"):
            source = observation("@reviewsensei " + command + " commit " + "a" * 40)
            p = prepare_comment_batch(
                source, current(source, extraction_admitted=False)
            )
            self.assertEqual(p.terminal.reason, "explicit-request-head-conflicts")
            self.assertEqual(p.requests, ())

    def test_every_embedded_reserved_form_has_no_legacy_authority(self):
        for fragment in (
            "@reviewsensei RS-DEF456 is acceptable risk please",
            "@reviewsensei I reviewed the media files and I approve please",
            "@reviewsensei review reenroll please",
            "@reviewsensei verify please",
        ):
            with self.subTest(fragment=fragment):
                source = observation("RS-ABC123 fixed\n" + fragment)
                _, _, batch = run_batch(source, {})
                self.assertEqual(batch.items[0].proposal.action, "assess_claim")
                self.assertEqual(
                    batch.items[1].proposal.reason, "explicit-command-required"
                )
                self.assertTrue(
                    all(i.proposal.status != "explicit" for i in batch.items)
                )


class BatchReviewRegressionTests(unittest.TestCase):
    def test_source_restrictions_survive_static_and_misclassified_items(self):
        for restriction in (
            "Do not assess RS-ABC123 or RS-DEF456",
            "Do not assess RS-ABC123",
        ):
            with self.subTest(restriction=restriction):
                source = observation("RS-ABC123 fixed\nRS-DEF456 fixed\n" + restriction)
                _, _, batch = run_batch(source, {2: ("none", "context", "affirmative")})
                self.assertIsNone(batch.items[0].proposal.action)
                if "or" in restriction:
                    self.assertIsNone(batch.items[1].proposal.action)
                else:
                    self.assertEqual(batch.items[1].proposal.action, "assess_claim")
        source = observation("Please recheck approval\nDo not verify")
        _, _, batch = run_batch(
            source,
            {i: ("recheck_approval", "context", "affirmative") for i in range(2)},
        )
        self.assertTrue(all(i.proposal.action is None for i in batch.items))

    def test_restrictions_use_only_unquoted_original_text(self):
        for restriction, blocked in (
            ('Do not assess either because "fixed" is misleading', True),
            ('"Do not assess either"', False),
            ('Do not assess RS-ABC123 because "RS-DEF456" is unrelated', True),
        ):
            with self.subTest(restriction=restriction):
                source = observation("RS-ABC123 fixed\nRS-DEF456 fixed\n" + restriction)
                _, _, batch = run_batch(source, {})
                self.assertEqual(batch.items[0].proposal.action is None, blocked)
                if "unrelated" in restriction or not blocked:
                    self.assertEqual(batch.items[1].proposal.action, "assess_claim")
                else:
                    self.assertIsNone(batch.items[1].proposal.action)

    def test_list_markers_cannot_hide_reserved_commands(self):
        for prefix in ("- ", "* ", "+ ", "1. ", "2) "):
            for command in (
                "review reenroll please",
                "verify",
                "verify please",
                "override RS-DEF456 acceptable risk",
            ):
                with self.subTest(prefix=prefix, command=command):
                    source = observation(
                        "RS-ABC123 fixed\n" + prefix + "@reviewsensei " + command
                    )
                    _, _, batch = run_batch(
                        source, {1: ("resume_review", "context", "affirmative")}
                    )
                    self.assertEqual(
                        batch.items[1].proposal.reason, "explicit-command-required"
                    )
                    self.assertIsNone(batch.items[1].proposal.action)
                    self.assertEqual(batch.items[0].proposal.action, "assess_claim")

    def test_numbered_labels_keep_original_spans_and_sentence_guard(self):
        source = observation("1. RS-ABC123: fixed\n2. RS-DEF456: fixed")
        _, _, batch = run_batch(source, {})
        self.assertEqual(
            [i.proposal.action for i in batch.items], ["assess_claim", "assess_claim"]
        )
        self.assertEqual(batch.items[0].proposal.claim.source_span_utf8, (0, 19))
        source = observation("1. RS-ABC123: fixed. Please verify\n2. RS-DEF456: fixed")
        _, _, batch = run_batch(source, {})
        self.assertIsNone(batch.items[0].proposal.action)
        self.assertEqual(batch.items[1].proposal.action, "assess_claim")

    def test_complete_item_packing_handles_adversarial_order(self):
        source = observation(
            "\n".join(
                "RS-ABC123 fixed in commit abcdef0 abcdef0 abcdef0 abcdef0"
                if i in (3, 7)
                else "Please check status"
                for i in range(10)
            )
        )
        prepared, _, batch = run_batch(
            source, {i: ("status", "context", "affirmative") for i in range(10)}
        )
        self.assertIsNone(prepared.terminal)
        self.assertEqual(len(prepared.requests), 3)
        self.assertEqual(sum(len(r.questions) for r in prepared.requests), 40)
        for i in range(10):
            owners = [
                r
                for r in prepared.requests
                if f"i{i}_intent" in {q.name for q in r.questions}
            ]
            self.assertEqual(len(owners), 1)
            expected = 8 if i in (3, 7) else 3
            self.assertEqual(
                sum(q.name.startswith(f"i{i}_") for q in owners[0].questions), expected
            )
        self.assertEqual(len(batch.items), 10)

    def test_source_restriction_survives_invalid_or_pending_native_group(self):
        source = observation(
            "\n".join(
                (
                    "RS-ABC123 fixed",
                    "RS-DEF456 fixed",
                    *("Please check status" for _ in range(5)),
                    "Do not assess RS-ABC123",
                )
            )
        )
        live = current(source)
        prepared = prepare_comment_batch(source, live)
        restrictive = next(
            r for r in prepared.requests if "i7_intent" in {q.name for q in r.questions}
        )
        for result in (None, "{}"):
            replies = responses(
                prepared, {i: ("status", "context", "affirmative") for i in range(2, 8)}
            )
            replies[restrictive.digest()] = result
            extracted = normalize_decision_groups(prepared, replies)
            batch = complete_comment_batch(source, live, prepared, extracted)
            self.assertIsNone(batch.items[0].proposal.action)
            self.assertEqual(batch.items[0].proposal.reason, "cross-item-qualification")
            self.assertEqual(batch.items[1].proposal.action, "assess_claim")
            self.assertFalse(batch.extraction_complete)

    def test_rescan_restriction_selects_fresh_review_not_remaining_review(self):
        for request, intent, blocked in (
            ("Please review this PR afresh", "fresh_review", True),
            ("Please finish the remaining review", "remaining_review", False),
        ):
            with self.subTest(intent=intent):
                source = observation(request + "\nDo not rescan")
                _, _, batch = run_batch(
                    source,
                    {
                        0: (intent, "context", "affirmative"),
                        1: ("none", "context", "affirmative"),
                    },
                )
                self.assertEqual(batch.items[0].proposal.action is None, blocked)
                if not blocked:
                    self.assertEqual(batch.items[0].proposal.action, "remaining")

    def test_broad_restriction_with_named_finding_does_not_narrow(self):
        for pointer in ("either", "both", "all of these", "only the first"):
            with self.subTest(pointer=pointer):
                source = observation(
                    "RS-ABC123 fixed\nRS-DEF456 fixed\nDo not assess "
                    + pointer
                    + " including RS-ABC123"
                )
                prepared, _, batch = run_batch(source, {})
                self.assertTrue(prepared.manifest.items[2].qualifier_broad)
                self.assertTrue(all(i.proposal.action is None for i in batch.items))
