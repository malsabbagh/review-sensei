"""Pure restoration preserves both concrete lifecycle inventory types."""

import unittest
from dataclasses import replace

from review_sensei_github.lifecycle_admission import (
    restore_original_lifecycle_admission,
)

from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_codec as fixture


class LifecycleAdmissionTypesTests(unittest.TestCase):
    def test_both_inventory_types_restore_only_retained_runtime_flags(self):
        original = fixture.result(fixture.comment())
        public = replace(
            original,
            comments=tuple(
                replace(comment, effective_blocking=None, needs_human=False)
                for comment in original.comments
            ),
        )
        for evaluate in (
            fixture.evaluate_active_window_round,
            fixture.evaluate_drift_round,
        ):
            state = evaluate(
                original,
                head_sha=fixture.HEAD,
                base_sha=fixture.BASE,
                max_active_findings=1,
            )
            with self.subTest(state=evaluate.__name__):
                restored = restore_original_lifecycle_admission(
                    public, state, head_sha=fixture.HEAD, base_sha=fixture.BASE
                )
                self.assertEqual(restored.to_dict(), public.to_dict())
                self.assertTrue(all(c.effective_blocking for c in restored.comments))
                self.assertTrue(all(c.needs_human for c in restored.comments))
                changed = replace(
                    public,
                    comments=(
                        replace(public.comments[0], body="Changed criterion."),
                        *public.comments[1:],
                    ),
                )
                with self.assertRaises(ReviewInputError):
                    restore_original_lifecycle_admission(
                        changed,
                        state,
                        head_sha=fixture.HEAD,
                        base_sha=fixture.BASE,
                    )
