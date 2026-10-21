import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from review_sensei_github import (
    BrokerClient,
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status
        self.reason = "reason"
        self.headers = {}


class BrokerClientTests(unittest.TestCase):
    def test_oidc_late_read_never_returns_assertion_after_transport_timeout(self):
        client = BrokerClient(
            opener=lambda *args, **kwargs: FakeResponse(b'{"value":"secret"}')
        )
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://fixture.invalid?fixture=1",
                },
                clear=True,
            ),
            mock.patch(
                "review_sensei_github.broker_client.time.monotonic",
                side_effect=[0, 31, 31],
            ),
        ):
            with self.assertRaises(GitHubBrokerClientError) as caught:
                client.request_oidc_token()
        self.assertIn("broker.transport_failure=timeout", str(caught.exception))
        self.assertIn("broker.post_outcome=not_applicable", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_oidc_late_read_rechecks_original_uncontrolled_budget(self):
        clock = [0.0]
        budget = EvidenceReadBudget(clock=lambda: clock[0])
        calls = []

        def opener(request, timeout):
            calls.append(request)
            clock[0] = 60.0
            return FakeResponse(b'{"value":"secret"}')

        client = BrokerClient(opener=opener, before_request=budget.consume)
        with mock.patch.dict(
            "os.environ",
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://fixture.invalid?fixture=1",
            },
            clear=True,
        ):
            with self.assertRaises(GitHubBrokerClientError) as caught:
                client.request_oidc_token()
        self.assertIsInstance(caught.exception.__cause__, ReviewInputError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.calls, 1)

    def test_exhausted_admission_preserves_broker_error_and_never_dispatches(self):
        budget = EvidenceReadBudget(clock=lambda: 0)
        for _ in range(60):
            budget.consume()
        original = budget.snapshot()
        client, calls = self.make_client(
            (b'{"token":"must-not-return"}', 200), before_request=budget.consume
        )
        with self.assertRaises(GitHubBrokerClientError) as caught:
            client.exchange("assertion")
        self.assertIsInstance(caught.exception.__cause__, ReviewInputError)
        self.assertEqual(budget.snapshot(), original)
        self.assertEqual(calls, [])

    def test_transport_failures_name_phase_without_remote_text_or_post_retry(self):
        class ReadFailure(FakeResponse):
            def read(self, size=-1):
                raise TimeoutError("secret read body")

        cases = [
            (TimeoutError("secret open"), "request_open", "timeout"),
            (URLError("secret URL"), "request_open", "transport"),
            (ReadFailure(), "response_read", "timeout"),
            (FakeResponse(b"bad json secret"), "response_decode", "invalid_json"),
            (FakeResponse(bytes([255])), "response_decode", "invalid_utf8"),
            (FakeResponse(b"x" * (256 * 1024 + 1)), "response_size", "oversized"),
        ]
        for response, phase, failure in cases:
            with self.subTest(phase=phase, failure=failure):
                calls = []

                def opener(request, timeout):
                    calls.append((request, timeout))
                    if isinstance(response, Exception):
                        raise response
                    return response

                client = BrokerClient(opener=opener)
                with self.assertRaises(GitHubBrokerClientError) as raised:
                    client.exchange("secret assertion")
                message = str(raised.exception)
                self.assertIn("broker.transport_phase=" + phase, message)
                self.assertIn("broker.transport_failure=" + failure, message)
                self.assertIn("broker.post_outcome=unknown", message)
                self.assertNotIn("secret", message)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1], 30)

    def test_bootstrap_header_only_shortens_admitted_time_without_new_control(self):
        client, calls = self.make_client(
            (b'{"token":"scoped"}', 200), before_request=lambda: 1.5
        )
        with mock.patch(
            "review_sensei_github.broker_client.time.time", return_value=1000
        ):
            self.assertEqual(
                client.exchange("assertion", capability="issue_reply"), "scoped"
            )
        self.assertEqual(calls[0][2]["X-reviewsensei-deadline-unix-ms"], "1001500")
        self.assertEqual(
            json.loads(calls[0][3]),
            {"oidc_token": "assertion", "capability": "issue_reply"},
        )

    def test_late_success_keeps_original_budget_and_never_returns_credential(self):
        clock = [0.0]
        budget = EvidenceReadBudget(clock=lambda: clock[0])
        before = budget.snapshot()
        calls = []

        def opener(request, timeout):
            calls.append(request)
            clock[0] = 60.0
            return FakeResponse(b'{"token":"must-not-return"}')

        client = BrokerClient(opener=opener, before_request=budget.consume)
        with self.assertRaises(GitHubBrokerClientError) as caught:
            client.exchange("assertion")
        self.assertIsInstance(caught.exception.__cause__, ReviewInputError)
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.calls, 1)
        self.assertEqual(
            budget.snapshot()["deadline_unix_ms"], before["deadline_unix_ms"]
        )

    def test_bootstrap_header_never_extends_original_wall_deadline(self):
        budget = EvidenceReadBudget(clock=lambda: 0, wall_clock=lambda: 1000)
        client, calls = self.make_client(
            (b'{"token":"scoped"}', 200), before_request=budget.consume
        )
        with mock.patch(
            "review_sensei_github.broker_client.time.time", return_value=1040
        ):
            self.assertEqual(client.exchange("assertion"), "scoped")
        self.assertEqual(calls[0][2]["X-reviewsensei-deadline-unix-ms"], "1060000")
        self.assertEqual(budget.calls, 1)
        self.assertEqual(budget.wall_deadline_ms, 1060000)

    def test_transport_time_fence_retains_only_valid_status_and_correlation(self):
        response = FakeResponse(b'{"token":"must-not-return"}')
        response.headers = {"cf-ray": "0123456789abcdef-YYZ", "authorization": "secret"}
        client = BrokerClient(opener=lambda *args, **kwargs: response)
        with mock.patch(
            "review_sensei_github.broker_client.time.monotonic", side_effect=[0, 31, 31]
        ):
            with self.assertRaises(GitHubBrokerClientError) as raised:
                client.exchange("assertion")
        text = str(raised.exception)
        self.assertIn("broker.transport_failure=timeout", text)
        self.assertIn("broker.response_status=200", text)
        self.assertIn("broker.correlation_id=0123456789abcdef-YYZ", text)
        self.assertIn("broker.post_outcome=unknown", text)
        self.assertNotIn("secret", text)

    def make_client(self, responses, **kwargs):
        calls = []

        def opener(request, timeout):
            calls.append(
                (request.method, request.full_url, request.headers, request.data)
            )
            if isinstance(responses, list):
                response = responses.pop(0)
            else:
                response = responses
            if isinstance(response, Exception):
                raise response
            return FakeResponse(response[0], response[1])

        return BrokerClient(
            broker_url="https://broker.reviewsensei.dev/api/github/token",
            opener=opener,
            **kwargs,
        ), calls

    def test_constructor_and_oidc_request_fail_closed(self):
        with self.assertRaises(GitHubBrokerClientError):
            BrokerClient(broker_url="http://broker.reviewsensei.dev/token")
        with self.assertRaises(GitHubBrokerClientError):
            BrokerClient(broker_url="https://broker.reviewsensei.dev/api/github")
        with self.assertRaises(GitHubBrokerClientError):
            BrokerClient(timeout=0)

        client, _ = self.make_client((b"{}", 200))
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(GitHubBrokerClientError):
                client.request_oidc_token()

        with mock.patch.dict(
            "os.environ",
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.test",
            },
            clear=True,
        ):
            for body in (b"not-json", b"[]", b"{}"):
                client, _ = self.make_client((body, 200))
                with self.assertRaises(GitHubBrokerClientError):
                    client.request_oidc_token()

    def test_request_and_exchange_sanitize_transport_failures(self):
        request_error = URLError("secret transport detail")
        client, _ = self.make_client(request_error)
        with mock.patch.dict(
            "os.environ",
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.test",
            },
            clear=True,
        ):
            with self.assertRaises(GitHubBrokerClientError) as raised:
                client.request_oidc_token()
        self.assertNotIn("secret transport detail", str(raised.exception))

        for code, expected in (
            (400, GitHubBrokerClientError),
            (429, GitHubHTTPTransientError),
        ):
            err = HTTPError(
                "https://broker.reviewsensei.test",
                code,
                "error",
                {},
                FakeResponse(b'{"message":"secret"}', code),
            )
            client, _ = self.make_client(err)
            with self.assertRaises(expected):
                client.exchange("oidc")

    def test_exchange_rejects_malformed_response_shapes(self):
        for body in (b"not-json", b"[]", b"{}"):
            client, _ = self.make_client((body, 200))
            with self.assertRaises(GitHubBrokerClientError):
                client.exchange("oidc")

    def test_request_oidc_token_from_environment(self):
        client, calls = self.make_client((b'{"value":"oidc.token"}', 200))
        with mock.patch.dict(
            "os.environ",
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": (
                    "https://token.actions.test?audience=x"
                ),
            },
            clear=True,
        ):
            self.assertEqual(client.request_oidc_token(), "oidc.token")
        self.assertIn("audience=sts.reviewsensei.dev", calls[0][1])
        self.assertEqual(calls[0][2]["Authorization"], "Bearer request-token")

    def test_exchange_posts_fixed_broker_body(self):
        client, calls = self.make_client((b'{"token":"ghs_capability"}', 200))
        token = client.exchange("oidc.token")
        self.assertEqual(token, "ghs_capability")
        body = json.loads(calls[0][3].decode("utf-8"))
        self.assertEqual(body, {"oidc_token": "oidc.token"})

    def test_exchange_posts_only_named_capability(self):
        client, calls = self.make_client((b'{"token":"ghs_capability"}', 200))
        self.assertEqual(
            client.exchange("oidc.token", capability="review_publish"),
            "ghs_capability",
        )
        body = json.loads(calls[0][3].decode("utf-8"))
        self.assertEqual(
            body,
            {"oidc_token": "oidc.token", "capability": "review_publish"},
        )

    def test_exchange_accepts_read_only_status_capability(self):
        client, calls = self.make_client((b'{"token":"ghs_capability"}', 200))
        self.assertEqual(
            client.exchange("oidc.token", capability="review_status"),
            "ghs_capability",
        )
        body = json.loads(calls[0][3].decode("utf-8"))
        self.assertEqual(
            body,
            {"oidc_token": "oidc.token", "capability": "review_status"},
        )

    def test_exchange_accepts_separate_read_only_actions_capability(self):
        client, calls = self.make_client((b'{"token":"ghs_actions_read"}', 200))
        self.assertEqual(
            client.exchange("oidc.token", capability="review_actions"),
            "ghs_actions_read",
        )
        self.assertEqual(
            json.loads(calls[0][3].decode()),
            {"oidc_token": "oidc.token", "capability": "review_actions"},
        )

    def test_open_session_preserves_only_valid_broker_owner(self):
        for owner in (
            {"run_id": "123", "head_sha": "a" * 40},
            {"run_id": "123", "head_sha": "b" * 40},
            {"run_id": "invalid", "head_sha": "a" * 40},
        ):
            with self.subTest(owner=owner):
                response = json.dumps(
                    {
                        "token": "ghs_session",
                        "capability": "review_session",
                        "session_state": "known",
                        "reservation_owner": owner,
                    }
                ).encode()
                client, _ = self.make_client((response, 200))
                if owner["run_id"] == "123" and owner["head_sha"] == "a" * 40:
                    self.assertEqual(
                        client.open_session(
                            "oidc.token",
                            repository_id=99,
                            pull_request=7,
                            head_sha="a" * 40,
                        ).reservation_owner,
                        owner,
                    )
                else:
                    with self.assertRaises(GitHubBrokerClientError):
                        client.open_session(
                            "oidc.token",
                            repository_id=99,
                            pull_request=7,
                            head_sha="a" * 40,
                        )

    def test_open_session_release_accepts_moved_head_owner_token(self):
        response = json.dumps(
            {
                "token": "ghs_session",
                "capability": "review_session",
                "release_only": True,
                "reservation_owner": {"run_id": "123", "head_sha": "a" * 40},
            }
        ).encode()
        client, calls = self.make_client((response, 200))
        session = client.open_session_release(
            "oidc.token",
            repository_id=99,
            pull_request=7,
            head_sha="a" * 40,
        )
        self.assertEqual(session.state, "release")
        self.assertEqual(
            session.reservation_owner, {"run_id": "123", "head_sha": "a" * 40}
        )
        payload = json.loads(calls[0][3])
        self.assertEqual(payload["release_only"], True)
        self.assertEqual(payload["capability"], "review_session")

    def test_open_session_release_rejects_grants_and_state(self):
        for extra in ({"session_state": "enrolled"}, {"session_grant": "sg1.x"}):
            with self.subTest(extra=extra):
                response = json.dumps(
                    {
                        "token": "ghs_session",
                        "capability": "review_session",
                        "release_only": True,
                        "reservation_owner": {
                            "run_id": "123",
                            "head_sha": "a" * 40,
                        },
                        **extra,
                    }
                ).encode()
                client, _ = self.make_client((response, 200))
                with self.assertRaises(GitHubBrokerClientError):
                    client.open_session_release(
                        "oidc.token",
                        repository_id=99,
                        pull_request=7,
                        head_sha="a" * 40,
                    )

    def test_open_session_requires_a_broker_attested_scope(self):
        client, calls = self.make_client(
            (
                b'{"token":"ghs_session","capability":"review_session",'
                b'"session_state":"enrolled"}',
                200,
            )
        )
        session = client.open_session(
            "oidc.token",
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
        )
        self.assertEqual(session.token, "ghs_session")
        self.assertEqual(session.state, "enrolled")
        body = json.loads(calls[0][3].decode("utf-8"))
        self.assertEqual(
            body,
            {
                "oidc_token": "oidc.token",
                "capability": "review_session",
                "session": {
                    "repository_id": 987654321,
                    "pull_request": 7,
                    "head_sha": "a" * 40,
                },
            },
        )

    def test_open_session_rejects_a_rate_limited_state(self):
        client, _calls = self.make_client(
            (
                b'{"token":"ghs_session","capability":"review_session",'
                b'"session_state":"rate_limited"}',
                200,
            )
        )
        with self.assertRaises(GitHubBrokerClientError):
            client.open_session(
                "oidc.token",
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
            )

    def test_session_mutation_grant_requires_broker_derived_actor_fields(self):
        request_attestation = {
            "version": 1,
            "repository": "acme/widgets",
            "repository_id": 987654321,
            "pull_request": 7,
            "head_sha": "a" * 40,
            "operation": "command",
            "source_comment_id": 13579,
            "run_id": "10000000001",
            "issued_at": 1_700_000_000,
            "concurrency_group": "reviewsensei-session-987654321-7",
            "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "job_workflow_sha": "a" * 40,
        }
        returned_attestation = {
            **request_attestation,
            "actor": "octocat",
            "actor_type": "User",
            "association": "OWNER",
            "command_id": 13579,
            "command_digest": "b" * 64,
        }
        response = json.dumps(
            {
                "token": "ghs_session",
                "capability": "review_session",
                "session_state": "known",
                "session_grant": "sg1.1780000000000." + "c" * 43,
                "session_attestation": returned_attestation,
            }
        ).encode("utf-8")
        client, calls = self.make_client((response, 200))
        grant = client.authorize_session_mutation(
            "oidc.token",
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            session_attestation=request_attestation,
        )
        self.assertEqual(grant.attestation["actor"], "octocat")
        self.assertEqual(grant.attestation["command_id"], 13579)
        self.assertNotIn("ghs_session", repr(grant))
        self.assertNotIn("sg1.1780000000000." + "c" * 43, repr(grant))
        payload = json.loads(calls[0][3].decode("utf-8"))
        self.assertNotIn("actor", payload["session_attestation"])

        missing_actor = {**returned_attestation}
        del missing_actor["actor"]
        client, _ = self.make_client(
            (
                json.dumps(
                    {
                        "token": "ghs_session",
                        "capability": "review_session",
                        "session_state": "known",
                        "session_grant": "sg1.1780000000000." + "c" * 43,
                        "session_attestation": missing_actor,
                    }
                ).encode("utf-8"),
                200,
            )
        )
        with self.assertRaises(GitHubBrokerClientError):
            client.authorize_session_mutation(
                "oidc.token",
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                session_attestation=request_attestation,
            )

    def test_broker_requests_send_reviewsensei_user_agent(self):
        attestation = {
            "version": 1,
            "repository": "acme/widgets",
            "repository_id": 987654321,
            "pull_request": 7,
            "head_sha": "a" * 40,
            "operation": "command",
            "source_comment_id": 13579,
            "run_id": "10000000001",
            "issued_at": 1_700_000_000,
            "concurrency_group": "reviewsensei-session-987654321-7",
            "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
            "job_workflow_sha": "a" * 40,
            "actor": "octocat",
            "actor_type": "User",
            "association": "OWNER",
            "command_id": 13579,
            "command_digest": "b" * 64,
        }
        grant = "sg1.1780000000000." + "c" * 43
        client, calls = self.make_client(
            [
                (b'{"token":"ghs_capability"}', 200),
                (json.dumps({"session_attestation": attestation}).encode(), 200),
            ]
        )
        client.exchange("oidc.token")
        client.verify_session_grant(grant, attestation)
        self.assertEqual(
            [url for _, url, _, _ in calls],
            [
                "https://broker.reviewsensei.dev/api/github/token",
                "https://broker.reviewsensei.dev/api/github/session-grant",
            ],
        )
        for _, _, headers, _ in calls:
            self.assertEqual(
                headers["User-agent"],
                "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
            )

    def test_exchange_rejects_arbitrary_capability(self):
        client, calls = self.make_client((b'{"token":"ghs_capability"}', 200))
        with self.assertRaises(GitHubBrokerClientError):
            client.exchange("oidc.token", capability="contents_write")
        self.assertEqual(calls, [])

    def test_exchange_rejects_invalid_or_empty_token(self):
        client, _ = self.make_client((b"{}", 200))
        with self.assertRaises(GitHubBrokerClientError):
            client.exchange("")

    def test_transient_http_error_sanitized(self):
        err = HTTPError(
            "https://broker.reviewsense.test",
            500,
            "error",
            {},
            FakeResponse(b'{"message":"secret"}', 500),
        )
        client, _ = self.make_client(err)
        with self.assertRaises(GitHubHTTPTransientError) as raised:
            client.exchange("oidc")
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("oidc", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
