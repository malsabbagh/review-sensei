"""Source-produced finalization witnesses; no live provider, Worker or GitHub."""

from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from review_sensei_github.action_completion import (
    CompletionScope,
    CompletionState,
    FinalizationCompletion,
    FinalizationSubject,
    approval_confirmed,
    finalization_confirmed,
    reply_completion_from_finalization,
    reply_source,
    require_reply_completion,
)
from review_sensei_github.action_reply import _outcome
from review_sensei_github.api_run_history import read_previous_run
from review_sensei_github.api_run_requests import (
    ApiRunRequestTransport,
    ApiRunSessionLedger,
)
from review_sensei_github.checks import review_check_outcome
from review_sensei_github.conversation import ConversationPublisher, ReplyResult
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import (
    IndexedFinding,
    PublicationResult,
    ReviewApprovalFinalizer,
    ReviewPublisher,
    approval_eligibility_marker,
    finding_index_marker,
)
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)
from review_sensei_github.thread_resolution import ThreadResolutionResult
from test_action_reply import BODY, SOURCE, ReplyFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_human_assessment import APP, HUMAN, Provider, State, prior, response_for

from review_sensei.api_work import ApiExecutionOwner, ApiRunIdentity
from review_sensei.errors import ReviewInputError
from review_sensei.models import ConversationReply

ENV = {"GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1"}


def add_current_index(state):
    eligibility = state.eligibility
    entries = tuple(
        IndexedFinding(
            instance=item.fingerprint,
            lifecycle=item.fingerprint,
            blocking=False,
            needs_human=True,
            body_placed=True,
        )
        for item in eligibility.human_review.findings
    )
    entries += (IndexedFinding("d" * 64, "d" * 64, True, False, body_placed=True),)
    state.reviews[0]["body"] += "\n" + finding_index_marker(
        repository_id=1,
        pull_request=1,
        head_sha=HEAD,
        base_sha=BASE,
        result_digest=eligibility.result_digest,
        entries=entries,
    )


def expected_authority(state):
    return {
        "repository": "owner/repo",
        "pull_request": 1,
        "head_sha": HEAD,
        "source": {
            "kind": "issue_comment",
            "id": 10,
            "updated_at": "updated",
            "body_sha256": hashlib.sha256(HUMAN.encode()).hexdigest(),
        },
    }


