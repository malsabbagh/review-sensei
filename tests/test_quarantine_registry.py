"""Published first-execution slots are immutable across candidate releases."""

import json
import unittest
from copy import deepcopy

from test_quarantine_activation import registration

from review_sensei.errors import ReviewInputError


def raw(rows):
    return json.dumps(
        {"schema_version": "quarantine-activation-registry-v1", "registrations": rows}
    ).encode()


class QuarantineRegistryTests(unittest.TestCase):
    def test_only_unchanged_prefix_and_new_independent_slots_are_accepted(self):
        from review_sensei_github.quarantine_activation import (
            require_registry_successor,
        )

        old = registration()
        new = {**old, "predecessor_comment_id": 42, "workflow_id": 701}
        self.assertEqual(require_registry_successor(raw([]), raw([old])), [old])
        self.assertEqual(
            require_registry_successor(raw([old]), raw([old, new])), [old, new]
        )
        for rows in (
            [],
            [new],
            [new, old],
            [old, old],
            [old, {**new, "workflow_id": old["workflow_id"]}],
        ):
            with self.subTest(rows=rows), self.assertRaises(ReviewInputError):
                require_registry_successor(raw([old]), raw(rows))
        for field in old:
            changed = deepcopy(old)
            changed[field] = 702 if type(old[field]) is int else "changed"
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                require_registry_successor(raw([old]), raw([changed]))
