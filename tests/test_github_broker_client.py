import io
import json
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from review_sensei_github import (
    BrokerClient,
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError


# Synthetic caller-owned v3 input. These transport unit tests do not issue
# real OIDC assertions or emulate the Worker's independent source admission.
def current_authority(purpose="review", *, repository_id=99, head_sha="a" * 40):
    return {
        "version": 3,
        "purpose": purpose,
        "repository": "acme/widgets",
        "repository_id": repository_id,
        "pull_request": 7,
        "head_sha": head_sha,
        "run_id": "123",
        "run_attempt": 1,
        "job": "command" if purpose == "command" else "hosted",
        "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
        "job_workflow_sha": "a" * 40,
        "issued_at": 1_700_000_000,
        "expires_at": 1_700_000_600,
        "audience": "reviewsensei-run-action",
        "source": (
            {"kind": "pull_request", "id": 7, "updated_at": None, "body_sha256": None}
            if purpose == "review"
            else {
                "kind": "issue_comment",
                "id": 13579,
                "updated_at": "2026-10-26T00:00:00Z",
                "body_sha256": "b" * 64,
            }
        ),
    }


def current_grant(authority):
    return {
        **authority,
        "actor": None
        if authority["purpose"] == "review"
        else {"login": "octocat", "type": "User", "association": "OWNER"},
    }


def session_response(authority, *, owner=None, **extra):
    """Explicit synthetic response for the fixture's known request subject."""

    def respond(payload):
        expected = {
            **authority,
            "issued_at": payload["session_attestation"]["issued_at"],
            "expires_at": payload["session_attestation"]["expires_at"],
        }
        assert payload["session_attestation"] == expected
        return json.dumps(
            {
                "token": "ghs_session",
                "capability": "review_session",
                "session_state": "known",
                "app_id": 12345,
                "reservation_owner": owner,
                "session_grant": "sg1.1780000000000." + "c" * 43,
                "session_attestation": current_grant(expected),
                **extra,
            }
        ).encode(), 200

    return respond


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status
        self.reason = "reason"
        self.headers = {}


class BrokerClientTests(unittest.TestCase):
    def test_expired_admission_latency_never_opens_oidc_or_post_request(self):
        for operation in ("oidc", "post"):
            with self.subTest(operation=operation):
                clock = [0.0]
                admissions = []
                dispatches = []

                def admit():
                    admissions.append(True)
                    clock[0] = 2.0
                    return 1.0

                def opener(request, timeout):
                    dispatches.append(request)
                    return FakeResponse(b'{"value":"assertion","token":"scoped"}')

                client = BrokerClient(
                    authority=current_authority(),
                    opener=opener,
                    timeout=1,
                    before_request=admit,
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
                        side_effect=lambda: clock[0],
                    ),
                    mock.patch(
                        "review_sensei_github.broker_client.time.time",
                        side_effect=lambda: 1000 + clock[0],
                    ),
                ):
                    with self.assertRaises(GitHubBrokerClientError):
                        client.request_oidc_token() if operation == "oidc" else client.exchange(
                            "assertion"
                        )
                self.assertEqual(len(admissions), 1)
                self.assertEqual(dispatches, [])

    def test_partial_admission_latency_uses_only_the_remaining_original_window(self):
        for operation in ("oidc", "post"):
            with self.subTest(operation=operation):
                clock = [0.0]
                admissions = []
                dispatches = []

                def admit():
                    admissions.append(True)
                    clock[0] = 0.25
                    return 1.0

                def opener(request, timeout):
                    dispatches.append((request, timeout))
                    return FakeResponse(b'{"value":"assertion","token":"scoped"}')

                client = BrokerClient(
                    authority=current_authority(),
                    opener=opener,
                    timeout=1,
                    before_request=admit,
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
                        side_effect=lambda: clock[0],
                    ),
                    mock.patch(
                        "review_sensei_github.broker_client.time.time",
                        side_effect=lambda: 1000 + clock[0],
                    ),
                ):
                    result = (
                        client.request_oidc_token()
                        if operation == "oidc"
                        else client.exchange("assertion")
                    )
                self.assertIn(result, ("assertion", "scoped"))
                self.assertEqual(len(admissions), 1)
                self.assertEqual(len(dispatches), 1)
                self.assertEqual(dispatches[0][1], 0.75)
                if operation == "post":
                    self.assertEqual(
                        dispatches[0][0].headers["X-reviewsensei-deadline-unix-ms"],
                        "1001000",
                    )
                    self.assertEqual(
                        json.loads(dispatches[0][0].data),
                        {
                            "oidc_token": "assertion",
                            "session_attestation": client.authority,
                        },
                    )

    def test_oidc_late_read_never_returns_assertion_after_transport_timeout(self):
        client = BrokerClient(
            authority=current_authority(),
            opener=lambda *args, **kwargs: FakeResponse(b'{"value":"secret"}'),
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
                side_effect=[0, 0, 31, 31],
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

        client = BrokerClient(
            authority=current_authority(), opener=opener, before_request=budget.consume
        )
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

    def test_failure_metrics_distinguish_preparation_socket_and_total_elapsed(self):
        calls = []

        def opener(request, timeout):
            calls.append(timeout)
            raise TimeoutError("PRIVATE-TOKEN-AND-REPOSITORY-CONTENT")

        client = BrokerClient(
            authority=current_authority(), opener=opener, before_request=lambda: 1
        )
        with mock.patch(
            "review_sensei_github.broker_client.time.monotonic",
            side_effect=[0.25, 0.75, 1.0],
        ):
            with self.assertRaises(GitHubBrokerClientError) as raised:
                client.exchange("PRIVATE-TOKEN-AND-REPOSITORY-CONTENT")
        message = str(raised.exception)
        for field in (
            "broker.elapsed_ms=750",
            "broker.elapsed_scope=request_with_preparation",
            "broker.admitted_timeout_ms=1000",
            "broker.preparation_ms=500",
            "broker.socket_timeout_ms=500",
            "broker.retry_count=0",
            "broker.post_outcome=unknown",
        ):
            self.assertIn(field, message)
        self.assertNotIn("PRIVATE-TOKEN", message)
        self.assertEqual(calls, [0.5])

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

                client = BrokerClient(authority=current_authority(), opener=opener)
                with mock.patch(
                    "review_sensei_github.broker_client.time.monotonic", return_value=0
                ):
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
            (b'{"token":"scoped"}', 200),
            before_request=lambda: 1.5,
            authority=current_authority("reply"),
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
            {
                "oidc_token": "assertion",
                "capability": "issue_reply",
                "session_attestation": client.authority,
            },
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

        client = BrokerClient(
            authority=current_authority(), opener=opener, before_request=budget.consume
        )
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
        client = BrokerClient(
            authority=current_authority(), opener=lambda *args, **kwargs: response
        )
        with mock.patch(
            "review_sensei_github.broker_client.time.monotonic",
            side_effect=[0, 0, 31, 31],
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
            if callable(response):
                response = response(json.loads(request.data))
            return FakeResponse(response[0], response[1])

        kwargs.setdefault("authority", current_authority())
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
        self.assertEqual(
            body, {"oidc_token": "oidc.token", "session_attestation": client.authority}
        )

    def test_exchange_posts_only_named_capability(self):
        client, calls = self.make_client((b'{"token":"ghs_capability"}', 200))
        self.assertEqual(
            client.exchange("oidc.token", capability="review_publish"),
            "ghs_capability",
        )
        body = json.loads(calls[0][3].decode("utf-8"))
        self.assertEqual(
            body,
            {
                "oidc_token": "oidc.token",
                "capability": "review_publish",
                "session_attestation": client.authority,
            },
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
            {
                "oidc_token": "oidc.token",
                "capability": "review_status",
                "session_attestation": client.authority,
            },
        )

    def test_exchange_accepts_separate_read_only_actions_capability(self):
        client, calls = self.make_client((b'{"token":"ghs_actions_read"}', 200))
        self.assertEqual(
            client.exchange("oidc.token", capability="review_actions"),
            "ghs_actions_read",
        )
        self.assertEqual(
            json.loads(calls[0][3].decode()),
            {
                "oidc_token": "oidc.token",
                "capability": "review_actions",
                "session_attestation": client.authority,
            },
        )

    def test_open_session_preserves_only_valid_broker_owner(self):
        for owner in (
            {"run_id": "123", "head_sha": "a" * 40},
            {"run_id": "123", "head_sha": "b" * 40},
            {"run_id": "invalid", "head_sha": "a" * 40},
        ):
            with self.subTest(owner=owner):
                client, _ = self.make_client(
                    session_response(current_authority(), owner=owner)
                )
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
                "app_id": 12345,
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
                        "app_id": 12345,
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
        authority = current_authority(repository_id=987654321)
        client, calls = self.make_client(
            session_response(authority, session_state="enrolled"), authority=authority
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
                "session_attestation": client.authority,
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
            ),
            authority=current_authority(repository_id=987654321),
        )
        with self.assertRaises(GitHubBrokerClientError):
            client.open_session(
                "oidc.token",
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
            )

    def test_session_mutation_grant_requires_broker_derived_actor_fields(self):
        request_attestation = current_authority("command", repository_id=987654321)
        client, calls = self.make_client(
            session_response(request_attestation), authority=request_attestation
        )
        grant = client.authorize_session_mutation(
            "oidc.token",
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            session_attestation=request_attestation,
        )
        self.assertEqual(
            grant.attestation["actor"],
            {"login": "octocat", "type": "User", "association": "OWNER"},
        )
        self.assertEqual(grant.attestation["source"]["id"], 13579)
        self.assertEqual(grant.app_id, 12345)
        self.assertNotIn("ghs_session", repr(grant))
        self.assertNotIn("sg1.1780000000000." + "c" * 43, repr(grant))
        payload = json.loads(calls[0][3].decode("utf-8"))
        self.assertEqual(payload["session_attestation"], client.authority)
        self.assertNotIn("actor", payload["session_attestation"])

        def missing_actor(payload):
            raw, status = session_response(request_attestation)(payload)
            document = json.loads(raw)
            del document["session_attestation"]["actor"]
            return json.dumps(document).encode(), status

        client, calls = self.make_client(missing_actor, authority=request_attestation)
        with self.assertRaises(GitHubBrokerClientError):
            client.authorize_session_mutation(
                "oidc.token",
                repository_id=987654321,
                pull_request=7,
                head_sha="a" * 40,
                session_attestation=request_attestation,
            )
        self.assertEqual(len(calls), 1)

    def test_obsolete_mutation_grants_refuse_before_any_transport(self):
        old_command = {
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
            "job_workflow_ref": current_authority()["job_workflow_ref"],
            "job_workflow_sha": "a" * 40,
        }
        old_feedback = json.loads(
            (
                Path(__file__).parent / "fixtures" / "feedback-session-attestation.json"
            ).read_text(encoding="utf-8")
        )["request"]
        for document, actor in (
            (
                old_command,
                {
                    "actor": "octocat",
                    "actor_type": "User",
                    "association": "OWNER",
                    "command_id": 13579,
                    "command_digest": "b" * 64,
                },
            ),
            (
                old_feedback,
                {
                    "actor": "octocat",
                    "actor_id": 12345678,
                    "actor_type": "User",
                    "association": "OWNER",
                },
            ),
        ):
            for operation in ("issue", "verify"):
                with self.subTest(version=document["version"], operation=operation):
                    client, calls = self.make_client((b"{}", 200))
                    with self.assertRaises(GitHubBrokerClientError):
                        if operation == "issue":
                            client.authorize_session_mutation(
                                "oidc.token",
                                repository_id=document["repository_id"],
                                pull_request=document["pull_request"],
                                head_sha=document["head_sha"],
                                session_attestation=document,
                            )
                        else:
                            client.verify_session_grant(
                                "sg1.1780000000000." + "c" * 43,
                                {**document, **actor},
                            )
                    self.assertEqual(calls, [])

    def test_missing_current_authority_refuses_before_budget_or_transport(self):
        admissions, physical = [], []
        client = BrokerClient(
            opener=lambda *args, **kwargs: physical.append(True),
            before_request=lambda: admissions.append(True),
        )
        with self.assertRaises(GitHubBrokerClientError):
            client.exchange("synthetic-oidc")
        self.assertEqual(admissions, [])
        self.assertEqual(physical, [])

    def test_session_app_identity_must_come_from_validated_broker_response(self):
        authority = current_authority()
        for app_id in (None, True, "12345", 0, -1):
            with self.subTest(app_id=app_id):
                client, calls = self.make_client(
                    session_response(authority, app_id=app_id)
                )
                with self.assertRaises(GitHubBrokerClientError):
                    client.open_session(
                        "synthetic-oidc",
                        repository_id=99,
                        pull_request=7,
                        head_sha="a" * 40,
                    )
                self.assertEqual(len(calls), 1)

    def test_broker_requests_send_reviewsensei_user_agent(self):
        attestation = current_grant(
            current_authority("command", repository_id=987654321)
        )
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