class SourceCompletionTests(unittest.TestCase):
    def test_review_publication_requires_confirmed_check_then_explicit_cleanup(self):
        state = State(prior(blocking=True))
        publisher = ReviewPublisher(http=state.http)
        outcome = review_check_outcome(
            policy="auto-approve",
            review_status="complete",
            required_fixes=True,
            required_decisions=True,
        )
        diagnostic = publisher._publish_check_state(
            check_token="check",
            repository="owner/repo",
            head_sha=HEAD,
            app_slug=APP,
            outcome=outcome,
        )
        publication = publisher._finalize_after_publication(
            token="review",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=state.eligibility,
            check_diagnostic=diagnostic,
            status="published",
            review_id=50,
        )
        self.assertEqual(
            publication.finalization_completion.scope, CompletionScope.PUBLICATION
        )
        self.assertFalse(finalization_confirmed(publication))
        completed = publisher._finish_thread_cleanup(publication)
        self.assertTrue(finalization_confirmed(completed))
        self.assertIs(publisher._finish_thread_cleanup(completed), completed)
        publisher._pending_thread_cleanup = lambda: "thread_resolution_unconfirmed"
        incomplete = publisher._finish_thread_cleanup(publication)
        self.assertFalse(finalization_confirmed(incomplete))
        self.assertEqual(incomplete.thread_resolution, "unconfirmed")

    def test_actual_human_reassessment_confirms_withholding_after_check_tail(self):
        state = State(prior(blocking=True))
        add_current_index(state)
        with patch.dict("os.environ", ENV, clear=True):
            result, _ = state.application_reply(
                Provider(response_for(state.eligibility))
            )
        self.assertEqual(result.status, "replied")
        self.assertEqual(result.approval_status, "approval_withheld")
        self.assertTrue(state.check_runs)
        self.assertTrue(
            require_reply_completion(
                result, expected_authority(state), run_id="456", run_attempt="1"
            )
        )
        self.assertEqual(
            result.finalization_completion.scope, CompletionScope.FINALIZATION
        )

    def test_actual_masked_finalizer_exception_keeps_default_unconfirmed(self):
        state = State(prior(blocking=True))
        add_current_index(state)
        with (
            patch.dict("os.environ", ENV, clear=True),
            patch.object(
                ReviewApprovalFinalizer,
                "finalize",
                side_effect=GitHubPublicationTransientError("unknown approval write"),
            ),
        ):
            result, _ = state.application_reply(
                Provider(response_for(state.eligibility))
            )
        self.assertEqual(result.approval_status, "approval_withheld")
        self.assertIsNone(result.finalization_completion)
        self.assertFalse(
            require_reply_completion(
                result, expected_authority(state), run_id="456", run_attempt="1"
            )
        )

    def test_actual_unknown_check_write_never_issues_completion(self):
        state = State(prior(blocking=True))
        add_current_index(state)

        def fail_check(_state, method, path, _body):
            if method == "POST" and path.endswith("/check-runs"):
                raise GitHubPublicationTransientError("unknown check write")

        state.hook = fail_check
        with patch.dict("os.environ", ENV, clear=True):
            result, _ = state.application_reply(
                Provider(response_for(state.eligibility))
            )
        self.assertIsNone(result.finalization_completion)
        self.assertEqual(result.approval_diagnostic, "check_reconciliation_failed")

    def test_known_approval_without_selected_check_confirmation_is_not_full_completion(
        self,
    ):
        state = State(prior(blocking=True))
        add_current_index(state)
        finalizer = ReviewApprovalFinalizer(http=state.http)
        approval = finalizer.finalize(
            token="review",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=state.eligibility,
        )
        self.assertTrue(approval_confirmed(approval))
        result = finalizer.finalize_recorded_decisions(
            token="review",
            repository="owner/repo",
            pull_request=1,
            head_sha=HEAD,
            app_slug=APP,
            eligibility=state.eligibility,
        )
        self.assertEqual(result.status, "approval_withheld")
        self.assertFalse(finalization_confirmed(result))

    def test_unconfirmed_thread_tail_cannot_borrow_confirmed_approval(self):
        state = State(prior(blocking=True))
        add_current_index(state)
        finalizer = ReviewApprovalFinalizer(http=state.http)
        with patch.object(
            finalizer,
            "reconcile_decided_threads",
            return_value=ThreadResolutionResult(unconfirmed=("d" * 64,)),
        ):
            result = finalizer.finalize_recorded_decisions(
                token="review",
                check_token="check",
                repository="owner/repo",
                pull_request=1,
                head_sha=HEAD,
                app_slug=APP,
                eligibility=state.eligibility,
            )
        self.assertTrue(state.check_runs)
        self.assertEqual(result.thread_resolution, "unconfirmed")
        self.assertFalse(finalization_confirmed(result))

    def test_generic_source_publisher_explicitly_confirms_unnecessary_finalization(
        self,
    ):
        state = State()
        with patch.dict("os.environ", ENV, clear=True):
            result = ConversationPublisher(http=state.http).publish_current(
                token="reply",
                repository="owner/repo",
                pull_request=1,
                source_comment_id=10,
                source_updated_at="updated",
                head_sha=HEAD,
                reply=ConversationReply(body="Thanks for the question.", resolve=False),
                app_slug=APP,
                root_comment_id=10,
                source_kind="issue",
            )
        self.assertEqual(result.status, "replied")
        self.assertEqual(
            result.finalization_completion.scope, CompletionScope.UNNECESSARY
        )
        self.assertTrue(
            require_reply_completion(
                result, expected_authority(state), run_id="456", run_attempt="1"
            )
        )

    def test_process_identity_scope_and_exact_result_are_required(self):
        state = State(prior(blocking=True))
        add_current_index(state)
        with patch.dict("os.environ", ENV, clear=True):
            result, _ = state.application_reply(
                Provider(response_for(state.eligibility))
            )
        authority = expected_authority(state)
        self.assertTrue(
            require_reply_completion(result, authority, run_id="456", run_attempt="1")
        )
        for changed in (
            replace(result, comment_id=result.comment_id + 1),
            replace(result, approval_diagnostic="another result"),
            replace(
                result, finalization_completion=asdict(result.finalization_completion)
            ),
            replace(
                result,
                finalization_completion=copy.deepcopy(result.finalization_completion),
            ),
            replace(result, finalization_completion=True),
        ):
            with self.subTest(changed=changed):
                self.assertFalse(
                    require_reply_completion(
                        changed, authority, run_id="456", run_attempt="1"
                    )
                )
        for name, value in (
            ("repository", "other/repo"),
            ("pull_request", 2),
            ("head_sha", "c" * 40),
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    require_reply_completion(
                        result,
                        {**authority, name: value},
                        run_id="456",
                        run_attempt="1",
                    )
                )
        for name, value in (
            ("id", 11),
            ("updated_at", "edited"),
            ("body_sha256", "f" * 64),
            ("kind", "review_comment"),
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    require_reply_completion(
                        result,
                        {**authority, "source": {**authority["source"], name: value}},
                        run_id="456",
                        run_attempt="1",
                    )
                )
        self.assertFalse(
            require_reply_completion(result, authority, run_id="457", run_attempt="1")
        )
        self.assertFalse(
            require_reply_completion(result, authority, run_id="456", run_attempt="2")
        )

    def test_typed_constructor_or_boolean_never_mints_process_confirmation(self):
        subject = FinalizationSubject("owner/repo", 1, HEAD, "d" * 64)
        for state in (CompletionState.UNCONFIRMED, CompletionState.CONFIRMED):
            result = PublicationResult(
                "approval_withheld",
                finalization_completion=FinalizationCompletion(
                    subject,
                    CompletionScope.APPROVAL,
                    "e" * 64,
                    state,
                ),
            )
            self.assertFalse(approval_confirmed(result))
        with self.assertRaises(ReviewInputError):
            _outcome(
                ReplyResult(
                    "replied", comment_id=1, approval_status="approval_withheld"
                )
            )


