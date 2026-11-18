"""A response cannot install a self-restoring helper before source resumes."""

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.action_run import execute_command
from review_sensei_github.publication_recovery import _PaginationObservationHttp

from review_sensei.errors import ReviewInputError
from tests import test_trust_status_route as status_fixture
from tests.test_run_authority import NOW
from tests.test_trust_pause_route import PauseFixture


class ReturnFixture(PauseFixture):
    boundary = None
    armed = False

    def open(self, request, timeout):
        response = super().open(request, timeout)
        if not self.armed or request.get_method() != "PATCH":
            return response
        self.armed = False
        fixture = self
        if self.boundary == "lost":
            self.install()
            raise OSError("stored physical response lost")

        class Returned:
            @property
            def status(self):
                if fixture.boundary in {"status", "status-cleanup-error"}:
                    fixture.install()
                return response.status

            def __getattr__(self, name):
                return getattr(response, name)

            def __enter__(self):
                response.__enter__()
                if fixture.boundary in {"enter", "enter-error", "enter-cleanup-error"}:
                    fixture.install()
                if fixture.boundary in {"enter-error", "enter-cleanup-error"}:
                    raise OSError("synthetic failed response enter")
                return self

            def read(self, size):
                raw = response.read(size)
                if fixture.boundary in {"read", "read-restore-cleanup-error"}:
                    fixture.install()
                return raw

            def __exit__(self, *args):
                fixture.closed = True
                result = response.__exit__(*args)
                if fixture.boundary == "read-restore-cleanup-error":
                    fixture.restore()
                    raise OSError("cleanup restored source but failed")
                if fixture.boundary in {"enter-cleanup-error", "status-cleanup-error"}:
                    raise OSError("synthetic response cleanup failed")
                if fixture.boundary == "exit":
                    fixture.install()
                return result

        return Returned()


class ResponseCustodyTests(unittest.TestCase):
    def test_response_mutation_never_invokes_shadow_and_keeps_physical_debit(self):
        for boundary in (
            "status",
            "enter",
            "read",
            "exit",
            "lost",
            "enter-error",
            "enter-cleanup-error",
            "status-cleanup-error",
            "read-restore-cleanup-error",
        ):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as directory,
                patch("time.time", return_value=NOW),
                patch.object(status_fixture, "BODY", "@reviewsensei review pause"),
            ):
                f = ReturnFixture(directory)
                base = f.http
                wrapper = _PaginationObservationHttp(base, f.caller.budget_view)
                f.http = f.ledger.http = f.action_ledger.http = wrapper
                f.run = replace(f.run, http=wrapper)
                called, at_boundary = [], []
                from review_sensei_github.api_run_requests import _AccountedResponse

                changes = []

                def malicious(*args, **kwargs):
                    called.append(True)
                    del wrapper.pagination_page_size
                    return base.request(
                        "GET", "/repos/owner/repo/pulls/7", token=f.session.token
                    )

                def install():
                    at_boundary.append(len(f.native))
                    wrapper.pagination_page_size = malicious
                    change = patch.object(
                        _AccountedResponse,
                        "_require_return",
                        lambda self: None,
                        create=True,
                    )
                    change.start()
                    changes.append(change)

                def restore():
                    del wrapper.pagination_page_size
                    for change in reversed(changes):
                        change.stop()
                    changes.clear()

                f.install, f.restore = install, restore
                f.boundary, f.armed, f.closed = boundary, True, False
                try:
                    with self.assertRaises(ReviewInputError):
                        execute_command(f.run, f.args)
                finally:
                    for change in reversed(changes):
                        change.stop()
                self.assertFalse(called)
                self.assertEqual(len(f.native), at_boundary[0])
                self.assertEqual(sum(method == "PATCH" for method, _, _ in f.native), 1)
                native = f.caller.observations[-1]
                self.assertTrue(native["mutation"])
                self.assertEqual(
                    native["outcome"],
                    "response"
                    if boundary in {"read", "exit", "read-restore-cleanup-error"}
                    else "unknown",
                )
                self.assertNotEqual(f.caller.accounting.action["phase"], "terminal")
                if boundary != "lost":
                    self.assertTrue(f.closed)
