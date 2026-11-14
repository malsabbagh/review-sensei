"""Public data-seam checks; none of these fixtures carry runtime authority."""

import unittest
from dataclasses import replace

from review_sensei.command_pairing_data import (
    PairingDebit,
    PairingEdge,
    PairingInput,
    validate_pairing_data,
)
from review_sensei.errors import ReviewInputError


def debit(name, sequence, state="pending", kind="root-write", occurrence="one"):
    return PairingDebit(
        name,
        sequence,
        999,
        "owner",
        "ordinary",
        state,
        kind,
        occurrence,
        "target",
        "wire-digest",
    )


def sample():
    old = (debit("old-pending", 1), debit("old-confirmed", 2, "confirmed"))
    unknown = debit("unknown", 4)
    anchor = debit("anchor", 5, "confirmed", "confirmation-read")
    terminal = (replace(debit("terminal-old", 3), block="terminal"),)
    return PairingInput(
        old,
        old + (unknown, anchor),
        terminal,
        terminal,
        (PairingEdge("checkpoint", "unknown", "anchor", "one", "target"),),
        3,
        "owner",
        "ordinary",
        161,
        17,
    )


class CommandPairingDataTests(unittest.TestCase):
    def reject(self, value):
        with self.assertRaises(ReviewInputError):
            validate_pairing_data(value)

    def test_preserves_old_debt_and_terminal_fields(self):
        value = sample()
        report = validate_pairing_data(value)
        self.assertEqual(
            (report.initial_pending, report.new_pending, report.new_confirmed),
            (1, 1, 1),
        )
        self.assertEqual(
            (report.conditional_pending_bound, report.terminal_pending_bound), (120, 17)
        )
        for field in ("deadline", "owner", "block", "fingerprint", "state"):
            old = value.final_ordinary[0]
            changed = replace(
                old, **{field: 1000 if field == "deadline" else "changed"}
            )
            self.reject(
                replace(value, final_ordinary=(changed,) + value.final_ordinary[1:])
            )
        self.reject(replace(value, final_terminal=()))
        self.reject(replace(value, final_ordinary=value.final_ordinary[1:]))

    def test_missing_extra_foreign_initial_and_cached_anchors(self):
        value = sample()
        edge = value.edges[0]
        for changes in (
            {"anchor_id": "missing"},
            {"anchor_id": "old-confirmed"},
            {"unknown_id": "old-pending"},
            {"target": "foreign"},
            {"occurrence": "cached"},
            {"category": "other"},
        ):
            self.reject(replace(value, edges=(replace(edge, **changes),)))
        self.reject(replace(value, edges=()))
        self.reject(replace(value, edges=value.edges * 2))
        for changes in (
            {"owner": "foreign"},
            {"block": "terminal"},
            {"sequence": 0},
            {"sequence": 3},
            {"state": "pending"},
            {"occurrence": "cached"},
            {"target": "other"},
        ):
            anchor = replace(value.final_ordinary[-1], **changes)
            self.reject(
                replace(value, final_ordinary=value.final_ordinary[:-1] + (anchor,))
            )

    def test_read_loss_and_unclassified_pending_refuse(self):
        value = sample()
        unknown = replace(value.final_ordinary[-2], kind="ordinary-read")
        self.reject(
            replace(
                value,
                final_ordinary=value.final_ordinary[:-2]
                + (unknown, value.final_ordinary[-1]),
            )
        )
        self.reject(
            replace(value, final_ordinary=value.final_ordinary + (debit("extra", 6),))
        )
        self.reject(replace(value, ordinary_bound=1))
        self.reject(replace(value, ordinary_bound=True))
        self.reject(
            replace(
                value, final_ordinary=value.final_ordinary + (value.final_ordinary[-1],)
            )
        )

    def test_anchor_reuse_within_class_refuses(self):
        value = sample()
        second = debit("second", 6)
        anchor = replace(value.final_ordinary[-1], sequence=7)
        self.reject(
            replace(
                value,
                final_ordinary=value.final_ordinary[:-1] + (second, anchor),
                edges=value.edges + (replace(value.edges[0], unknown_id="second"),),
            )
        )

    def test_exhaustive_small_three_class_mixtures(self):
        # Cross-class reuse is deliberately allowed; no cross-class disjointness
        # premise is smuggled into the conservative U <= 3C data inequality.
        kinds = (
            ("checkpoint", "root-read"),
            ("native", "native-write"),
            ("receipt", "receipt-write"),
        )
        for count in range(9):
            for mask in range(8):
                values, edges = [], []
                for index in range(count):
                    occurrence = str(index)
                    anchor_id = f"a{index}"
                    for offset, (category, kind) in enumerate(kinds):
                        if mask & (1 << offset):
                            name = f"u{index}-{offset}"
                            values.append(
                                debit(
                                    name,
                                    index * 4 + offset + 1,
                                    kind=kind,
                                    occurrence=occurrence,
                                )
                            )
                            edges.append(
                                PairingEdge(
                                    category, name, anchor_id, occurrence, "target"
                                )
                            )
                    values.append(
                        debit(
                            anchor_id,
                            index * 4 + 4,
                            "confirmed",
                            "confirmation-read",
                            occurrence,
                        )
                    )
                value = PairingInput(
                    (),
                    tuple(values),
                    (),
                    (),
                    tuple(edges),
                    0,
                    "owner",
                    "ordinary",
                    len(values),
                    17,
                )
                report = validate_pairing_data(value)
                self.assertLessEqual(report.new_pending, 3 * report.new_confirmed)
                self.assertLessEqual(
                    report.new_pending, report.conditional_pending_bound
                )

    def test_fabricated_consistent_data_is_not_authority(self):
        # This succeeds even though no HTTP request happened: the seam is plain
        # data validation, explicitly not a transport receipt verifier or grant.
        self.assertEqual(validate_pairing_data(sample()).new_pending, 1)


if __name__ == "__main__":
    unittest.main()
