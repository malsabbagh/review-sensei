"""Existing check updates bind an observed App, target, head and exact output."""

import copy
import unittest

from review_sensei_github.finding_lifecycle import _validate

from review_sensei.errors import ReviewInputError
from tests import test_finding_lifecycle_journal as fixture

PATH = "/repos/owner/repo/check-runs/1234"
BODY = {
    "status": "completed",
    "conclusion": "success",
    "output": {"title": "Ready", "summary": "Exact"},
}


def native():
    return {
        "id": 1234,
        "name": "ReviewSensei",
        "head_sha": fixture.HEAD,
        "app": {"id": 999, "slug": "reviewsensei"},
        **copy.deepcopy(BODY),
    }


class CheckReceiptBindingTests(unittest.TestCase):
    def prepared(self):
        journal = fixture.journal()
        source = native()
        route = f"/repos/owner/repo/commits/{fixture.HEAD}/check-runs?check_name=ReviewSensei&per_page=100"
        journal._observe_check_read(
            "GET", route, (200, {"total_count": 1, "check_runs": [source]})
        )
        effect = fixture.effect(BODY, path=PATH, method="PATCH")
        effect["check_update_precondition"] = journal._check_precondition(PATH)
        return journal, effect, source, route

    def test_exact_existing_check_confirms_and_roundtrips(self):
        journal, effect, _, _ = self.prepared()
        journal._dispatch_effect(
            fixture.FakeHTTP((200, native())),
            effect,
            "PATCH",
            PATH,
            token="fixture",
            body=BODY,
        )
        self.assertEqual(effect["status"], "confirmed")
        self.assertEqual(effect["response"][1]["id"], 1234)
        self.assertEqual(effect["response"][1]["app_id"], 999)
        _validate(fixture.document([effect]))

    def test_foreign_or_malformed_success_stays_pending(self):
        for fault in (
            "id",
            "bool",
            "head",
            "app",
            "slug",
            "output",
            "status",
            "errors",
            "missing",
        ):
            with self.subTest(fault=fault):
                journal, effect, _, _ = self.prepared()
                response = native()
                if fault == "id":
                    response["id"] = 1235
                elif fault == "bool":
                    response["id"] = True
                elif fault == "head":
                    response["head_sha"] = "e" * 40
                elif fault == "app":
                    response["app"]["id"] = 1000
                elif fault == "slug":
                    response["app"]["slug"] = "foreign"
                elif fault == "output":
                    response["output"]["summary"] = "Changed"
                elif fault == "errors":
                    response["errors"] = [{"message": "partial"}]
                elif fault == "missing":
                    del response["app"]["id"]
                http = fixture.FakeHTTP((201 if fault == "status" else 200, response))
                before = copy.deepcopy(effect)
                with self.assertRaises(ReviewInputError):
                    journal._dispatch_effect(
                        http, effect, "PATCH", PATH, token="fixture", body=BODY
                    )
                self.assertEqual(effect, before)
                self.assertEqual(len(http.calls), 1)

    def test_missing_app_id_is_generic_and_stale_observation_is_not_reused(self):
        journal, _, source, route = self.prepared()
        del source["app"]["id"]
        with self.assertRaises(ReviewInputError):
            journal._check_precondition(PATH)
        journal._observe_check_read("GET", route, (200, {"check_runs": [source]}))
        self.assertIsNone(journal._check_precondition(PATH))
        journal._observe_check_read("GET", route, (503, None))
        self.assertIsNone(journal._check_precondition(PATH))

    def test_stopped_command_has_no_later_native_intent_or_dispatch(self):
        journal = fixture.journal()
        journal._command_native_stopped = True
        http = fixture.FakeHTTP((200, native()))
        before = copy.deepcopy(journal.effects)
        with self.assertRaisesRegex(ReviewInputError, "later native dispatch stopped"):
            journal.request(http, "PATCH", PATH, token="fixture", body=BODY)
        self.assertEqual(journal.effects, before)
        self.assertEqual(http.calls, [])

    def test_cold_pending_readback_never_reposts_and_requires_same_app(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                journal, effect, _, _ = self.prepared()
                retained = fixture.document([effect])
                cold = fixture.journal(retained=retained)
                current = native()
                if foreign:
                    current["app"]["id"] = 1000
                http = fixture.FakeHTTP((200, current))
                result = cold._readback(http, cold.effects[0], token="fixture")
                self.assertEqual(result is None, foreign)
                self.assertEqual(len(http.calls), 1)
                self.assertEqual(http.calls[0][0], "GET")
                self.assertEqual(cold.effects[0]["status"], "pending")
