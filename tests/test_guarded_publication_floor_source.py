"""Custom producer methods cannot inherit the original success-work floor."""

import unittest
from unittest.mock import patch

from review_sensei_github.checks import ReviewCheckPublisher
from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
from review_sensei_github.guarded_publication_plan import require_durable_floor
from review_sensei_github.publication import ReviewPublisher

from review_sensei.source_registry import require_instance_source


class GuardedFloorSourceTests(unittest.TestCase):
    def test_original_defining_module_seals_cover_all_producers(self):
        for cls, names in (
            (ReviewPublisher, ("_publish_check_state",)),
            (ReviewCheckPublisher, ("start", "complete", "_write")),
            (FindingLifecycleJournal, ("request", "finish")),
        ):
            require_instance_source(object.__new__(cls), methods=names)

    def test_class_substitutions_skip_floor_without_invoking_custom_method(self):
        for cls, name in (
            (ReviewCheckPublisher, "start"),
            (ReviewCheckPublisher, "complete"),
            (ReviewCheckPublisher, "_write"),
            (ReviewPublisher, "_publish_check_state"),
            (FindingLifecycleJournal, "finish"),
        ):
            with self.subTest(method=name):
                journal = object.__new__(FindingLifecycleJournal)
                journal.effects = []
                journal.reviewer = object.__new__(ReviewPublisher)
                journal.reviewer.checks = object.__new__(ReviewCheckPublisher)
                calls = []

                def custom(*args, **kwargs):
                    calls.append((args, kwargs))

                # No record, ledger or budget exists: reaching checkpoint pricing
                # would fail. The source exclusion must return without any I/O or
                # even invocation of the substitute producer.
                with patch.object(cls, name, custom):
                    self.assertIsNone(
                        require_durable_floor(journal, checks_enabled=True)
                    )
                self.assertEqual(calls, [])

    def test_transitive_instance_dispatch_shadows_do_not_reach_pricing(self):
        for name in ("_is_effect", "_dispatch_effect"):
            with self.subTest(method=name):
                journal = object.__new__(FindingLifecycleJournal)
                journal.effects = []
                journal.reviewer = object.__new__(ReviewPublisher)
                journal.reviewer.checks = object.__new__(ReviewCheckPublisher)
                calls = []
                setattr(journal, name, lambda *args, **kwargs: calls.append(1))
                # No record/ledger/budget: exclusion must precede all pricing.
                self.assertIsNone(require_durable_floor(journal, checks_enabled=True))
                self.assertEqual(calls, [])

    def test_original_unmodified_producer_reaches_record_validation(self):
        journal = object.__new__(FindingLifecycleJournal)
        journal.effects = []
        journal.reviewer = object.__new__(ReviewPublisher)
        journal.reviewer.checks = object.__new__(ReviewCheckPublisher)
        with self.assertRaisesRegex(AttributeError, "_record"):
            require_durable_floor(journal, checks_enabled=True)
