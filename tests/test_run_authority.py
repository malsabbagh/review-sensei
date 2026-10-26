"""Closed v3 request/grant parsing and mocked physical broker dispatches."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.run_authority import (
    AUDIENCE,
    CAPABILITIES,
    REQUEST_KEYS,
    parse_authority,
    request_authority,
)

NOW = 1_700_000_000
MAX_INTEGER = 9_007_199_254_740_991
HEAD = "b" * 40
WORKFLOW = "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5"
SOURCE_TIME = "2026-10-26T00:00:00Z"
BODY = "@reviewsensei verify"
OPAQUE_GRANT = f"sg1.{NOW * 1000}." + "a" * 43


def authority(purpose="review", *, job=None, kind="issue_comment"):
    return {
        "version": 3,
        "purpose": purpose,
        "repository": "owner/repo",
        "repository_id": 123,
        "pull_request": 7,
        "head_sha": HEAD,
        "run_id": "456",
        "run_attempt": 1,
        "job": job or ("command" if purpose == "command" else "hosted"),
        "job_workflow_ref": WORKFLOW,
        "job_workflow_sha": "c" * 40,
        "issued_at": NOW,
        "expires_at": NOW + 600,
        "audience": AUDIENCE,
        "source": (
            {"kind": "pull_request", "id": 7, "updated_at": None, "body_sha256": None}
            if purpose == "review"
            else {
                "kind": kind,
                "id": 81,
                "updated_at": SOURCE_TIME,
                "body_sha256": hashlib.sha256(BODY.encode()).hexdigest(),
            }
        ),
    }


def granted(request):
    return {
        **copy.deepcopy(request),
        "actor": (
            None
            if request["purpose"] == "review"
            else {"login": "alice", "type": "User", "association": "OWNER"}
        ),
    }


class Response(io.BytesIO):
    def __init__(self, document):
        super().__init__(json.dumps(document).encode())
        self.status = 200
        self.headers = {}


class BrokerFixture:
    def __init__(self, request, reply=None):
        self.calls = []
        self.admissions = []
        self.reply = reply
        self.client = BrokerClient(
            opener=self.open,
            authority=request,
            before_request=self.admit,
        )

    def admit(self):
        self.admissions.append(True)
        return 30

    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.calls.append((request, payload))
        self.assert_request(request, timeout)
        document = {
            "token": "synthetic-installation-token",
            "capability": payload.get("capability", "review_publish"),
            "session_state": "known",
            "app_id": 999,
            "reservation_owner": None,
            "session_grant": OPAQUE_GRANT,
            "session_attestation": (
                copy.deepcopy(payload["session_attestation"])
                if request.full_url.endswith("/session-grant")
                else granted(payload["session_attestation"])
            ),
        }
        if self.reply is not None:
            document = self.reply(document)
        return Response(document)

    @staticmethod
    def assert_request(request, timeout):
        assert request.get_method() == "POST"
        assert 0 < timeout <= 30
        assert request.full_url.startswith("https://github.reviewsensei.dev/")


class CurrentAuthorityParserTests(unittest.TestCase):
    def test_valid_requests_and_grants_are_exact_detached_documents(self):
        for purpose, job, kind in (
            ("review", "hosted", "issue_comment"),
            ("review", "local", "issue_comment"),
            ("command", "command", "issue_comment"),
            ("reply", "hosted", "issue_comment"),
            ("reply", "local", "review_comment"),
        ):
            for grant in (False, True):
                with self.subTest(purpose=purpose, job=job, kind=kind, grant=grant):
                    request = authority(purpose, job=job, kind=kind)
                    document = granted(request) if grant else request
                    parsed = parse_authority(document, grant=grant)
                    self.assertEqual(parsed, document)
                    self.assertIsNot(parsed, document)
                    self.assertIsNot(parsed["source"], document["source"])
                    document["source"]["id"] = 999
                    self.assertNotEqual(parsed, document)

    def test_unknown_top_level_nested_and_obsolete_documents_refuse(self):
        for purpose in CAPABILITIES:
            for grant in (False, True):
                original = granted(authority(purpose)) if grant else authority(purpose)
                mutations = []
                for key in ("unknown", "read_accounting", "control_budget"):
                    value = copy.deepcopy(original)
                    value[key] = {}
                    mutations.append(value)
                    value = copy.deepcopy(original)
                    value["source"][key] = {}
                    mutations.append(value)
                for version in (1, 2, True, False, "3", 3.0, [], {}):
                    value = copy.deepcopy(original)
                    value["version"] = version
                    mutations.append(value)
                for missing in original:
                    value = copy.deepcopy(original)
                    del value[missing]
                    mutations.append(value)
                if grant and purpose != "review":
                    value = copy.deepcopy(original)
                    value["actor"]["read_accounting"] = {}
                    mutations.append(value)
                for document in mutations:
                    with self.subTest(purpose=purpose, grant=grant, document=document):
                        with self.assertRaises(GitHubBrokerClientError):
                            parse_authority(document, grant=grant)

    def test_malformed_unhashable_json_values_raise_typed_errors(self):
        for purpose in CAPABILITIES:
            for grant in (False, True):
                original = granted(authority(purpose)) if grant else authority(purpose)
                for key in original:
                    for malformed in ([], {}, True, None):
                        if key == "actor" and purpose == "review" and malformed is None:
                            continue
                        document = copy.deepcopy(original)
                        document[key] = malformed
                        with self.subTest(purpose=purpose, grant=grant, key=key, value=malformed):
                            with self.assertRaises(GitHubBrokerClientError):
                                parse_authority(document, grant=grant)
                for key in original["source"]:
                    for malformed in ([], {}):
                        document = copy.deepcopy(original)
                        document["source"][key] = malformed
                        with self.subTest(purpose=purpose, grant=grant, source=key):
                            with self.assertRaises(GitHubBrokerClientError):
                                parse_authority(document, grant=grant)
                if grant and purpose != "review":
                    for key in original["actor"]:
                        document = copy.deepcopy(original)
                        document["actor"][key] = []
                        with self.subTest(purpose=purpose, actor=key):
                            with self.assertRaises(GitHubBrokerClientError):
                                parse_authority(document, grant=True)

    def test_nonserializable_nested_values_raise_typed_errors(self):
        for malformed in ({"not-json"}, b"not-json"):
            document = authority("command")
            document["source"]["updated_at"] = malformed
            with self.subTest(value=malformed):
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(document)

    def test_integer_timestamp_and_lifetime_boundaries(self):
        for key in ("repository_id", "pull_request", "issued_at", "expires_at"):
            for value in (True, False, 0, -1, MAX_INTEGER + 1, 1.0, "1"):
                document = authority("command")
                document[key] = value
                with self.subTest(key=key, value=value):
                    with self.assertRaises(GitHubBrokerClientError):
                        parse_authority(document)
        for value in (True, False, 0, -1, MAX_INTEGER + 1, 1.0, "1"):
            document = authority("command")
            document["source"]["id"] = value
            with self.subTest(source_id=value):
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(document)
        document = authority("command")
        document["source"]["id"] = MAX_INTEGER
        self.assertEqual(parse_authority(document), document)
        for duration in (1, 600):
            document = authority()
            document.update(issued_at=MAX_INTEGER-duration, expires_at=MAX_INTEGER)
            self.assertEqual(parse_authority(document), document)
        for duration in (-1, 0, 601):
            document = authority()
            document["expires_at"] = document["issued_at"] + duration
            with self.subTest(duration=duration):
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(document)
        for attempt in (True, False, 0, 3, 1.0, "1", [], {}):
            document = authority()
            document["run_attempt"] = attempt
            with self.subTest(attempt=attempt):
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(document)
        document = authority()
        document["run_attempt"] = 2
        self.assertEqual(parse_authority(document), document)

    def test_purpose_job_source_and_actor_scope_must_match(self):
        for purpose, job, kind in (
            ("review", "command", "issue_comment"),
            ("command", "hosted", "issue_comment"),
            ("command", "command", "review_comment"),
            ("reply", "command", "issue_comment"),
            ("reply", "local", "pull_request"),
        ):
            with self.subTest(purpose=purpose, job=job, kind=kind):
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(authority(purpose, job=job, kind=kind))
        for purpose in CAPABILITIES:
            document = granted(authority(purpose))
            document["actor"] = (
                {"login": "alice", "type": "User", "association": "OWNER"}
                if purpose == "review" else None
            )
            with self.subTest(purpose=purpose):
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(document, grant=True)
        for field, value in (("association", "NONE"), ("type", "Bot"), ("login", "")):
            document = granted(authority("command"))
            document["actor"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(document, grant=True)

    def test_request_builder_uses_current_run_and_preserves_comment_source(self):
        env = {
            "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "2",
            "REVIEW_SENSEI_WORKFLOW_REF": WORKFLOW,
            "REVIEW_SENSEI_WORKFLOW_SHA": "c" * 40,
        }
        for purpose in CAPABILITIES:
            document = authority(purpose)
            with patch.dict("os.environ", env, clear=True), patch(
                "review_sensei_github.run_authority.time.time", return_value=NOW
            ):
                built = request_authority(
                    purpose=purpose, repository="owner/repo", repository_id=123,
                    pull_request=7, head_sha=HEAD, job=document["job"],
                    source=None if purpose == "review" else document["source"],
                )
            self.assertEqual(built, {**document, "run_attempt": 2})


class CurrentBrokerAuthorityTests(unittest.TestCase):
    def test_all_valid_purpose_capabilities_send_current_request(self):
        for purpose, capabilities in CAPABILITIES.items():
            for capability in sorted(capabilities):
                with self.subTest(purpose=purpose, capability=capability):
                    request = authority(purpose)
                    fixture = BrokerFixture(request)
                    with patch("review_sensei_github.broker_client.time.time", return_value=NOW):
                        token = fixture.client.exchange("synthetic-oidc", capability=capability)
                    self.assertEqual(token, "synthetic-installation-token")
                    self.assertEqual(len(fixture.calls), 1)
                    self.assertEqual(len(fixture.admissions), 1)
                    payload = fixture.calls[0][1]
                    self.assertEqual(payload, {
                        "oidc_token": "synthetic-oidc", "capability": capability,
                        "session_attestation": request,
                    })

    def test_unknown_obsolete_or_nested_accounting_refuses_before_post(self):
        documents = []
        for version in (1, 2, True):
            documents.append({**authority(), "version": version})
        documents.append({**authority(), "read_accounting": {}})
        documents.append({**authority(), "source": {**authority()["source"], "read_accounting": {}}})
        for request in documents:
            for route in ("construct", "authorize", "verify"):
                calls = []
                client = BrokerClient(opener=lambda *a, **k: calls.append(True))
                with self.subTest(route=route, request=request):
                    with self.assertRaises(GitHubBrokerClientError):
                        if route == "construct":
                            BrokerClient(authority=request, opener=client.opener)
                        elif route == "authorize":
                            client.authorize_session_mutation(
                                "synthetic-oidc", repository_id=123, pull_request=7,
                                head_sha=HEAD, session_attestation=request,
                            )
                        else:
                            client.verify_session_grant(OPAQUE_GRANT, granted(request))
                    self.assertEqual(calls, [])

    def test_wrong_purpose_capability_refuses_before_admission_or_post(self):
        for purpose, capability in (
            ("review", "inline_reply"), ("review", "issue_reply"),
            ("command", "learning_write"), ("command", "inline_reply"),
            ("reply", "learning_write"),
        ):
            fixture = BrokerFixture(authority(purpose))
            with self.subTest(purpose=purpose, capability=capability):
                with self.assertRaises(GitHubBrokerClientError):
                    fixture.client.exchange("synthetic-oidc", capability=capability)
                self.assertEqual(fixture.calls, [])
                self.assertEqual(fixture.admissions, [])

    def test_malformed_capability_refuses_with_typed_error_before_post(self):
        for capability in ([], {}, True, "unknown"):
            fixture = BrokerFixture(authority())
            with self.subTest(capability=capability):
                with self.assertRaises(GitHubBrokerClientError):
                    fixture.client.exchange("synthetic-oidc", capability=capability)
                self.assertEqual(fixture.calls, [])
                self.assertEqual(fixture.admissions, [])

    def test_open_session_returns_exact_current_grant_for_each_purpose(self):
        for purpose in CAPABILITIES:
            request = authority(purpose)
            fixture = BrokerFixture(request)
            with self.subTest(purpose=purpose), patch(
                "review_sensei_github.broker_client.time.time", return_value=NOW
            ):
                session = fixture.client.open_session(
                    "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
                )
            self.assertEqual(session.attestation, granted(request))
            self.assertEqual(session.grant, OPAQUE_GRANT)
            self.assertEqual(fixture.calls[0][1]["session"], {
                "repository_id": 123, "pull_request": 7, "head_sha": HEAD,
            })

    def test_returned_grant_scope_mismatch_refuses(self):
        mutations = {
            "repository": "other/repo", "repository_id": 124,
            "pull_request": 8, "head_sha": "d" * 40, "run_id": "457",
            "run_attempt": 2, "job": "local", "job_workflow_sha": "e" * 40,
            "job_workflow_ref": "other/repo/.github/workflows/review-sensei-run.yml@v5",
            "issued_at": NOW-1, "expires_at": NOW+599,
            "source": {"kind": "pull_request", "id": 8, "updated_at": None, "body_sha256": None},
        }
        for key, value in mutations.items():
            def reply(document):
                document["session_attestation"][key] = value
                return document
            fixture = BrokerFixture(authority(), reply)
            with self.subTest(key=key), patch(
                "review_sensei_github.broker_client.time.time", return_value=NOW
            ):
                with self.assertRaises(GitHubBrokerClientError):
                    fixture.client.open_session(
                        "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
                    )
                self.assertEqual(len(fixture.calls), 1)

    def test_malformed_returned_state_has_typed_error(self):
        for route in ("open", "authorize"):
            for state in ([], {}, True, None, "unknown"):
                fixture = BrokerFixture(authority(), lambda d: {**d, "session_state": state})
                with self.subTest(route=route, state=state), patch(
                    "review_sensei_github.broker_client.time.time", return_value=NOW
                ):
                    with self.assertRaises(GitHubBrokerClientError):
                        if route == "open":
                            fixture.client.open_session(
                                "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
                            )
                        else:
                            fixture.client.authorize_session_mutation(
                                "synthetic-oidc", repository_id=123, pull_request=7,
                                head_sha=HEAD, session_attestation=authority(),
                            )
                    self.assertEqual(len(fixture.calls), 1)

    def test_verify_grant_requires_exact_returned_actor_and_source(self):
        for purpose in CAPABILITIES:
            request = authority(purpose)
            grant = granted(request)
            fixture = BrokerFixture(request)
            self.assertEqual(fixture.client.verify_session_grant(OPAQUE_GRANT, grant), grant)
            url_request, payload = fixture.calls[0]
            self.assertTrue(url_request.full_url.endswith("/session-grant"))
            self.assertEqual(payload, {"session_grant": OPAQUE_GRANT, "session_attestation": grant})
        for field, value in (("actor", {"login": "bob", "type": "User", "association": "OWNER"}),
                             ("source", {**authority("command")["source"], "id": 82})):
            fixture = BrokerFixture(authority("command"), lambda d: {
                **d, "session_attestation": {**d["session_attestation"], field: value},
            })
            with self.subTest(field=field):
                with self.assertRaises(GitHubBrokerClientError):
                    fixture.client.verify_session_grant(OPAQUE_GRANT, granted(authority("command")))

    def test_exchange_refresh_changes_only_request_age(self):
        for purpose in CAPABILITIES:
            original = authority(purpose)
            fixture = BrokerFixture(original)
            for now in (NOW+10, NOW+20):
                with patch("review_sensei_github.broker_client.time.time", return_value=now):
                    fixture.client.exchange("synthetic-oidc", capability="review_status")
                sent = fixture.calls[-1][1]["session_attestation"]
                self.assertEqual(sent, {**original, "issued_at": now, "expires_at": now+600})
                self.assertEqual(set(sent), REQUEST_KEYS)
            self.assertEqual(original, authority(purpose))

    def test_mutation_authorization_accepts_refreshed_exact_request_grant(self):
        for purpose in CAPABILITIES:
            request = authority(purpose)
            fixture = BrokerFixture(request)
            with self.subTest(purpose=purpose), patch(
                "review_sensei_github.broker_client.time.time", return_value=NOW+10
            ):
                result = fixture.client.authorize_session_mutation(
                    "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD,
                    session_attestation=request,
                )
                self.assertEqual(result.attestation, granted({
                    **request, "issued_at": NOW+10, "expires_at": NOW+610,
                }))


if __name__ == "__main__":
    unittest.main()
