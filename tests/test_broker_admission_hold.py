"""The operator admission hold is one charged response, never a retry policy."""

import io
import json
import unittest
from urllib.error import HTTPError

from review_sensei_github.broker_admission import inspect_broker_http_error
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.errors import (
    GitHubBrokerAdmissionHeldError,
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.broker_diagnostics import MAX_DIAGNOSTIC_BYTES, BrokerDiagnostic

HELD = b'{"error":"broker_admission_held","temporary":true}'
TOKEN_URL = "https://github.reviewsensei.dev/github/token"
GRANT_URL = "https://github.reviewsensei.dev/github/session-grant"


class TrackedError(HTTPError):
    def __init__(self, code=503, body=HELD, *, read_error=False, close_error=False):
        super().__init__(TOKEN_URL, code, "remote reason withheld", {}, io.BytesIO(body))
        self.read_sizes = []
        self.close_calls = 0
        self.read_error, self.close_error = read_error, close_error

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.read_error:
            raise OSError("sensitive remote transport detail")
        return self.fp.read(size)

    def close(self):
        self.close_calls += 1
        super().close()
        if self.close_error:
            raise OSError("sensitive remote close detail")


class BrokerAdmissionHoldTests(unittest.TestCase):
    def call(self, error, *, url=TOKEN_URL, maximum=12):
        attempts = []
        budget = EvidenceReadBudget()

        def opener(request, timeout):
            attempts.append(request)
            raise error

        client = BrokerClient(
            opener=opener,
            control=BrokerDispatchAccounting(budget, "a" * 64, maximum, maximum),
        )
        try:
            # Exercise the actual shared HTTP transport for both sensitive
            # broker surfaces. Authority parsing has its own protocol tests.
            client._post({"opaque": "request"}, url=url)
        except (GitHubBrokerClientError, GitHubHTTPTransientError) as raised:
            return raised, attempts, budget
        self.fail("broker HTTP error unexpectedly returned a capability")

    def test_exact_held_response_is_typed_one_attempt_one_charge_on_both_surfaces(self):
        for url in (TOKEN_URL, GRANT_URL):
            for maximum in (9, 12, 30):
                with self.subTest(url=url, maximum=maximum):
                    error = TrackedError()
                    raised, attempts, budget = self.call(error, url=url, maximum=maximum)
                    self.assertIs(type(raised), GitHubBrokerAdmissionHeldError)
                    self.assertEqual(raised.error_category, "github_broker_admission_held")
                    self.assertIn("temporarily held by the operator", str(raised))
                    self.assertIn("no automatic retry", str(raised))
                    self.assertIsNone(raised.broker_diagnostic)
                    self.assertEqual(len(attempts), 1)
                    self.assertEqual(attempts[0].method, "POST")
                    self.assertEqual(attempts[0].full_url, url)
                    self.assertEqual(budget.calls, 1)
                    self.assertEqual(error.read_sizes, [MAX_DIAGNOSTIC_BYTES + 1])
                    self.assertEqual(error.close_calls, 1)
                    self.assertIs(raised.__cause__, error)

    def test_nonexact_or_non503_responses_preserve_original_error_contracts(self):
        cases = [
            (503, b'{"error":"broker_admission_held","temporary":1}'),
            (503, b'{"error":"broker_admission_held","temporary":"true"}'),
            (503, b'{"error":"broker_admission_held","temporary":false}'),
            (503, b'{"error":"broker_admission_held"}'),
            (503, b'{"error":"broker_admission_held","temporary":true,"token":"sensitive"}'),
            (503, b'{"error":"broker_admission_held","temporary":false,"temporary":true}'),
            (503, b'{"error":"another_error","temporary":true}'),
            (503, b'["broker_admission_held",true]'),
            (503, b'{"error":"broker_admission_held","temporary":tru'),
            (503, b'\xffsensitive'),
            (503, HELD + b" " * MAX_DIAGNOSTIC_BYTES),
            (429, HELD), (431, HELD), (500, HELD), (502, HELD), (504, HELD),
        ]
        for code, body in cases:
            with self.subTest(code=code, body_length=len(body)):
                error = TrackedError(code, body)
                raised, attempts, budget = self.call(error)
                expected = GitHubHTTPTransientError if code == 429 or code >= 500 else GitHubBrokerClientError
                self.assertIs(type(raised), expected)
                self.assertEqual(raised.broker_diagnostic.code, "broker_unknown")
                self.assertNotIn("sensitive", str(raised))
                self.assertEqual(len(attempts), 1)
                self.assertEqual(budget.calls, 1)
                self.assertEqual(error.read_sizes, [MAX_DIAGNOSTIC_BYTES + 1])
                self.assertEqual(error.close_calls, 1)

    def test_existing_closed_ordinary_diagnostic_and_correlation_survive_single_read(self):
        diagnostic = BrokerDiagnostic("broker_unknown", "0123456789abcdef-ABC")
        body = json.dumps({"error": "ordinary", "diagnostic": {
            "version": 1, "code": diagnostic.code, "stage": diagnostic.stage,
            "action": diagnostic.action, "correlation_id": diagnostic.correlation_id,
        }}).encode()
        error = TrackedError(431, body)
        raised, attempts, budget = self.call(error)
        self.assertIs(type(raised), GitHubBrokerClientError)
        self.assertEqual(raised.broker_diagnostic, diagnostic)
        self.assertEqual(error.read_sizes, [MAX_DIAGNOSTIC_BYTES + 1])
        self.assertEqual(error.close_calls, 1)
        self.assertEqual((len(attempts), budget.calls), (1, 1))

    def test_known_ordinary_diagnostic_does_not_change_outer_status_contract(self):
        for code in (431, 429, 500, 503, 504):
            for upstream in (401, 500):
                with self.subTest(code=code, upstream=upstream):
                    diagnostic = BrokerDiagnostic(
                        "github_capability_issue_failed", upstream_status=upstream
                    )
                    body = json.dumps({"diagnostic": {
                        "version": 1, "code": diagnostic.code,
                        "stage": diagnostic.stage, "action": diagnostic.action,
                        "upstream_status": upstream,
                    }}).encode()
                    error = TrackedError(code, body)
                    raised, attempts, budget = self.call(error)
                    expected = GitHubBrokerClientError if code == 431 else GitHubHTTPTransientError
                    self.assertIs(type(raised), expected)
                    self.assertEqual(raised.broker_diagnostic, diagnostic)
                    self.assertEqual((len(attempts), budget.calls), (1, 1))
                    self.assertEqual(error.read_sizes, [MAX_DIAGNOSTIC_BYTES + 1])
                    self.assertEqual(error.close_calls, 1)

    def test_failed_body_read_and_failed_close_are_sanitized_without_followup(self):
        for read_error, close_error in ((True, False), (False, True), (True, True)):
            with self.subTest(read_error=read_error, close_error=close_error):
                error = TrackedError(body=b"sensitive body", read_error=read_error, close_error=close_error)
                raised, attempts, budget = self.call(error)
                self.assertIs(type(raised), GitHubHTTPTransientError)
                self.assertEqual(raised.broker_diagnostic.code, "broker_unknown")
                self.assertNotIn("sensitive", str(raised))
                self.assertEqual((len(attempts), budget.calls), (1, 1))
                self.assertEqual(error.read_sizes, [MAX_DIAGNOSTIC_BYTES + 1])
                self.assertEqual(error.close_calls, 1)

    def test_correlation_header_survives_unknown_or_truncated_body(self):
        error = TrackedError(body=b"truncated{")
        error.headers["cf-ray"] = "0123456789abcdef-ABC"
        inspected = inspect_broker_http_error(error)
        self.assertFalse(inspected.admission_held)
        self.assertEqual(inspected.diagnostic.correlation_id, "0123456789abcdef-ABC")
        self.assertEqual(error.read_sizes, [MAX_DIAGNOSTIC_BYTES + 1])
        self.assertEqual(error.close_calls, 1)
