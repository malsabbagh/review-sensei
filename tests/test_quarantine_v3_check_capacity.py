"""Current-head single-page admission includes immutable old uncertainty."""

import copy
import importlib.metadata
import json
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import urlsplit

import pypi_attestations
from review_sensei_github import quarantine_published_tail as tail
from review_sensei_github.checks import MAX_CHECK_PAGE_SIZE
from review_sensei_github.publication_recovery import _publication_transport
from review_sensei_github.quarantine_capacity import NativeCapacityGuard
from test_quarantine_activation import IDENTITY, registered_resource
from test_quarantine_capacity import no_reserve
from test_quarantine_v3_journey import FixtureProvenance, SameHeadV3JourneyFixture

from review_sensei.errors import ReviewInputError


@contextmanager
def synthetic_services():
    original_version = importlib.metadata.version
    versions = {"review-sensei": "0.8.17", "review-sensei-github": "0.3.23"}
    with (
        patch.dict(tail.BROKER_QUALIFICATIONS, {}, clear=True),
        patch.dict(tail.FAILED_PUBLISHED_EXECUTORS, {}, clear=True),
        patch.object(pypi_attestations, "Provenance", FixtureProvenance),
        patch.object(
            importlib.metadata,
            "version",
            side_effect=lambda name: (
                versions[name] if name in versions else original_version(name)
            ),
        ),
        patch.object(
            importlib.metadata, "requires", return_value=["review-sensei==0.8.17"]
        ),
    ):
        yield


class DenseSameHeadFixture(SameHeadV3JourneyFixture):
    def external(self, request, timeout):
        response = super().external(request, timeout)
        if (
            hasattr(self, "publication_source")
            and request.method == "POST"
            and urlsplit(request.full_url).path.endswith("/check-runs")
            and not getattr(self, "delayed_checks", False)
        ):
            self.delayed_checks = True
            original = self.old_checks[-1]
            self.check_runs.extend(
                {**copy.deepcopy(original), "id": 9900000 + i, "conclusion": "failure"}
                for i in range(2)
            )
            # A bounded positive old PATCH changes prose without refunding any
            # original allocation; its positive-growth liability stays reserved.
            next(row for row in self.check_runs if row["id"] == original["id"])[
                "output"
            ]["summary"] += "\u6f22" * 100
        return response


class GenericOldSameHeadFixture(SameHeadV3JourneyFixture):
    same_head_check_exposure = None


def check_rows(count):
    return [
        {
            "id": i + 1,
            "head_sha": "a" * 40,
            "name": "ReviewSensei",
            "external_id": "legacy",
            "app": {"slug": "reviewsensei"},
            "status": "completed",
            "conclusion": "failure",
        }
        for i in range(count)
    ]


