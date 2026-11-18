"""Current run-owned lifecycle qualification using the real caller transport.

Synthetic remote HTTP is the only service substitution. The configured original
caller limit and source-owned successor policy are not enlarged by these tests.
"""

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_api_work_lifecycle as lifecycle
from review_sensei_github import GitHubWriteOptions, ReviewPublisher
from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.finding_lifecycle import restore_finding_lifecycle
from review_sensei_github.session_ledger import parse_session_comment
from test_activation_tail import CurrentHostedFixture
from test_api_work_persistence import IDENTITY

from review_sensei.api_work import ApiRunAccounting
from review_sensei.errors import ReviewInputError


class CurrentHostedFindingLifecycleTests(unittest.TestCase):
    def test_ordinary_current_producer_installs_root_before_native_review(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = CurrentHostedFixture(directory)
            roots_at_review = []
            original_open = fixture.service._external_open

            def observed(request, timeout):
                if request.method == "POST" and request.full_url.endswith(
                    "/pulls/7/reviews"
                ):
                    selected = parse_session_comment(
                        fixture.service.comment["body"], identity=IDENTITY
                    )
                    roots_at_review.append(selected.finding_lifecycle)
                return original_open(request, timeout)

            # The caller owns the opener captured at construction.
            fixture.caller._opener = observed

            def options(**kwargs):
                return GitHubWriteOptions(**kwargs, max_active_findings=1)

            with patch(
                "test_api_work_lifecycle.GitHubWriteOptions", side_effect=options
            ):
                service, publication, calls = lifecycle.CallerLifecycleTests().journey(
                    directory, prepared=(fixture.service, fixture.native), human=True
                )
            self.assertEqual(len(calls), 1)
            self.assertTrue(roots_at_review, publication)
            self.assertTrue(all(roots_at_review))
            fixture.assert_physical_accounting(self)

            selected = parse_session_comment(service.comment["body"], identity=IDENTITY)
            original_funds = ApiRunAccounting.from_document(selected.api_run_accounting)
            reference = selected.finding_lifecycle
            self.assertIsNotNone(reference)
            # A newly allocated publisher has no cached domain state. The
            # supported same-run successor must acquire its original reserved
            # work through authenticated ceased-owner evidence, not a new pot.
            effects_before = (
                len(service.reviews),
                len(service.inline),
                len(service.check_runs),
            )
            native = service.recover_lifecycle()
            ledger = ApiRunSessionLedger(native, service.accounted, IDENTITY)
            fresh = ReviewPublisher(http=service.http)
            restored_record, document = restore_finding_lifecycle(
                fresh, ledger, IDENTITY
            )
            self.assertEqual(restored_record.finding_lifecycle, reference)
            self.assertIsNotNone(document)
            self.assertIn(
                (IDENTITY.repository_id, IDENTITY.pull_request),
                fresh._active_window_states,
            )
            self.assertEqual(
                effects_before,
                (len(service.reviews), len(service.inline), len(service.check_runs)),
            )
            self.assertEqual(len(calls), 1)
            state = fresh._active_window_states[
                (IDENTITY.repository_id, IDENTITY.pull_request)
            ]
            obligations = state.authoritative_decision_inventory.obligations
            self.assertEqual(len(obligations), 1)
            self.assertTrue(obligations[0].comment.needs_human)
            successor = service.accounted.accounting
            self.assertEqual(successor.configuration, original_funds.configuration)
            self.assertEqual(successor.identity, original_funds.identity)
            self.assertEqual(successor.blocks[:2], original_funds.blocks[:2])
            self.assertEqual(successor.remaining, 0)
            self.assertEqual(successor.owner.run_attempt, 2)
            self.assertEqual(
                successor.configuration.limit,
                successor.startup.liability
                + successor.prior_nonstartup_liability
                + successor.confirmed
                + successor.uncertain
                + successor.reserved_undispatched
                + successor.remaining,
            )
            # Fresh *distinct-run* takeover is not authorized by this policy.
            # The run binding must fail before any caller/native dispatch.
            before = len(service.trace)
            with self.assertRaisesRegex(
                ReviewInputError, "run or frozen configuration conflicts"
            ):
                successor.require_binding(
                    replace(successor.identity, run_id=successor.identity.run_id + 1),
                    successor.configuration,
                )
            self.assertEqual(len(service.trace), before)

    def test_unchanged_small_original_allowance_refuses_before_native_effects(self):
        from review_sensei.errors import EvidenceBudgetExhausted

        for human in (False, True):
            with self.subTest(human=human), tempfile.TemporaryDirectory() as directory:
                fixture = CurrentHostedFixture(
                    directory, limit=lifecycle.QUALIFICATION_LIMIT
                )
                with patch(
                    "test_api_work_lifecycle.GitHubWriteOptions",
                    side_effect=lambda **kw: GitHubWriteOptions(
                        **kw, max_active_findings=1
                    ),
                ):
                    with self.assertRaises(EvidenceBudgetExhausted):
                        lifecycle.CallerLifecycleTests().journey(
                            directory,
                            prepared=(fixture.service, fixture.native),
                            human=human,
                        )
                self.assertEqual(fixture.service.reviews, [])
                self.assertEqual(fixture.service.check_runs, [])
                self.assertEqual(
                    fixture.caller.accounting.configuration.limit,
                    lifecycle.QUALIFICATION_LIMIT,
                )
                fixture.assert_physical_accounting(self)


def _process_worker(directory, phase):
    """Only simulated remote-service JSON and normal runtime files cross here."""
    import json
    from pathlib import Path

    root = Path(directory)
    service_file = root / "remote-service.json"
    if phase == "produce":
        current = CurrentHostedFixture(directory)
        service = current.service
        roots = []
        original = current.caller._opener

        def observed(request, timeout):
            if request.method == "POST" and request.full_url.endswith(
                "/pulls/7/reviews"
            ):
                roots.append(
                    parse_session_comment(
                        service.comment["body"], identity=IDENTITY
                    ).finding_lifecycle
                )
            return original(request, timeout)

        current.caller._opener = observed
        with patch(
            "test_api_work_lifecycle.GitHubWriteOptions",
            side_effect=lambda **kw: GitHubWriteOptions(**kw, max_active_findings=1),
        ):
            _, _, calls = lifecycle.CallerLifecycleTests().journey(
                directory, prepared=(service, current.native), human=True
            )
        assert len(calls) == 1 and roots and all(roots)
        current.assert_physical_accounting(unittest.TestCase())
        service_file.write_text(
            json.dumps(
                {
                    name: getattr(service, name)
                    for name in (
                        "comment",
                        "extra_comments",
                        "reviews",
                        "inline",
                        "check_runs",
                        "trace",
                    )
                }
            )
        )
        return {"provider_calls": len(calls), "root_before_effect": True}
    service = lifecycle.LifecycleFixture(directory, limit=512)
    remote = json.loads(service_file.read_text())
    for name, value in remote.items():
        if name == "extra_comments":
            value = {int(key): item for key, item in value.items()}
        setattr(service, name, value)
    previous = parse_session_comment(service.comment["body"], identity=IDENTITY)
    original = ApiRunAccounting.from_document(previous.api_run_accounting)
    native = service.recover_lifecycle()
    ledger = ApiRunSessionLedger(native, service.accounted, IDENTITY)
    publisher = ReviewPublisher(http=service.http)
    record, document = restore_finding_lifecycle(publisher, ledger, IDENTITY)
    assert record.finding_lifecycle == previous.finding_lifecycle
    assert document is not None
    state = publisher._active_window_states[
        (IDENTITY.repository_id, IDENTITY.pull_request)
    ]
    assert len(state.authoritative_decision_inventory.obligations) == 1
    assert state.authoritative_decision_inventory.obligations[0].comment.needs_human
    now = service.accounted.accounting
    assert (
        now.configuration == original.configuration
        and now.identity == original.identity
    )
    assert now.blocks[:2] == original.blocks[:2] and now.remaining == 0
    assert (
        now.configuration.limit
        == now.startup.liability
        + now.prior_nonstartup_liability
        + now.confirmed
        + now.uncertain
        + now.reserved_undispatched
        + now.remaining
    )
    assert (
        service.reviews == remote["reviews"]
        and service.inline == remote["inline"]
        and service.check_runs == remote["check_runs"]
    )
    return {
        "provider_calls": 0,
        "restored": True,
        "owner_attempt": now.owner.run_attempt,
        "limit": now.configuration.limit,
    }


class IndependentCurrentHostedProcessTests(unittest.TestCase):
    def test_current_original_funds_survive_interpreter_restart(self):
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            results = []
            for phase in ("produce", "restore"):
                code = "import json,sys; from tests.test_finding_lifecycle_current_hosted import _process_worker; print(json.dumps(_process_worker(sys.argv[1],sys.argv[2])))"
                env = dict(os.environ)
                env["PYTHONPATH"] = os.pathsep.join(
                    str(Path(item).resolve())
                    for item in (
                        "src",
                        "packages/python/review-sensei-github/src",
                        "tests",
                        ".",
                    )
                )
                completed = subprocess.run(
                    [sys.executable, "-c", code, directory, phase],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=90,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                results.append(json.loads(completed.stdout.strip().splitlines()[-1]))
            self.assertTrue(results[0]["root_before_effect"])
            self.assertEqual(sum(item["provider_calls"] for item in results), 1)
            self.assertEqual(
                results[1],
                {
                    "provider_calls": 0,
                    "restored": True,
                    "owner_attempt": 2,
                    "limit": 512,
                },
            )


class ProductionDefaultReviewRunTests(unittest.TestCase):
    def test_actual_execute_selects_durable_default_without_option_patch(self):
        import json

        from review_sensei_github.finding_lifecycle import _unpack_journal
        from test_review_run import EntryFixture

        from review_sensei.finding_lifecycle_store import read_finding_lifecycle

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            roots = []
            original_open = fixture._external_open

            def observed(request, timeout):
                if request.method == "POST" and request.full_url.endswith(
                    "/pulls/7/reviews"
                ):
                    roots.append(
                        parse_session_comment(
                            fixture.comment["body"], identity=IDENTITY
                        ).finding_lifecycle
                    )
                return original_open(request, timeout)

            fixture._external_open = observed
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            selected = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(selected.finding_lifecycle)
            self.assertTrue(roots and all(roots))
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(fixture.accounted.accounting.configuration.limit, 512)
            self.assertEqual(
                json.loads((fixture.root / "publication.json").read_text())["status"],
                "published",
            )
            # Read the selected carrier through the actual run-owned ledger,
            # not an injected state or replaced publication options object.
            ledger = fixture.selected.ledger
            document = _unpack_journal(read_finding_lifecycle(ledger, selected))
            from review_sensei.finding_drift import DriftEvaluationResult
            from review_sensei.finding_lifecycle import decode_lifecycle_state

            self.assertIsInstance(
                decode_lifecycle_state(document["lifecycle"]).state,
                DriftEvaluationResult,
            )
