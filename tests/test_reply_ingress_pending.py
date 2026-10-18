"""Complete source admission and host-authoritative pending delivery."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.conversation import (
    MAX_REPLY_BYTES,
    ConversationPublisher,
)
from review_sensei_github.errors import GitHubConversationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.human_assessment import HumanAssessmentPublisher
from review_sensei_github.pending import (
    MAX_PENDING_PAGE_BYTES,
    pending_reply_pages,
)
from review_sensei_github.reply_budget import (
    ReplyPublicationHttp,
    reply_publication_budget,
)

from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    MAX_HUMAN_SOURCE_BYTES,
    HumanAssessmentDecision,
    HumanAssessmentReply,
    HumanAssessmentService,
    HumanReviewFinding,
    PendingHumanReview,
)
from review_sensei.models import ConversationReply
from review_sensei.presentation import assign_finding_identifiers
from tests.test_human_assessment import (
    APP,
    BASE,
    HEAD,
    HUMAN,
    Provider,
    State,
    prior,
    response_for,
)


def inventory(count=250):
    return PendingHumanReview(
        BASE,
        tuple(
            HumanReviewFinding(f"{i:06x}" + "0" * 58, "src/app.py", f"Concern {i}")
            for i in range(count)
        ),
    )


def state_with_inventory(count=250):
    current = inventory(count)
    return State(replace(prior(), human_review=current))


class ReplyIngressTests(unittest.TestCase):
    def test_complete_ascii_and_multibyte_boundaries_host_and_service(self):
        self.assertEqual(MAX_HUMAN_SOURCE_BYTES, 32768)
        for size in (32767, 32768, 32769):
            for multibyte in (False, True):
                with self.subTest(size=size, multibyte=multibyte):
                    state = State()
                    extra = size - len(HUMAN.encode())
                    suffix = (
                        ("😀" * (extra // 4) + "x" * (extra % 4))
                        if multibyte
                        else "x" * extra
                    )
                    source = HUMAN + suffix
                    self.assertEqual(len(source.encode()), size)
                    state.source["body"] = source
                    publisher = HumanAssessmentPublisher(http=state.http)
                    admitted = publisher._source(
                        token="synthetic",
                        repository="owner/repo",
                        pull_request=1,
                        prepared=state.prepared(),
                        app_slug=APP,
                    )
                    self.assertEqual(admitted is not None, size <= 32768)
                    provider = Provider(response_for(state.eligibility))
                    if size > 32768:
                        with self.assertRaises(ReviewInputError):
                            HumanAssessmentService(provider).reply(
                                context=state.prepared().context,
                                pending=state.eligibility.human_review,
                                source_body=source,
                            )
                        self.assertEqual(provider.calls, [])
                    else:
                        self.assertEqual(admitted["body"], source)
                        HumanAssessmentService(provider).reply(
                            context=state.prepared().context,
                            pending=state.eligibility.human_review,
                            source_body=source,
                        )
                        prompt = provider.calls[0].prompt
                        reference = json.loads(prompt[prompt.index("{") :])
                        self.assertEqual(reference["human_reply"], source)
                        self.assertLessEqual(len(prompt.encode()), 49152)
                        self.assertEqual(provider.calls[0].max_prompt_bytes, 49152)

    def test_admitted_escaped_source_still_refuses_oversized_prompt_without_inference(
        self,
    ):
        state = State()
        source = HUMAN + "\\" * (32768 - len(HUMAN.encode()))
        state.source["body"] = source
        publisher = HumanAssessmentPublisher(http=state.http)
        admitted = publisher._source(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            prepared=state.prepared(),
            app_slug=APP,
        )
        self.assertEqual(admitted["body"], source)
        provider = Provider(response_for(state.eligibility))
        with self.assertRaises(ReviewInputError):
            HumanAssessmentService(provider).reply(
                context=state.prepared().context,
                pending=state.eligibility.human_review,
                source_body=source,
            )
        self.assertEqual(provider.calls, [])
        self.assertEqual(state.events(), [])


class PendingPresentationTests(unittest.TestCase):
    def test_shared_guidance_retains_every_identity_path_and_validated_reason(self):
        current = state_with_inventory(250).eligibility
        reasons = {
            item.fingerprint: f"Distinct rationale {index} 深🙂"
            for index, item in enumerate(current.human_review.pending)
        }
        arguments = dict(
            body="Discussion.",
            head_sha=HEAD,
            eligibility=current,
            decision_reasons=reasons,
        )
        self.assertEqual(
            pending_reply_pages(**arguments),
            pending_reply_pages(**arguments, layout="legacy"),
        )
        pages = pending_reply_pages(**arguments, layout="shared-guidance-v1")
        self.assertEqual(
            pages, pending_reply_pages(**arguments, layout="shared-guidance-v1")
        )
        self.assertNotEqual(
            pages[0].report_digest, pending_reply_pages(**arguments)[0].report_digest
        )
        text = "\n".join(page.body for page in pages)
        identifiers = assign_finding_identifiers(
            tuple(item.fingerprint for item in current.human_review.findings)
        )
        for item in current.human_review.pending:
            self.assertEqual(text.count(item.fingerprint), 1)
            self.assertIn(f"**{identifiers[item.fingerprint]}**", text)
            self.assertIn("src&#47;app&#46;py", text)
            self.assertIn(reasons[item.fingerprint], text)
        self.assertIn("for each exact ID in a new authorized mention", text)
        self.assertTrue(
            all(len(page.body.encode()) <= MAX_PENDING_PAGE_BYTES for page in pages)
        )

    def test_unknown_layout_or_oversized_individual_body_refuses_without_truncation(
        self,
    ):
        for layout in ("unknown", {}, [], None):
            with self.subTest(layout=layout), self.assertRaises(ReviewInputError):
                pending_reply_pages(
                    body="Discussion.",
                    head_sha=HEAD,
                    eligibility=prior(),
                    layout=layout,
                )
        for layout in ("legacy", "shared-guidance-v1"):
            with self.subTest(layout=layout), self.assertRaises(ReviewInputError):
                pending_reply_pages(
                    body="深" * 1366, head_sha=HEAD, eligibility=prior(), layout=layout
                )

    def test_unicode_rationale_controls_cannot_reorder_authority(self):
        current = prior()
        finding = current.human_review.pending[0]
        text = pending_reply_pages(
            body="Discussion.",
            head_sha=HEAD,
            eligibility=current,
            decision_reasons={
                finding.fingerprint: "A\u202eB\u2066C\u200bD\x00E\u00a0F\u2028G"
            },
        )[0].body
        self.assertIn("AU202EBU2066CU200BDU0000E F G", text)
        for control in ("\u202e", "\u2066", "\u200b", "\x00", "\u00a0", "\u2028"):
            self.assertNotIn(control, text)
        self.assertEqual(text.count(finding.fingerprint), 1)
        self.assertIn("does not grant approval", text)

    def test_untrusted_locations_and_reasons_cannot_format_authority(self):
        path = "src/[APPROVED](https:evil)`*_file.py"
        reason = "[APPROVED](https://evil) **cleared** `done` @admin <b>x</b> \\"
        finding = HumanReviewFinding("a" * 64, path, "Concern")
        current = replace(prior(), human_review=PendingHumanReview(BASE, (finding,)))
        text = pending_reply_pages(
            body="Discussion.",
            head_sha=HEAD,
            eligibility=current,
            decision_reasons={finding.fingerprint: reason},
        )[0].body
        location = text.split(f"(`{finding.fingerprint}`), ", 1)[1].split(
            ": Validated reassessment reason: ", 1
        )[0]
        rationale = text.split("Validated reassessment reason: ", 1)[1].split(
            " Next: ", 1
        )[0]
        self.assertEqual(html.unescape(location), path)
        self.assertEqual(html.unescape(rationale), reason)
        for fragment in (location, rationale):
            for control in "`*_[\\]()<>@":
                self.assertNotIn(control, fragment)
        self.assertIn("does not grant approval", text)
        self.assertEqual(text.count(finding.fingerprint), 1)

    def test_maximum_inventory_worst_paths_reasons_and_collisions_fit_complete_delivery(
        self,
    ):
        path = "/".join(["x" * 240] * 17)
        self.assertEqual(len(path.encode()), 4096)
        findings = tuple(
            HumanReviewFinding("a" * 56 + f"{index:08x}", path, f"Concern {index}")
            for index in range(250)
        )
        current = replace(prior(), human_review=PendingHumanReview(BASE, findings))
        state = State(current)
        publisher, prepared = state.bridge()
        reply = HumanAssessmentReply(
            "x" * 4096,
            tuple(
                HumanAssessmentDecision(
                    item.fingerprint, "unresolved", "😀" * 256, "", ""
                )
                for item in findings
            ),
        )
        before_publish = len(state.calls)
        outcome = publisher.publish(
            token="issue",
            review_token=None,
            repository="owner/repo",
            pull_request=1,
            prepared=prepared,
            reply=reply,
            app_slug=APP,
        )
        self.assertEqual(outcome.status, "replied")
        self.assertGreater(state.reply_count, 1)
        self.assertLessEqual(state.reply_count, 16)
        delivery_calls = state.calls[before_publish:]
        self.assertLessEqual(len(delivery_calls), 128)
        self.assertLessEqual(
            sum(method not in {"GET", "HEAD"} for method, _, _ in delivery_calls), 32
        )
        self.assertEqual(state.events(), [])
        text = "\n".join(item["body"] for item in state.replies)
        for item in findings:
            self.assertIn(item.fingerprint, text)
            self.assertIn("**RS-" + item.fingerprint.upper() + "**", text)
        count = state.reply_count
        self.assertEqual(
            publisher.publish(
                token="issue",
                review_token=None,
                repository="owner/repo",
                pull_request=1,
                prepared=prepared,
                reply=reply,
                app_slug=APP,
            ).status,
            "already_replied",
        )
        self.assertEqual(state.reply_count, count)

    def test_current_authority_wins_over_old_finding_in_provider_prose(self):
        current = replace(
            prior(),
            human_review=None,
            facts=replace(
                prior().facts,
                has_human_adjudication_findings=False,
                review_status="partial",
                coverage_blocker="partial",
            ),
        )
        pages = pending_reply_pages(
            body="RS-F7A776 is still pending because I lack its gate code.",
            head_sha=HEAD,
            eligibility=current,
        )
        text = "\n".join(page.body for page in pages)
        authority, discussion = text.split("## Provider discussion", 1)
        self.assertNotIn("RS-F7A776", authority)
        self.assertIn("No pending human-assessment findings were recorded", authority)
        self.assertIn("coverage is `partial`", authority)
        self.assertIn("not current-head authority", authority)
        self.assertIn("RS-F7A776", discussion)

    def test_static_approval_requirements_match_canonical_policy(self):
        current = replace(prior(), human_review=None)
        clean = replace(
            current.facts,
            has_human_adjudication_findings=False,
            has_blocking_findings=False,
            review_status="complete",
            coverage_blocker="reviewed",
        )
        for change, expected in (
            ({"enabled": False}, "Automatic approval is disabled"),
            ({"app_authored": True}, "pull request is App-authored"),
            (
                {"review_status": "partial", "evidence_policy": "confirmed"},
                "Confirmed review evidence is unverified",
            ),
        ):
            with self.subTest(change=change):
                text = pending_reply_pages(
                    body="Discussion.",
                    head_sha=HEAD,
                    eligibility=replace(current, facts=replace(clean, **change)),
                )[0].body
                self.assertIn(expected, text)
                self.assertNotIn("No additional blocker was recorded", text)
        text = pending_reply_pages(
            body="Discussion.",
            head_sha=HEAD,
            eligibility=replace(current, facts=replace(clean, check_published=False)),
        )[0].body
        self.assertIn("No additional blocker was recorded", text)
        self.assertNotIn("Review coverage is", text)
        self.assertIn("Live thread", text)

    def test_all_250_ids_are_preserved_sorted_and_pages_bounded(self):
        current = replace(prior(), human_review=inventory())
        pages = pending_reply_pages(
            body="Discussion.", head_sha=HEAD, eligibility=current
        )
        self.assertGreater(len(pages), 1)
        self.assertLessEqual(len(pages), 16)
        text = "\n".join(page.body for page in pages)
        identifiers = assign_finding_identifiers(
            item.fingerprint for item in current.human_review.findings
        )
        for item in current.human_review.pending:
            self.assertEqual(text.count(item.fingerprint), 1)
            self.assertEqual(text.count("**" + identifiers[item.fingerprint] + "**"), 1)
        self.assertTrue(
            all(len(page.body.encode()) <= MAX_PENDING_PAGE_BYTES for page in pages)
        )
        self.assertEqual(
            pages,
            pending_reply_pages(body="Discussion.", head_sha=HEAD, eligibility=current),
        )

    def test_collision_ids_keep_full_fingerprints_and_do_not_drop_items(self):
        current = replace(
            prior(),
            human_review=PendingHumanReview(
                BASE,
                (
                    HumanReviewFinding("a" * 63 + "0", "a.py", "first"),
                    HumanReviewFinding("a" * 63 + "1", "b.py", "second"),
                ),
            ),
        )
        text = pending_reply_pages(
            body="Discussion.", head_sha=HEAD, eligibility=current
        )[0].body
        for item in current.human_review.pending:
            self.assertIn("**RS-" + item.fingerprint.upper() + "**", text)

    def test_mixed_cleared_pending_uses_validated_reason_and_preserves_coverage(self):
        original = inventory(2)
        current = replace(
            prior(),
            human_review=replace(
                original, resolved=(original.findings[0].fingerprint,)
            ),
            facts=replace(
                prior().facts, review_status="partial", coverage_blocker="partial"
            ),
        )
        remaining = original.findings[1].fingerprint
        pages = pending_reply_pages(
            body="Some evidence was insufficient.",
            head_sha=HEAD,
            eligibility=current,
            decision_reasons={
                remaining: "The current patch does not show the required request guard."
            },
            assessment_digest="a" * 64,
        )
        text = "\n".join(page.body for page in pages)
        self.assertNotIn(original.findings[0].fingerprint, text)
        self.assertIn(remaining, text)
        self.assertIn("required request guard", text)
        self.assertIn("coverage is `partial`", text)
        self.assertIn("does not grant approval", text)

    def test_body_decisions_and_result_all_bind_report_identity(self):
        current = prior()
        one = pending_reply_pages(
            body="one", head_sha=HEAD, eligibility=current, assessment_digest="a" * 64
        )
        for other in (
            pending_reply_pages(
                body="two",
                head_sha=HEAD,
                eligibility=current,
                assessment_digest="a" * 64,
            ),
            pending_reply_pages(
                body="one",
                head_sha=HEAD,
                eligibility=current,
                assessment_digest="b" * 64,
            ),
            pending_reply_pages(
                body="one",
                head_sha=HEAD,
                eligibility=replace(current, result_digest="c" * 64),
                assessment_digest="a" * 64,
            ),
        ):
            self.assertNotEqual(one[0].report_digest, other[0].report_digest)


class ReplyTransportTests(unittest.TestCase):
    def test_adapter_retains_transport_configuration_and_primed_cache(self):
        transport = GitHubHttp(api_url="https://example.test", timeout=7)
        transport.primed_issue_comments["/cached"] = [{"id": 1}]
        transport.custom_configuration = object()
        # This checks adapter configuration; a fixed clock avoids platform float
        # rounding in (now + budget) - now while retaining the strict limit.
        with (
            patch("review_sensei_github.reply_budget.time.monotonic", return_value=100),
            patch.object(transport, "request", return_value=(200, [])) as request,
        ):
            wrapped = ReplyPublicationHttp(transport)
            self.assertIs(
                wrapped.primed_issue_comments, transport.primed_issue_comments
            )
            self.assertIs(wrapped.custom_configuration, transport.custom_configuration)
            wrapped.request("GET", "/cached", token="synthetic")
            self.assertEqual(wrapped.requests, 1)
            request.assert_called_once()
            self.assertLessEqual(request.call_args.kwargs["timeout_seconds"], 60)

    def test_distinct_transports_fail_before_rerouting_or_writing(self):
        first = SimpleNamespace(http=GitHubHttp(api_url="https://one.test"))
        second = SimpleNamespace(http=GitHubHttp(api_url="https://two.test"))
        originals = (first.http, second.http)
        with self.assertRaises(GitHubConversationError):
            with reply_publication_budget(first, second):
                self.fail("different transports must not enter publication")
        self.assertIs(first.http, originals[0])
        self.assertIs(second.http, originals[1])

    def test_nested_budget_shares_counters_and_restores_after_failure(self):
        transport = GitHubHttp()
        owner = SimpleNamespace(http=transport)
        with patch.object(transport, "request", return_value=(200, [])):
            with reply_publication_budget(owner):
                wrapped = owner.http
                wrapped.request("GET", "/one", token="synthetic")
                with self.assertRaises(RuntimeError):
                    with reply_publication_budget(owner):
                        self.assertIs(owner.http, wrapped)
                        owner.http.request("POST", "/two", token="synthetic")
                        raise RuntimeError("interrupted")
                self.assertIs(owner.http, wrapped)
                self.assertEqual((wrapped.requests, wrapped.writes), (2, 1))
        self.assertIs(owner.http, transport)


class PendingDeliveryTests(unittest.TestCase):
    def publish(
        self, state, *, body="Discussion.", fence=lambda: None, layout="legacy"
    ):
        return ConversationPublisher(http=state.http).publish_pending(
            token="synthetic",
            repository="owner/repo",
            pull_request=1,
            source_comment_id=10,
            source_updated_at="updated",
            head_sha=HEAD,
            reply=ConversationReply(body, False),
            app_slug=APP,
            root_comment_id=10,
            source_kind="issue",
            eligibility=state.eligibility,
            fence=fence,
            layout=layout,
        )

    def test_shared_report_rechecks_whole_report_after_later_page_mutation(self):
        state = state_with_inventory()
        original_request = state.http.request

        def mutate_previous_page(method, path, **arguments):
            result = original_request(method, path, **arguments)
            if (
                method == "POST"
                and path.endswith("/issues/1/comments")
                and state.reply_count == 2
            ):
                state.replies[0]["body"] = "changed " + state.replies[0]["body"]
            return result

        state.http.request = mutate_previous_page
        with self.assertRaisesRegex(GitHubConversationError, "receipt conflicts"):
            self.publish(state, layout="shared-guidance-v1")
        self.assertGreater(state.reply_count, 1)
        self.assertEqual(state.events(), [])

    def test_shared_report_whole_readback_failure_cannot_claim_complete(self):
        state = state_with_inventory()
        original_request = state.http.request
        scans_after_last_post = 0

        def lose_final_readback(method, path, **arguments):
            nonlocal scans_after_last_post
            if (
                method == "GET"
                and "/issues/1/comments" in path
                and state.reply_count == 3
            ):
                scans_after_last_post += 1
                if scans_after_last_post == 2:
                    raise GitHubConversationError("whole report readback unavailable")
            return original_request(method, path, **arguments)

        expected = pending_reply_pages(
            body="Discussion.",
            head_sha=HEAD,
            eligibility=state.eligibility,
            layout="shared-guidance-v1",
        )
        self.assertEqual(len(expected), 3)
        state.http.request = lose_final_readback
        with self.assertRaisesRegex(GitHubConversationError, "whole report readback"):
            self.publish(state, layout="shared-guidance-v1")
        self.assertEqual(state.events(), [])

    def test_long_delivery_exact_readback_and_retry_create_no_duplicates(self):
        state = state_with_inventory()
        first = self.publish(state)
        self.assertEqual(first.status, "replied")
        self.assertGreater(state.reply_count, 1)
        count = state.reply_count
        second = self.publish(state)
        self.assertEqual(second.status, "already_replied")
        self.assertEqual(state.reply_count, count)
        self.assertTrue(
            all(
                len(reply["body"].encode()) <= MAX_REPLY_BYTES
                for reply in state.replies
            )
        )
        for item in state.eligibility.human_review.pending:
            self.assertEqual(
                sum(reply["body"].count(item.fingerprint) for reply in state.replies), 1
            )
        self.assertEqual(state.events(), [])

    def test_stale_between_pages_stops_without_claiming_complete(self):
        state = state_with_inventory()
        outcome = self.publish(
            state, fence=lambda: "skipped_stale_head" if state.reply_count else None
        )
        self.assertEqual(outcome.status, "skipped_stale_head")
        self.assertEqual(outcome.assessment_status, "pending_delivery_incomplete")
        self.assertEqual(state.reply_count, 1)
        self.assertEqual(state.events(), [])
        self.assertEqual(self.publish(state).status, "replied")
        self.assertEqual(self.publish(state).status, "already_replied")

    def test_marker_with_modified_app_body_refuses_resume(self):
        state = state_with_inventory()
        self.publish(state)
        changed = "CHANGED" + state.replies[0]["body"]
        content = changed.rpartition("\n\n<!-- reviewsensei:")[0]
        state.replies[0]["body"] = re.sub(
            r"content=[a-f0-9]{64}",
            "content=" + hashlib.sha256(content.encode()).hexdigest(),
            changed,
        )
        modified_content, marker = state.replies[0]["body"].rsplit(
            "\n\n<!-- reviewsensei:", 1
        )
        self.assertEqual(modified_content, content)
        self.assertIn(f"content={hashlib.sha256(content.encode()).hexdigest()}", marker)
        with self.assertRaises(GitHubConversationError):
            self.publish(state)
        self.assertEqual(state.events(), [])

    def test_wrong_head_report_is_rejected_before_any_request(self):
        state = State()
        with self.assertRaises(ReviewInputError):
            ConversationPublisher(http=state.http).publish_pending(
                token="synthetic",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at="updated",
                head_sha="c" * 40,
                reply=ConversationReply("Discussion.", False),
                app_slug=APP,
                root_comment_id=10,
                source_kind="issue",
                eligibility=state.eligibility,
                fence=lambda: None,
            )
        self.assertEqual(state.calls, [])

    def test_cumulative_transport_exhaustion_does_not_advance_authority(self):
        state = state_with_inventory()
        original = state.reviews[0]["body"]
        with patch(
            "review_sensei_github.reply_budget.MAX_REPLY_PUBLICATION_REQUESTS",
            8,
        ):
            with self.assertRaises(GitHubConversationError):
                self.publish(state)
        self.assertEqual(state.reviews[0]["body"], original)
        self.assertEqual(state.events(), [])
        self.assertLessEqual(len(state.calls), 8)

    def test_human_insufficient_evidence_lists_every_pending_id_without_provider(self):
        state = State(prior(extra_human=True))
        state.files = []
        provider = Provider({})
        outcome, _ = state.application_reply(provider)
        self.assertEqual(outcome.assessment_status, "insufficient_evidence")
        self.assertEqual(provider.calls, [])
        text = "\n".join(item["body"] for item in state.replies)
        for item in state.eligibility.human_review.pending:
            self.assertIn(item.fingerprint, text)
        self.assertEqual(state.events(), [])

    def test_delivery_deadline_uses_one_wrapper_and_charges_before_dispatch(self):
        state = State()
        wrapped = ReplyPublicationHttp(state.http)
        wrapped.deadline = 0
        with self.assertRaises(GitHubConversationError):
            wrapped.request("GET", "/repos/owner/repo/pulls/1", token="synthetic")
        self.assertEqual(state.calls, [])


if __name__ == "__main__":
    unittest.main()
