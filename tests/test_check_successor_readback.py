"""Exact cold check-update readback can settle a superseded creation, not guess."""

import copy
import unittest
from unittest.mock import patch

from test_finding_lifecycle_journal import HEAD, FakeHTTP, document, effect, journal

PATH = "/repos/owner/repo/check-runs"
CREATE = {
    "name": "ReviewSensei",
    "head_sha": HEAD,
    "external_id": "reviewsensei-review-v1",
    "status": "in_progress",
    "output": {"title": "Review in progress", "summary": "Current analysis."},
}
UPDATE = {
    "status": "completed",
    "conclusion": "action_required",
    "output": {"title": "Review incomplete", "summary": "Retained recovery required."},
}


def fixture():
    created = effect(
        CREATE,
        path=PATH,
        status="confirmed",
        response=[
            201,
            {
                "id": 501,
                "producer_id": None,
                "app_id": 999,
                "mutation": None,
            },
        ],
    )
    updated = effect(UPDATE, path=PATH + "/501", method="PATCH")
    updated["check_update_precondition"] = {
        "check_run_id": 501,
        "head_sha": HEAD,
        "app_id": 999,
        "app_slug": "reviewsensei",
    }
    value = journal(retained=document((created, updated)))
    native = {
        **copy.deepcopy(CREATE),
        **copy.deepcopy(UPDATE),
        "id": 501,
        "app": {"id": 999, "slug": "reviewsensei"},
    }
    return value, native


class PendingCheckSuccessorReadbackTests(unittest.TestCase):
    def test_exact_final_pending_patch_is_confirmed_by_same_native_read(self):
        value, native = fixture()
        http = FakeHTTP((200, native))
        result = value._readback(http, value.effects[0], token="synthetic")
        self.assertEqual(result, (200, native))
        self.assertEqual(value.effects[1]["status"], "confirmed")
        self.assertEqual(
            value.effects[1]["response"],
            [
                200,
                {
                    "id": 501,
                    "producer_id": None,
                    "app_id": 999,
                    "mutation": None,
                },
            ],
        )
        self.assertEqual(
            [(method, path) for method, path, _ in http.calls],
            [
                ("GET", PATH + "/501"),
            ],
        )

    def test_invalid_receipt_compaction_cannot_partially_clear_pending_state(self):
        from review_sensei.errors import ReviewInputError

        value, native = fixture()
        before = copy.deepcopy(value.effects)
        with patch.object(
            value, "_compact_response", side_effect=ReviewInputError("invalid receipt")
        ):
            with self.assertRaisesRegex(ReviewInputError, "invalid receipt"):
                value._readback(
                    FakeHTTP((200, native)), value.effects[0], token="synthetic"
                )
        self.assertEqual(value.effects, before)

    def test_foreign_or_changed_native_projection_never_settles_pending_patch(self):
        for field, replacement in (
            ("app", {"id": 1000, "slug": "reviewsensei"}),
            ("app", {"id": 999, "slug": "foreign"}),
            ("id", 502),
            ("head_sha", "f" * 40),
            ("name", "Another check"),
            ("external_id", "foreign-run"),
            ("status", "in_progress"),
            ("conclusion", "success"),
            ("output", {"title": "Edited", "summary": "Changed"}),
            ("edited_by_human", True),
            ("errors", ["rejected"]),
        ):
            with self.subTest(field=field, replacement=replacement):
                value, native = fixture()
                before = copy.deepcopy(value.effects)
                native[field] = replacement
                http = FakeHTTP((200, native))
                self.assertIsNone(
                    value._readback(http, value.effects[0], token="synthetic")
                )
                self.assertEqual(value.effects, before)
                self.assertEqual(len(http.calls), 1)

    def test_missing_or_foreign_precondition_and_other_unknown_work_remain_held(self):
        for fault in (
            "missing_precondition",
            "foreign_precondition",
            "foreign_path",
            "other_pending",
            "later_confirmed",
        ):
            with self.subTest(fault=fault):
                value, native = fixture()
                pending = value.effects[1]
                if fault == "missing_precondition":
                    del pending["check_update_precondition"]
                elif fault == "foreign_precondition":
                    pending["check_update_precondition"]["head_sha"] = "f" * 40
                elif fault == "foreign_path":
                    pending["path"] = PATH + "/502"
                else:
                    extra = effect(
                        {"body": "Other operation"},
                        path="/repos/owner/repo/pulls/1/reviews",
                    )
                    if fault == "later_confirmed":
                        extra.update(
                            status="confirmed",
                            response=[
                                201,
                                {
                                    "id": 900,
                                    "producer_id": 9001,
                                    "app_id": None,
                                    "mutation": None,
                                },
                            ],
                        )
                    value.effects.append(extra)
                before = copy.deepcopy(value.effects)
                self.assertIsNone(
                    value._readback(
                        FakeHTTP((200, native)), value.effects[0], token="synthetic"
                    )
                )
                self.assertEqual(value.effects, before)
