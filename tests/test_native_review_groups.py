"""Bounded complete native groups retain each explanation and its guidance."""

import json
import unittest
from copy import deepcopy

from review_sensei_github.native_review_groups import NativeInline, NativeReviewPlan

from review_sensei.errors import ReviewInputError
from review_sensei.presentation import MAINTAINER_RESPONSE_INSTRUCTIONS


def maximum_mixture():
    from types import SimpleNamespace

    from review_sensei_github.operation_entry import analysis_payload
    from review_sensei_github.publication import ReviewPublisher

    from review_sensei.convergence import ReviewConvergencePolicy
    from review_sensei.diff import DiffAnalysis
    from review_sensei.models import ReviewComment, ReviewResult
    from review_sensei.placement import HostPlacementFacts

    label = "ΐ*" * 85
    comments = tuple(
        ReviewComment(
            path="f.py",
            line=1,
            body=f"{ordinal:03}: "
            + ("@ " * 4000 if ordinal < 111 else "Complete distinct explanation."),
            severity=label if ordinal < 111 else None,
            defect_kind=label if ordinal < 111 else None,
            blocking=True,
            effective_blocking=True,
        )
        for ordinal in range(250)
    )
    result = ReviewResult(
        summary="Complete.",
        comments=comments,
        provider="fixture",
        model="fixture",
        review_status="complete",
    )
    assert len(analysis_payload(SimpleNamespace(result=result))) == 1_041_854
    analysis = DiffAnalysis(
        changed_lines={"f.py": frozenset({1})},
        changed_paths=("f.py",),
        diff_bytes=100,
        diff_files=1,
        diff_hunks=1,
        diff_lines=5,
    )
    assembled = ReviewPublisher._assemble_review_body(
        object.__new__(ReviewPublisher),
        result=result,
        analysis=analysis,
        convergence_policy=ReviewConvergencePolicy(mode="legacy"),
        read_facts=lambda: HostPlacementFacts(),
        auto_approve=True,
        repository_id=1,
        pull_request=1,
        head_sha="b" * 40,
        base_sha="a" * 40,
        literal_malformed_fences=True,
    )
    return result, assembled


