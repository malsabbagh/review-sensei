from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei.evidence import evidence_digest
from review_sensei.hosting.github.broker_client import BrokerClient
from review_sensei.hosting.github.errors import GitHubBrokerClientError
from review_sensei.hosting.github.feedback_attestation import parse_feedback_attestation
from tests.test_github_broker_client import FakeResponse


class FeedbackAttestationTests(unittest.TestCase):
    def fixture(self):
        return json.loads(
            (
                Path(__file__).parent / "fixtures" / "feedback-session-attestation.json"
            ).read_text(encoding="utf-8")
        )

    def test_complete_unicode_metadata_and_grant_are_closed_and_body_free(self):
        fixture = self.fixture()
        request = parse_feedback_attestation(fixture["request"])
        self.assertGreater(fixture["selection"]["sources"][0]["body_bytes"], 4096)
        self.assertEqual(
            evidence_digest(fixture["selection"]),
            request["feedback"]["selection_digest"],
        )
        grant = {
            **request,
            "actor": "octocat",
            "actor_id": 12345678,
            "actor_type": "User",
            "association": "OWNER",
        }
        self.assertEqual(parse_feedback_attestation(grant, grant=True), grant)
        self.assertNotIn(
            fixture["selection"]["sources"][0]["body"],
            json.dumps(grant, ensure_ascii=False),
        )
        request["feedback"]["target_ids"].clear()
        self.assertEqual(len(fixture["request"]["feedback"]["target_ids"]), 1)

    def test_invalid_identity_original_accounting_and_ambient_fields_refuse(self):
        paths = [
            (("operation",), "command"),
            (("version",), True),
            (("feedback", "sources", 0, "kind"), []),
            (("feedback", "sources", 0, "author_id"), True),
            (("feedback", "sources", 0, "body"), "untrusted extra prose"),
            (("feedback", "target_ids"), ["d" * 16]),
            (("feedback", "event_key"), "f" * 64),
            (("mutation", "read_accounting", "calls"), 65),
            (("mutation", "read_accounting", "deadline_unix_ms"), 0),
            (("mutation", "root_generation"), -1),
            (("mutation", "execution_identity"), "x" * 32),
        ]
        for path, invalid in paths:
            with self.subTest(path=path):
                value = copy.deepcopy(self.fixture()["request"])
                current = value
                for key in path[:-1]:
                    current = current[key]
                current[path[-1]] = invalid
                with self.assertRaises(GitHubBrokerClientError):
                    parse_feedback_attestation(value)

    def test_trigger_id_is_an_integer_even_when_float_equality_and_digest_match(self):
        request = self.fixture()["request"]
        request["feedback"]["trigger"]["comment_id"] = float(
            request["source_comment_id"]
        )
        request["feedback"]["event_key"] = evidence_digest(
            {
                "domain": "reviewsensei:feedback-event:v1",
                "repository": request["repository"],
                "pull_request": request["pull_request"],
                "trigger": {
                    "kind": "issue",
                    "comment_id": float(request["source_comment_id"]),
                },
            }
        )
        with self.assertRaises(GitHubBrokerClientError):
            parse_feedback_attestation(request)

    def test_valid_leading_bom_metadata_is_preserved_and_enums_are_scalar(self):
        request = self.fixture()["request"]
        request["feedback"]["sources"][1]["author"] = "\ufeffcollaborator"
        self.assertEqual(
            parse_feedback_attestation(request)["feedback"]["sources"][1]["author"],
            "\ufeffcollaborator",
        )
        for path, value in (
            (("mutation", "reason"), ["dispatch"]),
            (("feedback", "sources", 0, "association"), ["OWNER"]),
            (("feedback", "sources", 0, "kind"), ["issue"]),
            (("feedback", "trigger", "kind"), ["issue"]),
        ):
            with self.subTest(path=path):
                invalid = copy.deepcopy(self.fixture()["request"])
                current = invalid
                for key in path[:-1]:
                    current = current[key]
                current[path[-1]] = value
                with self.assertRaises(GitHubBrokerClientError):
                    parse_feedback_attestation(invalid)

    def test_client_charges_each_oidc_issue_and_verify_before_dispatch(self):
        request = self.fixture()["request"]
        grant = {
            **request,
            "actor": "octocat",
            "actor_id": 12345678,
            "actor_type": "User",
            "association": "OWNER",
        }
        charges = []
        dispatches = []

        def before_request():
            charges.append(len(charges) + 1)
            return 7.0 if len(charges) <= 3 else 0.0

        def opener(http_request, timeout):
            self.assertEqual(timeout, 7.0)
            self.assertEqual(len(charges), len(dispatches) + 1)
            dispatches.append(http_request.method)
            if http_request.method == "GET":
                response = {"value": "fresh-synthetic-assertion"}
            elif http_request.full_url.endswith("session-grant"):
                response = {"session_attestation": grant}
            else:
                response = {
                    "token": "synthetic-token",
                    "session_grant": "g" * 43,
                    "session_state": "known",
                    "session_attestation": grant,
                }
            return FakeResponse(json.dumps(response).encode())

        client = BrokerClient(opener=opener, before_request=before_request)
        with patch.dict(
            "os.environ",
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example.test?request=1",
            },
            clear=True,
        ):
            assertion = client.request_oidc_token()
        issued = client.authorize_session_mutation(
            assertion,
            repository_id=987654321,
            pull_request=7,
            head_sha="a" * 40,
            session_attestation=request,
        )
        self.assertEqual(
            client.verify_session_grant(issued.grant, issued.attestation), grant
        )
        with self.assertRaises(GitHubBrokerClientError):
            client.verify_session_grant(issued.grant, issued.attestation)
        self.assertEqual(dispatches, ["GET", "POST", "POST"])
        self.assertEqual(charges, [1, 2, 3, 4])

    def test_grant_actor_cannot_replace_live_selected_numeric_identity(self):
        request = self.fixture()["request"]
        grant = {
            **request,
            "actor": "octocat",
            "actor_id": 999,
            "actor_type": "User",
            "association": "OWNER",
        }
        with self.assertRaises(GitHubBrokerClientError):
            parse_feedback_attestation(grant, grant=True)
