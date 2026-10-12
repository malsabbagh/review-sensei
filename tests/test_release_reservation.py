import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from review_sensei_github.release_reservation import release_own_reservation
from review_sensei_github.session_ledger import render_session_comment

from review_sensei.session import SessionIdentity, SessionRecord

try:
    from fake_github_http import json_response, make_http
except ImportError:
    from tests.fake_github_http import json_response, make_http

FIXED_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
IDENTITY = SessionIdentity("owner/repo", 136, repository_id=99)
RUN_ID = "10000000001"


class _Broker:
    def __init__(self, owner_run_id=RUN_ID):
        self.owner_run_id = owner_run_id

    def request_oidc_token(self):
        return "oidc.token"

    def open_session_release(
        self, oidc_token, *, repository_id, pull_request, head_sha
    ):
        return SimpleNamespace(
            token="ghs_session",
            state="release",
            reservation_owner={"run_id": self.owner_run_id, "head_sha": head_sha},
        )


def _reserved(owner_run_id=RUN_ID, *, transaction=False):
    record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
    evolved = record.evolve(
        now=FIXED_NOW,
        generation=1,
        reservation_id="abcd1234",
        reserved_slot="initial",
        reservation_owner={"run_id": owner_run_id, "head_sha": "a" * 40},
    )
    return evolved


def _body(record):
    return render_session_comment(repository_id=99, pull_request=136, record=record)


class ReleaseReservationTests(unittest.TestCase):
    def _release(self, http, **overrides):
        kwargs = {
            "broker": _Broker(),
            "http": http,
            "repository": "owner/repo",
            "repository_id": 99,
            "pull_request": 136,
            "head_sha": "a" * 40,
            "run_id": RUN_ID,
            "oidc_token": "oidc.token",
        }
        kwargs.update(overrides)
        return release_own_reservation(**kwargs)

    def test_releases_own_reservation(self):
        import json as _json

        reserved = _reserved()
        state = {"body": _body(reserved)}

        def _is_patch(request):
            return request.method == "PATCH"

        def _patch(request):
            state["body"] = _json.loads(request.data)["body"]
            return json_response({"id": 7, "body": state["body"]})

        def _is_get(request):
            return request.method == "GET"

        def _get(request):
            return json_response([{"id": 7, "body": state["body"]}])

        http, calls = make_http([], routes=[(_is_patch, _patch), (_is_get, _get)])
        self.assertEqual(self._release(http), "released")
        self.assertIn("PATCH", [method for method, _url, _data in calls])

    def test_analysis_checkpoint_is_released_but_publication_is_not(self):
        # Transaction mode always carries an analysis-phase transaction
        # while the reservation is held; that checkpoint is not saved
        # publication work, so the owner release still applies.
        import json as _json

        from review_sensei.convergence import ReviewConvergencePolicy
        from review_sensei.session import (
            InMemorySessionLedger,
            prepare_review_transaction,
        )

        memory = InMemorySessionLedger()
        prepared = prepare_review_transaction(
            memory,
            IDENTITY,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id="d" * 64,
            base_sha="c" * 40,
            head_sha="a" * 40,
            configuration_digest="e" * 64,
            evidence_digest="f" * 64,
            now=FIXED_NOW,
        )
        reserved = prepared.record.evolve(
            reservation_owner={"run_id": RUN_ID, "head_sha": "a" * 40},
            now=FIXED_NOW,
        )
        state = {"body": _body(reserved)}

        def _is_patch(request):
            return request.method == "PATCH"

        def _patch(request):
            state["body"] = _json.loads(request.data)["body"]
            return json_response({"id": 7, "body": state["body"]})

        def _is_get(request):
            return request.method == "GET"

        def _get(request):
            return json_response([{"id": 7, "body": state["body"]}])

        http, calls = make_http([], routes=[(_is_patch, _patch), (_is_get, _get)])
        self.assertEqual(self._release(http), "released")
        self.assertIn("PATCH", [method for method, _url, _data in calls])

    def test_not_owner_is_a_noop(self):
        reserved = _reserved(owner_run_id="99999999999")
        http, calls = make_http([json_response([{"id": 7, "body": _body(reserved)}])])
        self.assertEqual(self._release(http), "not-owner")
        self.assertNotIn("PATCH", [method for method, _url, _data in calls])

    def test_load_failure_is_not_reported_as_absence(self):
        # An expired session is a load failure, not "no reservation":
        # it must surface so the cleanup step warns instead of silently
        # leaving the reservation held.
        from datetime import timedelta

        from review_sensei.errors import ReviewInputError

        record = SessionRecord.create(
            IDENTITY,
            now=FIXED_NOW - timedelta(days=40),
            expires_at=FIXED_NOW - timedelta(days=10),
        )
        http, _calls = make_http([json_response([{"id": 7, "body": _body(record)}])])
        with self.assertRaises(ReviewInputError):
            self._release(http)

    def test_no_reservation_is_a_noop(self):
        record = SessionRecord.create(IDENTITY, now=FIXED_NOW)
        http, calls = make_http([json_response([{"id": 7, "body": _body(record)}])])
        self.assertEqual(self._release(http), "no-reservation")
        self.assertNotIn("PATCH", [method for method, _url, _data in calls])


if __name__ == "__main__":
    unittest.main()