class NativeWithholdingActionTests(unittest.TestCase):
    def test_unknown_native_thread_write_remains_pending_despite_issued_result_proof(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            finalizer, eligibility = self.prepare_finalizer(fixture, run)
            original_opener = run.caller._opener

            def unknown_thread(request, timeout):
                if request.full_url.endswith("/graphql"):
                    fixture.trace.append((fixture.attempt, "POST", "/graphql"))
                    raise TimeoutError("synthetic unknown thread mutation")
                return original_opener(request, timeout=timeout)

            def complete_then_unknown(**_kwargs):
                publication = finalizer.finalize_recorded_decisions(
                    token=run.ledger.token,
                    check_token="synthetic-check",
                    repository=IDENTITY.repository,
                    pull_request=7,
                    head_sha=HEAD,
                    app_slug="reviewsensei[bot]",
                    eligibility=eligibility,
                )
                self.assertTrue(finalization_confirmed(publication))
                result = ReplyResult(
                    "replied",
                    comment_id=91,
                    approval_status=publication.status,
                    approval_diagnostic=publication.diagnostic,
                    resolution_status=publication.thread_resolution,
                )
                source = reply_source(
                    repository=IDENTITY.repository,
                    pull_request=7,
                    head_sha=HEAD,
                    kind="issue",
                    source_id=41,
                    updated_at=SOURCE["updated_at"],
                    body=BODY,
                )
                result = replace(
                    result,
                    finalization_completion=reply_completion_from_finalization(
                        publication,
                        result,
                        source,
                        result_digest=eligibility.result_digest,
                    ),
                )
                run.caller._opener = unknown_thread
                with self.assertRaises(TimeoutError):
                    with run.caller.open(
                        Request(
                            "https://api.github.test/graphql",
                            method="POST",
                            data=b'{"query":"mutation ResolveReviewThread"}',
                        ),
                        timeout=10,
                    ) as response:
                        response.read(1)
                return result

            with self.assertRaises(ReviewInputError):
                fixture.invoke(run, args, complete_then_unknown)
            self.assertEqual(fixture.action()["phase"], "pending")
            self.assertGreater(run.caller.accounting.uncertain, 0)
            with self.assertRaises(ReviewInputError):
                self.rollover(fixture, run, directory)

    def prepare_finalizer(self, fixture, run):
        eligibility = prior(blocking=True)
        eligibility = replace(
            eligibility,
            human_review=replace(
                eligibility.human_review,
                resolved=tuple(
                    item.fingerprint for item in eligibility.human_review.findings
                ),
            ),
            facts=replace(eligibility.facts, has_human_adjudication_findings=False),
        )
        fixture.reviews = [
            {
                "id": 100,
                "commit_id": HEAD,
                "state": "COMMENTED",
                "user": {"login": "reviewsensei[bot]", "type": "Bot", "id": 999},
                "body": approval_eligibility_marker(eligibility)
                + "\n"
                + finding_index_marker(
                    repository_id=123,
                    pull_request=7,
                    head_sha=HEAD,
                    base_sha=BASE,
                    result_digest=eligibility.result_digest,
                    entries=(
                        IndexedFinding(
                            "d" * 64, "d" * 64, True, False, body_placed=True
                        ),
                    ),
                ),
            }
        ]
        finalizer = ReviewApprovalFinalizer(http=run.http)
        finalizer.evidence_budget = run.caller.budget_view
        return finalizer, eligibility

    def rollover(self, fixture, run, directory):
        before = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
        config = run.caller.accounting.configuration
        fixture.trace = []
        owner_directory = Path(directory) / "457"
        owner_directory.mkdir()
        caller = ApiRunRequestTransport(
            directory=owner_directory,
            run=ApiRunIdentity(123, 457),
            plan=fixture.plan,
            attempt=1,
            opener=fixture._external_open,
            job="local",
        )
        fixture.accounted = caller
        http = GitHubHttp(api_url="https://api.github.test", opener=caller.open)
        native = GitHubIssueCommentSessionLedger(
            http, token="synthetic", app_slug="reviewsensei[bot]"
        )
        self.assertEqual(
            native.load(IDENTITY).record.record_sha256, before.record_sha256
        )
        caller.rollover_and_activate(
            native,
            IDENTITY,
            config,
            ApiExecutionOwner(1, "local", 0),
            before,
            lambda: None,
        )
        return caller, ApiRunSessionLedger(native, caller, IDENTITY), before

    def test_actual_confirmed_withholding_closes_then_next_run_keeps_exact_archive(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            fixture.lose_terminal_once = True
            finalizer, eligibility = self.prepare_finalizer(fixture, run)

            def complete(**_kwargs):
                publication = finalizer.finalize_recorded_decisions(
                    token=run.ledger.token,
                    check_token="synthetic-check",
                    repository=IDENTITY.repository,
                    pull_request=7,
                    head_sha=HEAD,
                    app_slug="reviewsensei[bot]",
                    eligibility=eligibility,
                )
                self.assertTrue(finalization_confirmed(publication))
                result = ReplyResult(
                    "replied",
                    comment_id=91,
                    approval_status=publication.status,
                    approval_diagnostic=publication.diagnostic,
                    resolution_status=publication.thread_resolution,
                )
                source = reply_source(
                    repository=IDENTITY.repository,
                    pull_request=7,
                    head_sha=HEAD,
                    kind="issue",
                    source_id=41,
                    updated_at=SOURCE["updated_at"],
                    body=BODY,
                )
                return replace(
                    result,
                    finalization_completion=reply_completion_from_finalization(
                        publication,
                        result,
                        source,
                        result_digest=eligibility.result_digest,
                    ),
                )

            self.assertEqual(fixture.invoke(run, args, complete), 0)
            self.assertEqual(fixture.action()["phase"], "terminal")
            self.assertEqual(fixture.terminal_patches, 1)
            caller, ledger, before = self.rollover(fixture, run, directory)
            archived = read_previous_run(
                ledger, IDENTITY, caller.accounting.previous_record
            )
            self.assertEqual(archived.to_dict(), before.to_dict())

    def test_actual_masked_error_stays_pending_and_prevents_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyFixture(directory)
            run, args = fixture.prepare()
            finalizer, eligibility = self.prepare_finalizer(fixture, run)

            def incomplete(**_kwargs):
                with patch.object(
                    finalizer,
                    "finalize",
                    side_effect=GitHubPublicationTransientError(
                        "unknown approval write"
                    ),
                ):
                    publication = finalizer.finalize_recorded_decisions(
                        token=run.ledger.token,
                        check_token="synthetic-check",
                        repository=IDENTITY.repository,
                        pull_request=7,
                        head_sha=HEAD,
                        app_slug="reviewsensei[bot]",
                        eligibility=eligibility,
                    )
                self.assertFalse(finalization_confirmed(publication))
                return ReplyResult(
                    "replied",
                    comment_id=91,
                    approval_status=publication.status,
                    approval_diagnostic=publication.diagnostic,
                    resolution_status=publication.thread_resolution,
                )

            with self.assertRaises(ReviewInputError):
                fixture.invoke(run, args, incomplete)
            self.assertEqual(fixture.action()["phase"], "pending")
            with self.assertRaises(ReviewInputError):
                self.rollover(fixture, run, directory)


if __name__ == "__main__":
    unittest.main()
