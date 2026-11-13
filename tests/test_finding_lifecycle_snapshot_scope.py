"""Retained snapshot proof uses each complete listing, never a stale part cache."""

import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.api_run_requests import ApiRunSessionLedger

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import (
    preflight_finding_lifecycle,
    stage_finding_lifecycle,
)
from review_sensei.session import read_session_dispositions
from tests import test_disposition_snapshots as snapshots
from tests import test_finding_lifecycle_parts as parts


class SnapshotScopeTests(unittest.TestCase):
    def fixture(self, directory):
        f, _, kwargs = parts.CurrentRunFindingPartsTests().fixture(directory)
        ledger = f.ledger
        ledger.enable_disposition_snapshot_writes = True
        root = snapshots.snapshot()
        document = root["state_manifest"]["document"]
        binding = dict(root["state_manifest"]["binding"])
        binding.update(
            repository_id=f.identity.repository_id,
            pull_request=f.identity.pull_request,
            producer=ledger.evidence_producer(),
        )

        def mutate(record):
            binding["generation"] = record.generation + 1
            manifest = ledger.stage_evidence(
                f.identity,
                binding=binding,
                document=document,
                item_count=1,
                max_manifest_bytes=800,
            )
            return record.evolve(
                generation=record.generation + 1,
                disposition_snapshot={**root, "state_manifest": manifest},
            )

        record = ledger.replace(f.identity, mutate)
        self.assertEqual(
            record.disposition_snapshot["state_manifest"]["encoding"],
            "partitioned-json-v1",
        )
        return f, record, kwargs, document["dispositions"]

    def test_actual_current_snapshot_checkpoint_is_bounded_and_cold_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs, decisions = self.fixture(directory)
            before = len(f.service.trace)
            preflight_finding_lifecycle(f.ledger, record, **kwargs)
            self.assertEqual(len(f.service.trace), before)
            result = stage_finding_lifecycle(f.ledger, record, **kwargs)
            self.assertLessEqual(len(f.service.trace) - before, 10)
            self.assertIsNone(f.ledger._plain_finding_snapshot_scope)
            self.assertEqual(
                list(read_session_dispositions(f.ledger, result)), decisions
            )
            f.assert_physical_accounting(self)

    def test_changed_or_missing_after_first_read_refuses_before_root_patch(self):
        for fault in ("missing", "changed", "foreign"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f, record, kwargs, _ = self.fixture(directory)
                ledger = f.ledger
                sid = record.disposition_snapshot["state_manifest"]["parts"][0][
                    "storage_id"
                ]
                initial_body = f.service.comment["body"]
                armed = []
                original_read = ApiRunSessionLedger._current_plain_snapshot_part
                original_open = f.caller._opener

                def read(current, identity, storage_id):
                    result = original_read(current, identity, storage_id)
                    if (
                        getattr(current, "_plain_finding_snapshot_scope", None)
                        is not None
                    ):
                        armed.append(True)
                    return result

                def external(request, timeout):
                    response = original_open(request, timeout)
                    if (
                        armed
                        and request.get_method() == "GET"
                        and urlsplit(request.full_url).path.endswith(
                            "/issues/7/comments"
                        )
                    ):
                        rows = json.loads(response.read(512 * 1024 + 1))
                        if fault == "missing":
                            rows = [r for r in rows if str(r.get("id")) != sid]
                        else:
                            row = next(r for r in rows if str(r.get("id")) == sid)
                            if fault == "foreign":
                                row["user"]["id"] += 1
                            else:
                                row["body"] = row["body"].replace(
                                    '"data":"', '"data":"A', 1
                                )
                        return json_response(rows)
                    return response

                f.caller._opener = external
                with patch.object(
                    ApiRunSessionLedger, "_current_plain_snapshot_part", read
                ):
                    with self.assertRaises(ReviewInputError):
                        stage_finding_lifecycle(ledger, record, **kwargs)
                self.assertTrue(armed)
                self.assertEqual(f.service.comment["body"], initial_body)
                self.assertIsNone(ledger._plain_finding_snapshot_scope)
                self.assertIsNone(
                    ledger._read_plain_finding_snapshot_part(f.identity, sid)
                )

    def test_maximum_future_manifest_rechecks_snapshot_before_any_write(self):
        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.session import disposition_snapshot_capacity

        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs, _ = self.fixture(directory)
            before = list(f.service.trace)
            root_body = f.service.comment["body"]
            observed = []

            def capacity(candidate):
                reference = candidate.finding_lifecycle
                if (
                    reference is not None
                    and reference["manifest"]["sha256"] == "f" * 64
                ):
                    observed.append(reference)
                    # Simulate a joint root envelope one byte below the retained
                    # snapshot, only at the maximum future manifest projection.
                    return len(canonical_bytes(candidate.disposition_snapshot)) - 1
                return disposition_snapshot_capacity(candidate)

            with patch("review_sensei.session.disposition_snapshot_capacity", capacity):
                with self.assertRaisesRegex(
                    ReviewInputError, "snapshot joint capacity"
                ):
                    preflight_finding_lifecycle(f.ledger, record, **kwargs)
            self.assertTrue(observed)
            self.assertEqual(f.service.trace, before)
            self.assertEqual(f.service.comment["body"], root_body)

    def test_replacement_root_wire_boundary_and_root_ownership(self):
        from review_sensei_github.session_ledger import plain_session_comment_json_bound

        from review_sensei.bounded_evidence import canonical_bytes

        with tempfile.TemporaryDirectory() as directory:
            f, record, _ = parts.CurrentRunFindingPartsTests().fixture(directory)
            ledger = f.ledger
            original = ledger._cached_comments()
            root_body = next(
                row["body"]
                for row in original
                if "reviewsensei:session:v" in row["body"]
            )
            rows = original + [
                {"id": 900000, "body": "", "user": {"id": 2, "type": "User"}}
            ]
            root_wire = plain_session_comment_json_bound(
                consumed_grants=set(ledger._observed_consumed_grants),
                repository_id=f.identity.repository_id,
                pull_request=f.identity.pull_request,
            )
            extra = 1000 + 512 + 4096
            base = (
                len(canonical_bytes(rows))
                - len(canonical_bytes(root_body))
                + root_wire
                + 4096
                + extra
            )
            rows[-1]["body"] = "x" * (512 * 1024 - 1 - base)
            ledger._replace_inventory(rows)
            before = len(f.service.trace)
            args = dict(added_parts=1, added_bytes=1000, checkpoint_parts=(1,))
            ledger.preflight_finding_lifecycle_retention(f.identity, **args)
            self.assertEqual(len(f.service.trace), before)
            rows[-1]["body"] += "x"
            ledger._replace_inventory(rows)
            # Crossing the old single-page envelope now chooses one fixed,
            # bounded multi-page plan; it does not raise the response ceiling.
            from review_sensei_github.api_run_requests import _PLAIN_FINDING_PLANS

            ledger.preflight_finding_lifecycle_retention(f.identity, **args)
            plan = _PLAIN_FINDING_PLANS[ledger]
            self.assertLess(plan.page_size, 100)
            self.assertLessEqual(len(plan.page_bounds), 10)
            self.assertTrue(all(bound < 512 * 1024 for bound in plan.page_bounds))
            self.assertEqual(len(f.service.trace), before)
            rows[-1]["body"] = "x" * (512 * 1024)
            ledger._replace_inventory(rows)
            with self.assertRaisesRegex(ReviewInputError, "page capacity"):
                ledger.preflight_finding_lifecycle_retention(f.identity, **args)
            self.assertEqual(len(f.service.trace), before)
            for defect in ("missing", "duplicate", "changed"):
                with self.subTest(defect=defect):
                    values = json.loads(json.dumps(original))
                    root_index = next(
                        i for i, row in enumerate(values) if row["body"] == root_body
                    )
                    if defect == "missing":
                        values.pop(root_index)
                    elif defect == "duplicate":
                        values.append({**values[root_index], "id": 999999})
                    else:
                        values[root_index]["body"] = "not the authenticated root"
                    ledger._replace_inventory(values)
                    with self.assertRaises(ReviewInputError):
                        ledger.preflight_finding_lifecycle_retention(f.identity, **args)
                    self.assertEqual(len(f.service.trace), before)

    def test_stale_listing_and_unselected_id_cannot_supply_part_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            f, record, kwargs, _ = self.fixture(directory)
            ledger = f.ledger
            before = len(f.service.trace)
            sid = record.disposition_snapshot["state_manifest"]["parts"][0][
                "storage_id"
            ]
            ledger._list_refresh = True
            with self.assertRaisesRegex(ReviewInputError, "stale"):
                preflight_finding_lifecycle(ledger, record, **kwargs)
            self.assertEqual(len(f.service.trace), before)
            ledger._list_refresh = False
            with self.assertRaisesRegex(ReviewInputError, "missing"):
                ledger._current_plain_snapshot_part(f.identity, "999999999")
            self.assertEqual(len(f.service.trace), before)
            # A copied scope marker cannot survive the required live checkpoint.
            from review_sensei.bounded_evidence import canonical_bytes

            ledger._plain_finding_snapshot_scope = (
                f.identity,
                record.record_sha256,
                canonical_bytes(record.disposition_snapshot),
                frozenset((sid,)),
            )
            ledger._plain_finding_scope_active = True
            with self.assertRaisesRegex(ReviewInputError, "scope changed"):
                ledger._read_plain_finding_snapshot_part(f.identity, sid)
            ledger._plain_finding_snapshot_scope = None
            ledger._plain_finding_scope_active = False
