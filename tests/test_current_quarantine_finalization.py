"""Current-hold reader preparations must have source custody, not copied fields."""

import copy
import unittest

from review_sensei_github.current_quarantine_finalization import (
    CurrentPreparation,
    LifecycleSizingContext,
    TerminalPreview,
    capture_completed_preview,
    capture_lifecycle_extension,
    capture_terminal_preview,
    is_current_plan,
    lifecycle_sizing_context,
    require,
    require_execution_grant,
    require_plan,
    require_terminal_preview,
    transfer,
    validate_lifecycle_sizing_context,
)

from review_sensei.errors import ReviewInputError


class CurrentQuarantineFinalizationTests(unittest.TestCase):
    def test_lifetime_proposal_capture_requires_original_return_frame(self):
        with self.assertRaisesRegex(ReviewInputError, "not source-issued"):
            capture_lifecycle_extension(None, None, None)

    def test_completed_branch_capture_requires_original_preflight_frame(self):
        with self.assertRaisesRegex(ReviewInputError, "not original"):
            capture_completed_preview(
                None,
                None,
                preview_record=None,
                details=None,
                alternatives=(),
                item_count=0,
                quote={},
            )

    def test_constructed_and_copied_preparations_are_not_issued(self):
        preparation = CurrentPreparation()
        for value in (preparation, copy.copy(preparation), copy.deepcopy(preparation)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ReviewInputError, "unissued"):
                    require(value)

    def test_preparation_is_not_a_physical_plan(self):
        preparation = CurrentPreparation()
        self.assertFalse(is_current_plan(preparation))
        with self.assertRaisesRegex(ReviewInputError, "unissued or changed"):
            require_plan(preparation, None)

    def test_direct_transfer_cannot_supply_execution_custody(self):
        with self.assertRaisesRegex(ReviewInputError, "unissued or consumed"):
            transfer(CurrentPreparation(), None, None)

    def test_sizing_context_is_unavailable_outside_original_preflight(self):
        self.assertIsNone(lifecycle_sizing_context(None, None, {}, 0))

    def test_constructed_copied_sizing_tokens_cannot_validate_or_execute(self):
        token = LifecycleSizingContext()
        for value in (token, copy.copy(token), copy.deepcopy(token)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ReviewInputError, "unissued"):
                    validate_lifecycle_sizing_context(value, None, None, {}, 0)
                with self.assertRaisesRegex(ReviewInputError, "source-issued plan"):
                    require_execution_grant(value)

    def test_terminal_report_cannot_be_captured_by_an_arbitrary_caller(self):
        with self.assertRaisesRegex(ReviewInputError, "not source-issued"):
            capture_terminal_preview(None, None, None, None, None)

    def test_copied_or_constructed_terminal_preview_cannot_grant_capacity(self):
        preview = TerminalPreview()
        for value in (preview, copy.copy(preview), copy.deepcopy(preview)):
            with self.assertRaisesRegex(ReviewInputError, "unissued or changed"):
                require_terminal_preview(value, None, None, None, None)

    def test_forged_journal_wrapper_metadata_cannot_select_a_sizing_frame(self):
        from review_sensei_github.finding_lifecycle import FindingLifecycleJournal

        original = FindingLifecycleJournal.preflight_command_finalization
        inner = original.__wrapped__
        try:
            original.__wrapped__ = lambda *args, **kwargs: None
            with self.assertRaisesRegex(ReviewInputError, "original.*source changed"):
                lifecycle_sizing_context(None, None, {}, 0)
        finally:
            original.__wrapped__ = inner
        self.assertIsNone(lifecycle_sizing_context(None, None, {}, 0))
