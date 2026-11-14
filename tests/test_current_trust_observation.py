"""Opt-in v4 parsing preserves observation scope without inventing trust history."""

import copy
import unittest
from unittest.mock import patch

from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.run_authority import parse_authority

from tests.test_run_authority import HEAD, NOW, BrokerFixture, authority, granted


def observed(purpose="command"):
    request = {**authority(purpose), "version": 4}
    grant = granted(request)
    grant["current_observation"] = {
        "schema_version": "current-observation-v1",
        "repository": {
            "id": 123,
            "full_name": "owner/repo",
            "owner_id": 42,
            "owner_login": "owner",
            "fork": False,
        },
        "installation": {
            "id": 99,
            "repository_selection": "selected",
            "repository_access": True,
            "permissions": {"metadata": "read", "pull_requests": "write"},
        },
        "capability": {
            "name": "review_session",
            "permissions": {"pull_requests": "write", "metadata": "read"},
        },
        "actor": {
            "id": 101,
            "login": "alice",
            "source_association": None if purpose == "review" else "OWNER",
        },
    }
    return request, grant


class CurrentTrustObservationTests(unittest.TestCase):
    def test_signed_session_exchange_retains_exact_observation_and_binding(self):
        request, grant = observed()

        def response(document):
            document["session_attestation"]["current_observation"] = copy.deepcopy(
                grant["current_observation"]
            )
            return document

        fixture = BrokerFixture(request, response)
        with patch("review_sensei_github.broker_client.time.time", return_value=NOW):
            session = fixture.client.open_session(
                "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
            )
            verified = fixture.client.verify_session_grant(
                session.grant, session.attestation
            )
        self.assertEqual(verified, grant)
        self.assertEqual(len(fixture.calls), 2)
        self.assertEqual(len(fixture.admissions), 2)
        for key, value in (("run_id", "999"), ("head_sha", "a" * 40), ("version", 3)):

            def changed(document, key=key, value=value):
                document = response(document)
                document["session_attestation"][key] = value
                return document

            bad = BrokerFixture(request, changed)
            with self.subTest(key=key), self.assertRaises(GitHubBrokerClientError):
                bad.client.open_session(
                    "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
                )

    def test_versioned_original_role_is_explicit_closed_and_round_trips(self):
        from review_sensei_github.trust_enrollment import prepare_original_enrollment

        from review_sensei.session import SessionIdentity, SessionRecord

        request, grant = observed()
        observation = grant["current_observation"]
        observation["schema_version"] = "current-observation-v2"
        observation["actor"].update(
            repository_permission="write", repository_role="maintain"
        )
        self.assertEqual(parse_authority(grant, grant=True), grant)

        def response(document):
            document["session_attestation"]["current_observation"] = copy.deepcopy(
                observation
            )
            return document

        fixture = BrokerFixture(request, response)
        with patch("review_sensei_github.trust_enrollment.time.time", return_value=NOW):
            session = fixture.client.open_session(
                "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
            )
            record = prepare_original_enrollment(
                fixture.client,
                session,
                SessionRecord.create(
                    SessionIdentity("owner/repo", 7, repository_id=123)
                ),
            )
        restored = SessionRecord.from_dict(record.to_dict())
        self.assertEqual(restored.trust_enrollment, record.trust_enrollment)
        for key, value in (
            ("repository_permission", True),
            ("repository_role", ""),
            ("repository_permission", "maintain"),
        ):
            bad = copy.deepcopy(grant)
            bad["current_observation"]["actor"][key] = value
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(GitHubBrokerClientError),
            ):
                parse_authority(bad, grant=True)
        for version in ("current-observation-v1", "current-observation-v3"):
            bad = copy.deepcopy(grant)
            bad["current_observation"]["schema_version"] = version
            with self.assertRaises(GitHubBrokerClientError):
                parse_authority(bad, grant=True)
        for key in ("repository_role", "repository_permission"):
            bad = copy.deepcopy(grant)
            del bad["current_observation"]["actor"][key]
            with self.assertRaises(GitHubBrokerClientError):
                parse_authority(bad, grant=True)

    def test_unsigned_capability_and_release_routes_refuse_before_dispatch(self):
        request, _ = observed()
        for capability in ("review_publish", "review_session", "check_publish"):
            fixture = BrokerFixture(request)
            with (
                self.subTest(capability=capability),
                self.assertRaises(GitHubBrokerClientError),
            ):
                fixture.client.exchange("synthetic-oidc", capability=capability)
            self.assertEqual(fixture.calls, [])
            self.assertEqual(fixture.admissions, [])
        fixture = BrokerFixture(request)
        with self.assertRaises(GitHubBrokerClientError):
            fixture.client.open_session_release(
                "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
            )
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.admissions, [])

    def test_verification_cannot_substitute_otherwise_valid_observations(self):
        request, grant = observed()
        for section, field, value in (
            ("repository", "owner_id", 43),
            ("installation", "id", 100),
            ("actor", "id", 102),
        ):

            def response(document, section=section, field=field, value=value):
                document["session_attestation"]["current_observation"][section][
                    field
                ] = value
                return document

            fixture = BrokerFixture(request, response)
            with (
                self.subTest(section=section),
                self.assertRaises(GitHubBrokerClientError),
            ):
                fixture.client.verify_session_grant(
                    "sg1.1700000000000." + "a" * 43, grant
                )
            self.assertEqual(len(fixture.calls), 1)
            self.assertEqual(len(fixture.admissions), 1)

    def test_v3_unchanged_v4_is_closed_and_detached(self):
        for purpose in ("review", "command", "reply"):
            old = granted(authority(purpose))
            self.assertEqual(parse_authority(old, grant=True), old)
            request, value = observed(purpose)
            self.assertEqual(parse_authority(request), request)
            parsed = parse_authority(value, grant=True)
            self.assertEqual(parsed, value)
            parsed["current_observation"]["repository"]["owner_id"] = 999
            self.assertEqual(value["current_observation"]["repository"]["owner_id"], 42)
            for bad in (
                {**request, "current_observation": value["current_observation"]},
                {**value, "version": 3},
                {
                    key: item
                    for key, item in value.items()
                    if key != "current_observation"
                },
            ):
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(bad, grant="actor" in bad)

    def test_invalid_scope_identity_permissions_and_unproven_history_refuse(self):
        _, original = observed()
        mutations = [
            ("repository", "id", 999),
            ("repository", "id", True),
            ("repository", "full_name", "other/repo"),
            ("repository", "owner_login", "other"),
            ("repository", "owner_id", 0),
            ("repository", "fork", True),
            ("installation", "id", True),
            ("installation", "repository_selection", "unknown"),
            ("installation", "repository_access", False),
            (
                "installation",
                "permissions",
                {"metadata": "read", "pull_requests": "read"},
            ),
            ("capability", "name", "review_publish"),
            (
                "capability",
                "permissions",
                {"metadata": "read", "pull_requests": "write", "contents": "write"},
            ),
            (
                "capability",
                "permissions",
                {"metadata": "admin", "pull_requests": "write"},
            ),
            ("capability", "permissions", {}),
            ("actor", "id", (1 << 53)),
            ("actor", "login", "other"),
            ("actor", "source_association", "MEMBER"),
        ]
        for section, key, value in mutations:
            with self.subTest(section=section, key=key, value=value):
                bad = copy.deepcopy(original)
                bad["current_observation"][section][key] = value
                with self.assertRaises(GitHubBrokerClientError):
                    parse_authority(bad, grant=True)
        for key, value in (("permission_epoch", 1), ("history_complete", True)):
            bad = copy.deepcopy(original)
            bad["current_observation"][key] = value
            with self.assertRaises(GitHubBrokerClientError):
                parse_authority(bad, grant=True)

    def test_observation_is_not_a_standalone_authority(self):
        _, grant = observed()
        with self.assertRaises(GitHubBrokerClientError):
            parse_authority(grant["current_observation"], grant=True)
        for field, value in (
            ("head_sha", "z" * 40),
            ("run_id", "0"),
            ("expires_at", grant["issued_at"]),
        ):
            bad = {**grant, field: value}
            with self.assertRaises(GitHubBrokerClientError):
                parse_authority(bad, grant=True)