class V3CheckCapacityTests(unittest.TestCase):
    def test_only_a_distinct_authenticated_head_projection_excludes_old_checks(self):
        reserve = no_reserve()
        reserve["checks"].update(
            new_objects=3, new_wire_bytes=3000, max_object_wire_bytes=1000
        )
        guard = NativeCapacityGuard(
            identity=IDENTITY,
            reserve=reserve,
            cursor=True,
            closure_wire=8192,
            old_check_head="b" * 40,
            current_check_head="a" * 40,
        )
        guard.seed("checks", check_rows(98))
        self.assertEqual(guard.certificates["checks"]["count_upper_bound"], 100)
        self.assertTrue(guard.certificates["checks"]["old_check_head_excluded"])
        self.assertEqual(guard.reserve, reserve)
        for previous, current in ((None, "a" * 40), ("a" * 40, "a" * 40)):
            with self.subTest(previous=previous):
                guard = NativeCapacityGuard(
                    identity=IDENTITY,
                    reserve=reserve,
                    cursor=True,
                    closure_wire=8192,
                    old_check_head=previous,
                    current_check_head=current,
                )
                with self.assertRaises(ReviewInputError):
                    guard.seed("checks", check_rows(98))

    def test_legacy_large_old_envelope_refuses_same_head_before_any_new_effect(self):
        with tempfile.TemporaryDirectory() as directory, synthetic_services():
            fixture = GenericOldSameHeadFixture(directory)
            old, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            before = fixture.comment["body"], fixture.provider_calls
            with registered_resource([registration]):
                self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assertIn("current-check transaction capacity", fixture.error)
            self.assertEqual(fixture.root_posts, 0)
            self.assertFalse(
                any(
                    method in {"POST", "PATCH", "PUT"} and path.startswith("/repos/")
                    for _, method, path in fixture.trace
                )
            )
            self.assertEqual((fixture.comment["body"], fixture.provider_calls), before)
            self.assertIsNone(old.api_run_accounting["terminal_transaction"])

    def test_maximum_passing_current_population_reaches_v3_finalizer_after_old_post_and_patch(
        self,
    ):
        from review_sensei_github.quarantine_capacity import _PAGE_BYTES

        def wire(row):
            return len(
                json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode()
            )

        with tempfile.TemporaryDirectory() as directory, synthetic_services():
            control = DenseSameHeadFixture(directory)
            _, registration = control.original_origins(
                failed_count=1, published_count=1
            )
            original_checks = copy.deepcopy(
                [
                    row
                    for row in control.check_runs
                    if row["head_sha"] == control.live_head
                ]
            )
            with registered_resource([registration]):
                self.assertEqual(
                    control.invoke(), 0, control.error or repr(control.exception)
                )
            guard = _publication_transport(
                control.selected.ledger.http
            )._quarantine_capacity_guard
            closure = guard.closure_wire
            old = copy.deepcopy(guard.reserve["checks"])
            template = {
                **check_rows(1)[0],
                "id": 100000,
                "app": {"slug": "unrelated-app"},
            }
            initial = sum(wire(row) + 4096 for row in original_checks)
            future = (
                old["new_wire_bytes"]
                + old["positive_update_bytes"]
                + old["new_objects"] * 4096
                + 4 * closure
                + 2 * 4096
            )
            maximum = (_PAGE_BYTES - initial - future) // (wire(template) + 4096)
            self.assertGreater(maximum, 0)
            self.boundary_measurement = {
                "maximum_extra_current_checks": maximum,
                "current_initial_checks": len(original_checks),
                "old_uncertain_posts": old["new_objects"],
                "old_uncertain_positive_bytes": old["positive_update_bytes"],
                "successor_check_allocations": 2,
                "closure_wire": closure,
                "maximum_admitted_encoded_bytes": initial
                + future
                + maximum * (wire(template) + 4096),
                "one_more_encoded_bytes": initial
                + future
                + (maximum + 1) * (wire(template) + 4096),
                "page_data_bytes": _PAGE_BYTES,
            }

        for count, accepted in ((maximum + 1, False), (maximum, True)):
            with (
                self.subTest(count=count),
                tempfile.TemporaryDirectory() as directory,
                synthetic_services(),
            ):
                fixture = DenseSameHeadFixture(directory)
                _, registration = fixture.original_origins(
                    failed_count=1, published_count=1
                )
                fixture.check_runs.extend(
                    {
                        **copy.deepcopy(template),
                        "id": 100000 + i,
                        "head_sha": fixture.live_head,
                    }
                    for i in range(count)
                )
                before = fixture.comment["body"], fixture.provider_calls
                with registered_resource([registration]):
                    self.assertEqual(
                        fixture.invoke(),
                        0 if accepted else 1,
                        fixture.error or repr(fixture.exception),
                    )
                if accepted:
                    actual = _publication_transport(
                        fixture.selected.ledger.http
                    )._quarantine_capacity_guard
                    self.assertEqual(actual.closure_wire, closure)
                    self.assertEqual(actual.reserve["checks"], old)
                    self.assertTrue(fixture.delayed_checks)
                    scoped = [
                        row
                        for row in fixture.check_runs
                        if ":quarantine:" in row.get("external_id", "")
                    ]
                    self.assertEqual(len(scoped), 1)
                    self.assertEqual(scoped[0]["status"], "completed")
                    self.assertEqual(
                        len(fixture.accounted.observations), len(fixture.trace)
                    )
                    self.assertLessEqual(len(fixture.check_runs), MAX_CHECK_PAGE_SIZE)
                else:
                    self.assertIn("current-check transaction capacity", fixture.error)
                    self.assertEqual(fixture.root_posts, 0)
                    self.assertEqual(
                        (fixture.comment["body"], fixture.provider_calls), before
                    )
                    self.assertFalse(
                        any(
                            method in {"POST", "PATCH", "PUT"}
                            and path.startswith("/repos/")
                            for _, method, path in fixture.trace
                        )
                    )

    def test_old_delayed_posts_are_included_at_the_exact_count_boundary(self):
        reserve = no_reserve()
        reserve["checks"].update(
            new_objects=3, new_wire_bytes=3000, max_object_wire_bytes=1000
        )
        for current, accepted in ((95, True), (96, False), (98, False)):
            with self.subTest(current=current):
                guard = NativeCapacityGuard(
                    identity=IDENTITY, reserve=reserve, cursor=True, closure_wire=8192
                )
                if accepted:
                    guard.seed("checks", check_rows(current))
                    self.assertEqual(
                        guard.certificates["checks"]["count_upper_bound"],
                        MAX_CHECK_PAGE_SIZE,
                    )
                else:
                    with self.assertRaisesRegex(
                        ReviewInputError, "current-check transaction capacity"
                    ):
                        guard.seed("checks", check_rows(current))
                self.assertEqual(guard.reserve, reserve)

    def test_old_positive_patch_growth_is_included_at_the_byte_boundary(self):
        from review_sensei_github.quarantine_capacity import _PAGE_BYTES

        seed = check_rows(1)
        base = NativeCapacityGuard(
            identity=IDENTITY, reserve=no_reserve(), cursor=True, closure_wire=8192
        )
        base.seed("checks", seed)
        room = _PAGE_BYTES - (
            base.stats["checks"]["bytes"] + 2 * (8192 + 4096) + 2 * 8192
        )
        for growth, accepted in ((room, True), (room + 1, False)):
            with self.subTest(growth=growth):
                reserve = no_reserve()
                reserve["checks"].update(
                    positive_update_bytes=growth, max_object_wire_bytes=8192
                )
                guard = NativeCapacityGuard(
                    identity=IDENTITY, reserve=reserve, cursor=True, closure_wire=8192
                )
                if accepted:
                    guard.seed("checks", seed)
                    self.assertEqual(
                        guard.certificates["checks"]["encoded_bytes_upper_bound"],
                        _PAGE_BYTES,
                    )
                else:
                    with self.assertRaisesRegex(
                        ReviewInputError, "current-check transaction capacity"
                    ):
                        guard.seed("checks", seed)
                self.assertEqual(guard.reserve, reserve)
