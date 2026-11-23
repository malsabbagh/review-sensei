"""Ordinary opt-in CLI enrollment and cold command admission, no live service."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import cli as package_cli
from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import ReviewPublisher
from review_sensei_github.run_authority import parse_authority
from review_sensei_github.session_ledger import parse_session_comment
from review_sensei_github.trust_enrollment import (
    _snapshot_from_authority,
    enroll_application_trust_snapshot,
    enrolled_trust_snapshot_from_record,
    observed_trust_snapshot_from_authority,
)
from test_action_run import CommandEntryFixture
from test_api_work_persistence import IDENTITY

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionRecord


class ObservedEntryFixture(CommandEntryFixture):
    def __init__(self, directory):
        super().__init__(directory, limit=512)
        self.observed_installation = 99
        self.observed_permissions = {
            "metadata": "read",
            "pull_requests": "write",
            "contents": "read",
            "issues": "write",
            "checks": "write",
        }
        self.observed_pr_repo_override = None
        self.observed_schema_version = "current-observation-v1"
        self.observed_actor_permission = None
        self.observed_actor_role = None
        self.observed_actor_role_requests = []
        self.observed_requests = []

    def select_command(self, *args, **kwargs):
        super().select_command(*args, **kwargs)
        # The original authenticated observation below identifies actor 101.
        # Its native source and event must describe that same account.
        self.source["user"]["id"] = 101

    def external(self, request, timeout):
        if urlsplit(request.full_url).hostname == "broker.github.test" and request.data:
            payload = json.loads(request.data)
            authority = payload.get("session_attestation", {})
            if authority.get("version") == 4:
                self._meter(request)
                verifying = urlsplit(request.full_url).path.endswith("/session-grant")
                authority = parse_authority(authority, grant=verifying)
                self.observed_requests.append(
                    ("verify" if verifying else payload["capability"], 4)
                )
                if not verifying:
                    self.observed_actor_role_requests.append(
                        bool(payload.get("observe_actor_role"))
                    )
                receipt = {
                    **payload["control_budget"],
                    "dispatched": min(
                        self.fanout, payload["control_budget"]["max_dispatches"]
                    ),
                }
                if verifying:
                    return json_response(
                        {"session_attestation": authority, "control_receipt": receipt}
                    )
                assert payload["capability"] == "review_session"
                actor = self._actor() if self.command_active else None
                actor_obs = {
                    "id": 101,
                    "login": "alice",
                    "source_association": actor["association"] if actor else None,
                }
                if self.observed_schema_version == "current-observation-v2":
                    actor_obs["repository_permission"] = (
                        self.observed_actor_permission or "write"
                    )
                    actor_obs["repository_role"] = (
                        self.observed_actor_role or "maintain"
                    )
                observation = {
                    "schema_version": self.observed_schema_version,
                    "repository": {
                        "id": 123,
                        "full_name": "owner/repo",
                        "owner_id": 42,
                        "owner_login": "owner",
                        "fork": False,
                    },
                    "installation": {
                        "id": self.observed_installation,
                        "repository_selection": "selected",
                        "repository_access": True,
                        "permissions": dict(self.observed_permissions),
                    },
                    "capability": {
                        "name": "review_session",
                        "permissions": {"metadata": "read", "pull_requests": "write"},
                    },
                    "actor": actor_obs,
                }
                return json_response(
                    {
                        "token": "synthetic-review_session",
                        "capability": "review_session",
                        "control_receipt": receipt,
                        "session_state": "known" if self.comment else "enrolled",
                        "app_id": 999,
                        "reservation_owner": {
                            "run_id": str(self.run_id),
                            "head_sha": self.requested_head,
                        },
                        "session_grant": "sg1.9999999999999." + "A" * 43,
                        "session_attestation": {
                            **authority,
                            "actor": actor,
                            "current_observation": observation,
                        },
                    }
                )
        response = super().external(request, timeout)
        if (
            request.get_method() == "GET"
            and urlsplit(request.full_url).path == "/repos/owner/repo/pulls/7"
        ):
            pull = json.loads(response.read(512 * 1024))
            repo_fields = (
                dict(self.observed_pr_repo_override)
                if self.observed_pr_repo_override is not None
                else {
                    "id": 123,
                    "full_name": "owner/repo",
                    "fork": False,
                    "owner": {"id": 42, "login": "owner"},
                }
            )
            pull.setdefault("base", {}).setdefault("repo", {}).update(repo_fields)
            return json_response(pull, response.status)
        return response

    def opted(self, call):
        original = package_cli.main

        def invoke(argv):
            return original([argv[0], "--enable-current-trust-enrollment", *argv[1:]])

        with patch.object(package_cli, "main", side_effect=invoke):
            return call()

    def opted_with_actor_role(self, call):
        original = package_cli.main

        def invoke(argv):
            return original(
                [
                    argv[0],
                    "--enable-current-trust-enrollment",
                    "--observe-original-actor-role",
                    *argv[1:],
                ]
            )

        with patch.object(package_cli, "main", side_effect=invoke):
            return call()

    def only_actor_role_opt_in(self, call):
        original = package_cli.main

        def invoke(argv):
            return original([argv[0], "--observe-original-actor-role", *argv[1:]])

        with patch.object(package_cli, "main", side_effect=invoke):
            return call()


class TrustEnrollmentEntryTests(unittest.TestCase):
    def test_actual_opted_review_then_cold_command_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            self.assertEqual(fixture.opted(fixture.invoke), 0, fixture.error)
            first = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            original = copy.deepcopy(first.trust_enrollment)
            self.assertIsNotNone(original)
            provider_calls = fixture.provider_calls
            fixture.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(fixture.opted(fixture.invoke_command), 0, fixture.error)
            current = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(current.trust_enrollment, original)
            self.assertEqual(fixture.provider_calls, provider_calls)
            self.assertEqual(current.api_run_accounting["configuration"]["limit"], 512)
            self.assertTrue(fixture.observed_requests)
            self.assertTrue(
                all(
                    capability in {"review_session", "verify"}
                    for capability, _ in fixture.observed_requests
                )
            )

    def test_enrollment_uses_existing_dispatch_and_original_allowance(self):
        traces = []
        for opted in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                fixture = ObservedEntryFixture(directory)
                code = fixture.opted(fixture.invoke) if opted else fixture.invoke()
                self.assertEqual(code, 0, fixture.error)
                traces.append([(method, path) for _, method, path in fixture.trace])
                current = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                self.assertEqual(
                    current.api_run_accounting["configuration"]["limit"], 512
                )
        self.assertEqual(traces[0], traces[1])

    def test_enrolled_toggle_off_and_changed_installation_refuse_before_business_writes(
        self,
    ):
        for change in ("toggle-off", "installation"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = ObservedEntryFixture(directory)
                self.assertEqual(fixture.opted(fixture.invoke), 0, fixture.error)
                before = copy.deepcopy(fixture.comment)
                fixture.select_command("@reviewsensei review status", new_run=True)
                if change == "installation":
                    fixture.observed_installation = 100
                start = len(fixture.trace)
                call = (
                    fixture.invoke_command
                    if change == "toggle-off"
                    else lambda: fixture.opted(fixture.invoke_command)
                )
                self.assertEqual(call(), 1, fixture.error)
                self.assertIn("trust", fixture.error)
                self.assertEqual(fixture.comment, before)
                self.assertFalse(
                    any(
                        method != "GET" and path.startswith("/repos/")
                        for _, method, path in fixture.trace[start:]
                    )
                )

    def test_legacy_existing_root_is_not_reenrolled_by_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            self.assertEqual(fixture.invoke(), 0, fixture.error)
            before = copy.deepcopy(fixture.comment)
            fixture.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(fixture.opted(fixture.invoke_command), 1, fixture.error)
            self.assertIn("legacy migration required", fixture.error)
            self.assertEqual(fixture.comment, before)

    def test_enrolled_trust_snapshot_from_record_contract_and_tamper_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            self.assertEqual(fixture.opted(fixture.invoke), 0, fixture.error)
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            snapshot = enrolled_trust_snapshot_from_record(record)
            self.assertEqual(snapshot.continuous_history, "unavailable")
            self.assertIsNone(snapshot.permission_epoch)
            self.assertIsNone(snapshot.actor_permission)
            self.assertEqual(snapshot.repository_id, 123)
            self.assertEqual(snapshot.owner_id, 42)
            self.assertEqual(snapshot.owner_login, "owner")
            self.assertEqual(snapshot.repository_slug, "owner/repo")
            self.assertFalse(snapshot.is_fork)
            self.assertEqual(snapshot.installation_id, 99)
            self.assertTrue(snapshot.repository_selected)
            self.assertEqual(snapshot.actor_id, 101)
            self.assertEqual(snapshot.actor_login, "alice")
            self.assertEqual(
                snapshot.granted_permissions, dict(fixture.observed_permissions)
            )
            self.assertEqual(
                snapshot.required_permissions,
                {"metadata": "read", "pull_requests": "write"},
            )
            with self.assertRaises(ReviewInputError):
                enrolled_trust_snapshot_from_record(
                    replace(record, trust_enrollment=None)
                )
            tampered_doc = copy.deepcopy(record.trust_enrollment)
            assert tampered_doc is not None
            tampered_doc["document"]["authority"]["current_observation"]["repository"][
                "owner_id"
            ] = 999
            with self.assertRaises(ReviewInputError):
                enrolled_trust_snapshot_from_record(
                    replace(record, trust_enrollment=tampered_doc)
                )
            tampered_sha = copy.deepcopy(record.trust_enrollment)
            assert tampered_sha is not None
            tampered_sha["sha256"] = "0" * 64
            with self.assertRaises(ReviewInputError):
                enrolled_trust_snapshot_from_record(
                    replace(record, trust_enrollment=tampered_sha)
                )

    def test_ordinary_hosted_runs_enforce_live_pr_trust_boundaries_and_finalizer_preflight(
        self,
    ):
        for kind, repo_override, expected_error in (
            (
                "ownership_transfer",
                {
                    "id": 123,
                    "full_name": "newowner/repo",
                    "fork": False,
                    "owner": {"id": 999, "login": "newowner"},
                },
                "retention current pull request identity mismatches",
            ),
            (
                "fork_status_change",
                {
                    "id": 123,
                    "full_name": "owner/repo",
                    "fork": True,
                    "owner": {"id": 42, "login": "owner"},
                },
                "repository trust transition refused",
            ),
        ):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = ObservedEntryFixture(directory)
                after_provider_start = []

                def inject_drift():
                    after_provider_start.append(len(fixture.trace))
                    fixture.observed_pr_repo_override = copy.deepcopy(repo_override)

                fixture.before_provider = inject_drift
                self.assertEqual(fixture.opted(fixture.invoke), 1, fixture.error)
                self.assertIn(expected_error, fixture.error)
                if kind == "fork_status_change":
                    self.assertIn(kind, fixture.error)
                self.assertTrue(after_provider_start)
                self.assertFalse(
                    any(
                        method != "GET" and ("/pulls/" in path or "/check-runs" in path)
                        for _, method, path in fixture.trace[after_provider_start[0] :]
                    )
                )

        # Cold action_run permission revocation refuses before business writes, and
        # ReviewApprovalFinalizer._preflight refuses live PR fork/ownership drift when enrolled.
        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            self.assertEqual(fixture.opted(fixture.invoke), 0, fixture.error)
            before = copy.deepcopy(fixture.comment)
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            snapshot = enrolled_trust_snapshot_from_record(record)

            # Direct ReviewApprovalFinalizer._preflight enforcement on PR drift:
            for drift_kind, drift_repo in (
                (
                    "fork_status_change",
                    {
                        "id": 123,
                        "full_name": "owner/repo",
                        "fork": True,
                        "owner": {"id": 42, "login": "owner"},
                    },
                ),
                (
                    "ownership_transfer",
                    {
                        "id": 123,
                        "full_name": "newowner/repo",
                        "fork": False,
                        "owner": {"id": 999, "login": "newowner"},
                    },
                ),
            ):
                with self.subTest(finalizer_drift=drift_kind):
                    finalizer_requests = []

                    def finalizer_opener(request, timeout, repo_payload=drift_repo):
                        finalizer_requests.append(
                            (request.get_method(), urlsplit(request.full_url).path)
                        )
                        return json_response(
                            {
                                "number": 7,
                                "base": {
                                    "ref": "main",
                                    "sha": "a" * 40,
                                    "repo": copy.deepcopy(repo_payload),
                                },
                                "head": {"sha": fixture.requested_head},
                                "user": {"login": "contributor"},
                            },
                            200,
                        )

                    publisher = ReviewPublisher(
                        http=GitHubHttp(opener=finalizer_opener)
                    )
                    publisher.enroll_trust_snapshot(snapshot=snapshot)
                    publisher.finalizer._observed_trust_context = snapshot
                    with self.assertRaisesRegex(
                        GitHubPublicationError,
                        rf"repository trust transition refused.*{drift_kind}",
                    ):
                        publisher.finalizer._preflight(
                            token="synthetic-token",
                            repository="owner/repo",
                            pull_request=7,
                            head_sha=fixture.requested_head,
                            app_slug="reviewsensei[bot]",
                        )
                    self.assertEqual(
                        finalizer_requests, [("GET", "/repos/owner/repo/pulls/7")]
                    )

            fixture.select_command("@reviewsensei review status", new_run=True)
            fixture.observed_permissions["pull_requests"] = "none"
            start = len(fixture.trace)
            self.assertEqual(fixture.opted(fixture.invoke_command), 1, fixture.error)
            self.assertIn("trust", fixture.error)
            self.assertEqual(fixture.comment, before)
            self.assertFalse(
                any(
                    method != "GET" and path.startswith("/repos/")
                    for _, method, path in fixture.trace[start:]
                )
            )

    def test_observe_original_actor_role_is_default_off_and_forwards_when_opted_in(
        self,
    ):
        # 1. Default-off opted review keeps observe_actor_role=False and v1 (None).
        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            self.assertEqual(fixture.opted(fixture.invoke), 0, fixture.error)
            self.assertEqual(fixture.observed_actor_role_requests, [False])
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            snapshot = enrolled_trust_snapshot_from_record(record)
            self.assertIsNone(snapshot.actor_permission)

        # 2. --observe-original-actor-role without --enable-current-trust-enrollment fails closed.
        for label, invoke_fn in (
            ("review_run", lambda f: f.invoke()),
            (
                "action_run",
                lambda f: (
                    f.select_command("@reviewsensei review status", new_run=True),
                    f.invoke_command(),
                )[1],
            ),
        ):
            with (
                self.subTest(route=label),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = ObservedEntryFixture(directory)
                code = fixture.only_actor_role_opt_in(lambda: invoke_fn(fixture))
                self.assertEqual(code, 1, fixture.error)
                self.assertIn(
                    "original actor-role observation requires opted v4 enrollment",
                    fixture.error,
                )
                self.assertEqual(fixture.observed_requests, [])

        # 3. Opted with both flags forwards observe_actor_role=True and records v2 actor_permission.
        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            fixture.observed_schema_version = "current-observation-v2"
            fixture.observed_actor_permission = "write"
            fixture.observed_actor_role = "maintain"
            self.assertEqual(
                fixture.opted_with_actor_role(fixture.invoke), 0, fixture.error
            )
            self.assertEqual(fixture.observed_actor_role_requests, [True])
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            snapshot = enrolled_trust_snapshot_from_record(record)
            self.assertEqual(snapshot.actor_permission, "write")
            self.assertEqual(snapshot.continuous_history, "unavailable")
            self.assertIsNone(snapshot.permission_epoch)

            fixture.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(
                fixture.opted_with_actor_role(fixture.invoke_command),
                0,
                fixture.error,
            )
            self.assertEqual(fixture.observed_actor_role_requests, [True, True])
            after = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            self.assertEqual(after.trust_enrollment, record.trust_enrollment)

    def test_trust_snapshot_bridge_helpers_and_none_branches(self) -> None:
        from types import SimpleNamespace

        # 1. Non-dict or non-v4 authority returns None cleanly.
        self.assertIsNone(observed_trust_snapshot_from_authority(None))
        self.assertIsNone(observed_trust_snapshot_from_authority("invalid"))
        self.assertIsNone(observed_trust_snapshot_from_authority({"version": 3}))

        with tempfile.TemporaryDirectory() as directory:
            fixture = ObservedEntryFixture(directory)
            self.assertEqual(fixture.opted(fixture.invoke), 0, fixture.error)
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            assert record is not None and record.trust_enrollment is not None
            base_authority = copy.deepcopy(
                record.trust_enrollment["document"]["authority"]
            )

            # 2. v1 observation uses passed actor_permission and parses valid capability/selection;
            #    defensive _snapshot_from_authority handles absent capability or repository_access=False.
            v1_auth = copy.deepcopy(base_authority)
            v1_auth["current_observation"]["schema_version"] = "current-observation-v1"
            snap_v1 = observed_trust_snapshot_from_authority(
                v1_auth, actor_permission="maintain"
            )
            assert snap_v1 is not None
            self.assertEqual(snap_v1.actor_permission, "maintain")
            self.assertEqual(
                snap_v1.required_permissions,
                {"metadata": "read", "pull_requests": "write"},
            )
            self.assertTrue(snap_v1.repository_selected)

            defensive_v1 = copy.deepcopy(v1_auth)
            defensive_v1["current_observation"].pop("capability", None)
            defensive_v1["current_observation"]["installation"]["repository_access"] = (
                False
            )
            snap_defensive = _snapshot_from_authority(
                defensive_v1, actor_permission="maintain"
            )
            self.assertIsNone(snap_defensive.required_permissions)
            self.assertFalse(snap_defensive.repository_selected)

            # 3. v2 observation prefers embedded actor.repository_permission over actor_permission kwarg.
            v2_auth = copy.deepcopy(base_authority)
            v2_auth["current_observation"]["schema_version"] = "current-observation-v2"
            v2_auth["current_observation"]["actor"]["repository_permission"] = "write"
            v2_auth["current_observation"]["actor"]["repository_role"] = "write"
            snap_v2 = observed_trust_snapshot_from_authority(
                v2_auth, actor_permission="admin"
            )
            assert snap_v2 is not None
            self.assertEqual(snap_v2.actor_permission, "write")
            self.assertEqual(
                snap_v2.required_permissions,
                {"metadata": "read", "pull_requests": "write"},
            )

            # 4. enroll_application_trust_snapshot short-circuits when caller or trust_enrollment is absent,
            #    and enrolls record snapshot even when run.session_authority is None or non-v4.
            enrolled_calls = []
            app = SimpleNamespace(
                enroll_trust_snapshot=lambda *, snapshot: enrolled_calls.append(
                    snapshot
                )
            )
            self.assertIsNone(
                enroll_application_trust_snapshot(app, SimpleNamespace(caller=None))
            )
            self.assertIsNone(
                enroll_application_trust_snapshot(
                    app,
                    SimpleNamespace(
                        caller=SimpleNamespace(_effect_record="not-a-record")
                    ),
                )
            )
            unenrolled = SessionRecord.create(IDENTITY, now=record.created_at)
            self.assertIsNone(
                enroll_application_trust_snapshot(
                    app,
                    SimpleNamespace(caller=SimpleNamespace(_effect_record=unenrolled)),
                )
            )
            self.assertEqual(enrolled_calls, [])

            observed_absent = enroll_application_trust_snapshot(
                app,
                SimpleNamespace(
                    caller=SimpleNamespace(_effect_record=record),
                    session_authority={"version": 3},
                ),
                actor_permission="admin",
            )
            self.assertIsNone(observed_absent)
            self.assertEqual(len(enrolled_calls), 1)
            self.assertIsNone(enrolled_calls[0].actor_permission)