class NativeReviewPlanTests(unittest.TestCase):
    def test_unknown_output_body_envelope_includes_full_length_finding_ids(self):
        from review_sensei_github.native_review_groups import full_inline_envelope

        bound = full_inline_envelope()
        self.assertEqual(bound["findings"], 250)
        self.assertEqual(bound["body_calls"], 8)
        self.assertGreaterEqual(bound["fixed_inline_bytes"], 2863)
        self.assertGreater(bound["body_response_margin_bytes"], 0)
        self.assertLessEqual(bound["largest_body_response_bytes"], 512 * 1024)
        for packet in (
            "smallest_with_old_roots_bytes",
            "second_with_root_opening_bytes",
            "third_with_middle_bytes",
        ):
            self.assertLessEqual(bound[packet], 512 * 1024)

    def test_selected_metadata_bound_covers_signed_64_bit_ids_and_timestamp_escaping(
        self,
    ):
        from review_sensei_github.native_review_groups import (
            SELECTED_COMMENT_METADATA_BYTES,
            wire_bytes,
        )

        node = {
            "n": str(2**63 - 1),
            "l": 2**31 - 1,
            "o": 2**31 - 1,
            "a": {"t": "Bot", "n": 2**63 - 1},
            "c": {"o": "a" * 40},
            "z": {"o": "a" * 40},
            "r": {"n": str(2**63 - 1)},
            "q": None,
            "u": '"' * 64,
        }
        self.assertLessEqual(wire_bytes(node) + 16, SELECTED_COMMENT_METADATA_BYTES)

    def test_every_cased_unicode_mapping_obeys_the_classification_wire_bound(self):
        from review_sensei_github.native_review_groups import wire_bytes

        from review_sensei.presentation import _impact_label, _title_label

        # CPython lower's contextual final-sigma substitution changes no width.
        # Title's preceding-cased flag selects title/lower mappings; cover both
        # contexts, all actual changing scalars, ASCII punctuation and combining
        # separators. This runs on every supported platform/Python UCD table.
        chars = [
            chr(number)
            for number in range(0x110000)
            if not 0xD800 <= number <= 0xDFFF
            and (
                chr(number).lower() != chr(number)
                or chr(number).upper() != chr(number)
                or chr(number).title() != chr(number)
            )
        ]
        chars.extend('_*`[]()<>\\"@- \u0307\u0345\u200d')
        for char in chars:
            for value in (
                char,
                "a" + char,
                char + "a",
                "A'" + char,
                "Σ" + char + "A",
                "a\u0307" + char,
            ):
                for label, suffix in (
                    (_impact_label(value), len(" impact")),
                    (_title_label(value), 0),
                ):
                    if label is not None:
                        self.assertLessEqual(
                            wire_bytes(label) - 2 - suffix,
                            3 * (wire_bytes(value) - 2),
                            (value, label),
                        )

    def plan(self, items):
        return NativeReviewPlan.prepare(
            repository_id=1,
            pull_request=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            transaction_id="c" * 64,
            result_sha256="d" * 64,
            items=items,
        )

    def test_original_250_item_counterexample_fits_complete_groups(self):
        result, assembled = maximum_mixture()
        items = tuple(
            NativeInline.from_prepared(comment, body, anchor=anchor)
            for comment, _, body, anchor in assembled.inline_comments
        )
        plan = self.plan(items)
        self.assertEqual(sum(len(group) for group in plan.groups), 250)
        self.assertLessEqual(len(plan.groups), 8)
        self.assertEqual(
            {item.instance for item in plan.items},
            {item.instance for group in plan.groups for item in group},
        )
        self.assertEqual(plan, self.plan(items))
        for ordinal, group in enumerate(plan.groups):
            with self.subTest(ordinal=ordinal):
                self.assertLessEqual(plan.response_upper_bound(ordinal), 512 * 1024)
                self.assertLessEqual(len(group), 32)
                self.assertTrue(
                    all(MAINTAINER_RESPONSE_INSTRUCTIONS in item.body for item in group)
                )
                frame = plan.part_body(ordinal)
                self.assertNotIn("reviewsensei:eligibility:", frame)
                self.assertIn("Approval remains withheld", frame)

    def test_duplicate_instances_or_changed_body_never_share_one_plan(self):
        item = NativeInline(
            path="a.py",
            line=1,
            side="RIGHT",
            body="Complete explanation.",
            instance="e" * 64,
        )
        plan = self.plan((item,))
        with self.assertRaises(ReviewInputError):
            self.plan((item, item))
        altered = NativeInline(
            path=item.path,
            line=item.line,
            side=item.side,
            body="Changed explanation.",
            instance=item.instance,
        )
        self.assertNotEqual(plan.sha256, self.plan((altered,)).sha256)
        self.assertLess(len(json.dumps(plan.intent())), 4096)

    def response(self, plan, ordinal):
        review_id = 2**63 - 1 - ordinal
        return {
            "data": {
                "node": {
                    "n": str(review_id),
                    "b": plan.part_body(ordinal),
                    "s": "COMMENTED",
                    "a": {"t": "Bot", "n": 999},
                    "c": {"o": plan.head_sha},
                    "p": {"n": plan.pull_request, "r": {"n": plan.repository_id}},
                    "x": {
                        "totalCount": len(plan.groups[ordinal]),
                        "pageInfo": {"hasNextPage": False, "endCursor": "x" * 2048},
                        "nodes": [
                            {
                                "n": str(2**63 - 1024 - 100 * ordinal - index),
                                "b": item.body,
                                "p": item.path,
                                "l": item.line,
                                "o": item.line,
                                "a": {"t": "Bot", "n": 999},
                                "c": {"o": plan.head_sha},
                                "z": {"o": plan.head_sha},
                                "r": {"n": str(review_id)},
                                "q": None,
                                "u": "9999-12-31T23:59:59.999999Z",
                            }
                            for index, item in enumerate(plan.groups[ordinal])
                        ],
                    },
                }
            }
        }

    def test_physical_complete_body_reads_fit_and_use_one_shared_counter(self):
        from fake_github_http import json_response
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.native_review_groups import (
            NativeWalkBudget,
            read_complete_group,
        )
        from review_sensei_github.quarantine_census import is_readonly_census

        _, assembled = maximum_mixture()
        plan = self.plan(
            tuple(
                NativeInline.from_prepared(comment, body, anchor=anchor)
                for comment, _, body, anchor in assembled.inline_comments
            )
        )
        calls = []

        def opener(request, timeout):
            self.assertTrue(is_readonly_census(request, "https://api.github.test"))
            self.assertLessEqual(len(request.data), 8192)
            response = json_response(self.response(plan, len(calls)))
            self.assertLessEqual(len(response.body), 512 * 1024)
            calls.append(request)
            return response

        http = GitHubHttp(api_url="https://api.github.test", opener=opener)
        budget = NativeWalkBudget()
        observed = []
        for ordinal in range(len(plan.groups)):
            evidence = read_complete_group(
                http,
                token="synthetic",
                plan=plan,
                ordinal=ordinal,
                node_id="x" * 2048,
                expected_review_id=2**63 - 1 - ordinal,
                producer_id=999,
                budget=budget,
            )
            self.assertEqual(evidence.content_sha256, plan.part_digest(ordinal))
            observed.extend(evidence.comment_ids)
        self.assertEqual(len(observed), 250)
        self.assertEqual(len(set(observed)), 250)
        self.assertEqual(budget.used, len(plan.groups))
        self.assertLessEqual(budget.used, 8)

    def test_incomplete_foreign_changed_and_duplicate_group_bodies_refuse(self):
        from fake_github_http import json_response, make_http
        from review_sensei_github.native_review_groups import (
            NativeWalkBudget,
            read_complete_group,
        )

        _, assembled = maximum_mixture()
        plan = self.plan(
            tuple(
                NativeInline.from_prepared(comment, body, anchor=anchor)
                for comment, _, body, anchor in assembled.inline_comments[:2]
            )
        )
        good = self.response(plan, 0)
        changes = [
            lambda v: v["data"]["node"].update(b="changed"),
            lambda v: v["data"]["node"]["x"].update(totalCount=3),
            lambda v: v["data"]["node"]["x"]["pageInfo"].update(hasNextPage=True),
            lambda v: v["data"]["node"]["x"]["nodes"][0]["a"].update(n=998),
            lambda v: v["data"]["node"]["x"]["nodes"][0]["r"].update(n="999"),
            lambda v: v["data"]["node"]["x"]["nodes"][0].update(b="changed"),
            lambda v: v["data"]["node"]["x"]["nodes"].__setitem__(
                1, deepcopy(v["data"]["node"]["x"]["nodes"][0])
            ),
        ]
        for change in changes:
            value = deepcopy(good)
            change(value)
            http, calls = make_http([json_response(value)])
            budget = NativeWalkBudget()
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                read_complete_group(
                    http,
                    token="synthetic",
                    plan=plan,
                    ordinal=0,
                    node_id="node",
                    expected_review_id=2**63 - 1,
                    producer_id=999,
                    budget=budget,
                )
            self.assertEqual(budget.used, 1)
            self.assertEqual(len(calls), 1)
