"""Empty-original artifact omission is an expiring source observation only."""

import unittest

from review_sensei_github.application import GitHubApplication
from review_sensei_github.empty_original_admission import (
    consume,
    install_application,
    install_restore,
)
from review_sensei_github.finding_lifecycle import restore_finding_lifecycle

from review_sensei.errors import ReviewInputError


class EmptyOriginalAdmissionTests(unittest.TestCase):
    def test_external_consumers_and_reinstallers_cannot_issue_receipts(self):
        self.assertFalse(consume(object(), object(), object(), object()))
        with self.assertRaisesRegex(ReviewInputError, "defining installer"):
            install_application(GitHubApplication)
        with self.assertRaisesRegex(ReviewInputError, "defining installer"):
            install_restore(restore_finding_lifecycle)

    def test_actual_historical_restore_and_own_cas_issue_only_within_invocation(self):
        import sys
        import tempfile
        from unittest.mock import patch

        from tests.test_quarantine_activation import registered_resource
        from tests.test_quarantine_published_tail_journey import (
            BROKER_QUALIFICATIONS,
            PublishedTailJourneyFixture,
        )

        class StopAfterObservation(BaseException):
            pass

        observed = []

        def observe(frame, event, value):
            if frame.f_code is consume.__code__ and event == "return":
                observed.append(value)
                # Receipt qualification is tested without pretending the
                # unfinished complete native-tail planner admits publication.
                raise StopAfterObservation()
            return observe

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            _old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            reviews = list(fixture.reviews)
            checks = list(fixture.check_runs)
            prior = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    with self.assertRaises(StopAfterObservation):
                        fixture.invoke()
            finally:
                sys.settrace(prior)
            self.assertEqual(observed, [True])
            self.assertEqual(fixture.reviews, reviews)
            # This is receipt custody and early-abort qualification, not a
            # complete-tail success claim.
            # The source fusion deferral now forbids a diagnostic check before
            # complete route admission and joint intent confirmation.
            self.assertEqual(fixture.check_runs, checks)
            self.assertFalse(consume(object(), object(), object(), object()))

    def test_actual_plain_isolation_retains_original_mark_without_deferral(self):
        import sys
        import tempfile
        from unittest.mock import patch

        from review_sensei.publication_intent_fusion import defer_current
        from tests.test_quarantine_activation import registered_resource
        from tests.test_quarantine_journey import JourneyFixture
        from tests.test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS

        observed = []

        def observe(frame, event, value):
            if frame.f_code is defer_current.__code__:
                if event == "return":
                    observed.append(value)
                return observe
            return None

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = JourneyFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            previous = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    code = fixture.invoke()
            finally:
                sys.settrace(previous)
            self.assertEqual(code, 0, fixture.error)
            self.assertTrue(observed)
            self.assertFalse(any(observed))

    def test_body_limit_prepublication_v1_admits_empty_original_while_literal_blocker_refuses(
        self,
    ):
        import sys
        import tempfile
        from unittest.mock import patch

        from test_quarantine_activation import registered_resource
        from test_quarantine_body_limit import BodyLimitJourneyFixture
        from test_quarantine_published_tail_journey import BROKER_QUALIFICATIONS

        class StopAfterConsume(BaseException):
            pass

        observed = []

        def observe(frame, event, value):
            if frame.f_code is consume.__code__ and event == "return":
                observed.append(value)
                raise StopAfterConsume()
            return observe

        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as directory2,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            f = BodyLimitJourneyFixture(directory)
            _, registration = f.original_body_failure()
            prior = sys.gettrace()
            try:
                # application.py seals empty_original_admission with require_module_seal(),
                # so monkey-patching consume is disallowed; trace hook matches above tests.
                sys.settrace(observe)
                with registered_resource([registration]):
                    with self.assertRaises(StopAfterConsume):
                        f.invoke()
            finally:
                sys.settrace(prior)
            self.assertEqual(observed, [True])

            # Mutating target_isolation contract away from body-limit-prepublication-v1
            # refuses the empty-original fast path.
            observed.clear()
            f2 = BodyLimitJourneyFixture(directory2)
            old2, reg2 = f2.original_body_failure()
            self.assertIsNone(old2.finding_lifecycle)
            # Tamper contract in prepublication metadata after registration creation ->
            # broker qualification digest check refuses before admission.
            reg2["prepublication"]["contract"] = (
                "literal-blocker-path-prepublication-v1"
            )
            with registered_resource([reg2]):
                self.assertEqual(f2.invoke(), 1)
            self.assertEqual(observed, [])
