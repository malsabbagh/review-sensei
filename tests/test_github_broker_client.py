import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from review_sensei.hosting.github import (
    BrokerClient,
    GitHubBrokerClientError,
    GitHubHTTPTransientError,
)


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status
        self.reason = "reason"
        self.headers = {}


class BrokerClientTests(unittest.TestCase):
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
            BrokerClient(timeout=0)

        client, _ = self.make_client((b"{}", 200))
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(GitHubBrokerClientError):
                client.request_oidc_token()

        with mock.patch.dict(
            "os.environ",
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_OIDC_TOKEN_REQUEST_URL": "https://token.actions.test",
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
                "ACTIONS_OIDC_TOKEN_REQUEST_URL": "https://token.actions.test",
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
                "ACTIONS_OIDC_TOKEN_REQUEST_URL": (
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
