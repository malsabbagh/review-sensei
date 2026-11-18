"""Closed recipe arithmetic only; no historical observation or lease issuance."""

import unittest

from review_sensei_github.quarantine_command_finalization import (
    _default_c5_prefix_headroom,
    _intermediate_stages,
)

from review_sensei.errors import ReviewInputError


class HistoricalReceiptRecipeTests(unittest.TestCase):
    def test_existing_closed_recipe_costs_remain_unchanged(self):
        for comments, trust in ((4, 7), (5, 0)):
            stages = _intermediate_stages(comments, 3, 1, trust)
            self.assertEqual(sum(count for _, count in stages), 98)
            self.assertEqual(sum(count for _, count in stages) + 3 + 4, 105)
        rows = _default_c5_prefix_headroom(stages, terminal_comments=5)
        receipt = next(row for row in rows if row["phase"] == "receipt")
        self.assertEqual(receipt, {"phase": "receipt", "available": 8, "minimum": 8})

    def test_two_page_success_and_loss_bounds_keep_original_reservation(self):
        stages = _intermediate_stages(5, 2, 2, 0)
        self.assertEqual(sum(count for _, count in stages), 96)
        self.assertEqual(sum(count for _, count in stages) + 4 + 4, 104)
        rows = _default_c5_prefix_headroom(
            stages, terminal_comments=10, receipt_reads=2
        )
        self.assertTrue(all(row["available"] >= row["minimum"] for row in rows))
        receipt = next(row for row in rows if row["phase"] == "receipt")
        self.assertEqual(receipt, {"phase": "receipt", "available": 11, "minimum": 10})
        pending = next(row for row in rows if row["phase"] == "pending-journal")
        self.assertEqual(pending["minimum"], 36)

    def test_noncompacted_put_loss_keeps_independent_finish_read(self):
        stages = _intermediate_stages(5, 2, 2, 0)
        names = [name for name, _ in stages]
        through_put = sum(
            count for _, count in stages[: names.index("review-update") + 1]
        )
        self.assertEqual(through_put, 66)
        # Existing source returns evaluation_failed before receipt. This DATA
        # test retains the independent pending PUT GET and finish checkpoint;
        # it does not fabricate that original-frame source exclusion.
        self.assertEqual(through_put + 1 + dict(stages)["finish-journal"] + 4, 84)
        self.assertLess(84, 105)

    def test_unsupported_read_profiles_and_changed_stages_refuse(self):
        for values in (
            (5, 2, 3, 0),
            (5, 3, 2, 0),
            (4, 2, 2, 7),
            (5, 2, 2, 1),
            (True, 3, 1, 7),
        ):
            with self.subTest(values=values), self.assertRaises(ReviewInputError):
                _intermediate_stages(*values)
        stages = _intermediate_stages(5, 2, 2, 0)
        with self.assertRaises(ReviewInputError):
            _default_c5_prefix_headroom(stages, terminal_comments=11, receipt_reads=2)
        changed = tuple(
            (name, count + int(name == "receipt")) for name, count in stages
        )
        with self.assertRaises(ReviewInputError):
            _default_c5_prefix_headroom(changed, terminal_comments=5, receipt_reads=2)


if __name__ == "__main__":
    unittest.main()
