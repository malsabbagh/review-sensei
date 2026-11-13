"""Source-produced original enrollment, immutable storage and cold data restore."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from review_sensei_github.errors import GitHubBrokerClientError
from review_sensei_github.trust_enrollment import (
    compare_current_observation,
    current_observation,
    original_observation,
    prepare_original_enrollment,
)

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from review_sensei.trust_enrollment import normalize_reference
from tests.test_current_trust_observation import observed
from tests.test_run_authority import HEAD, NOW, BrokerFixture

IDENTITY = SessionIdentity("owner/repo", 7, repository_id=123)
STAMP = datetime.fromtimestamp(NOW, timezone.utc)


def source_session(*, change=None):
    request, grant = observed("review")
    if change:
        change(grant)

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
    return fixture, session


class TrustEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch(
            "review_sensei_github.trust_enrollment.time.time", return_value=NOW
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def enrolled(self):
        fixture, session = source_session()
        original = SessionRecord.create(IDENTITY, now=STAMP)
        record = prepare_original_enrollment(fixture.client, session, original)
        return fixture, session, record

    def test_source_exchange_retains_no_secret_and_cold_codec_preserves_original(self):
        fixture, session, record = self.enrolled()
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(record.generation, 0)
        self.assertEqual(original_observation(record)["app_id"], 999)
        serialized = json.dumps(record.to_dict())
        for secret in (session.token, session.grant, "synthetic-oidc"):
            self.assertNotIn(secret, serialized)
        restored = SessionRecord.from_dict(json.loads(serialized))
        self.assertEqual(restored, record)
        self.assertEqual(
            compare_current_observation(fixture.client, session, restored)[
                "continuous_history"
            ],
            "unavailable",
        )
        cold = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import json,sys
from review_sensei.session import SessionRecord
from review_sensei_github.trust_enrollment import original_observation
record=SessionRecord.from_dict(json.load(sys.stdin))
assert original_observation(record)["authority"]["current_observation"]["installation"]["id"]==99
print(record.record_sha256)
""",
            ],
            input=serialized,
            text=True,
            capture_output=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            timeout=30,
        )
        self.assertEqual(cold.returncode, 0, cold.stderr)
        self.assertEqual(cold.stdout.strip(), record.record_sha256)

    def test_native_cold_reader_authenticates_original_and_rejects_foreign_owner(self):
        from fake_github_http import json_response, make_http
        from review_sensei_github.session_ledger import (
            GitHubIssueCommentSessionLedger,
            render_session_comment,
        )
        from review_sensei_github.trust_enrollment import (
            compare_restored_observation,
            load_original_enrollment,
        )

        from review_sensei.bounded_evidence import EvidenceReadBudget

        fixture, session, record = self.enrolled()
        row = {
            "id": 55,
            "body": render_session_comment(
                repository_id=123, pull_request=7, record=record
            ),
            "user": {"id": 888, "type": "Bot", "login": "reviewsensei[bot]"},
        }
        for valid in (True, False):
            current = copy.deepcopy(row)
            if not valid:
                current["user"]["login"] = "foreign[bot]"
            http, calls = make_http([json_response([current])])
            budget = EvidenceReadBudget()
            ledger = GitHubIssueCommentSessionLedger(
                http,
                token=session.token,
                app_slug="reviewsensei[bot]",
                evidence_budget=budget,
            )
            if valid:
                loaded, restored = load_original_enrollment(ledger, IDENTITY, now=STAMP)
                self.assertEqual(loaded.record, record)
                self.assertEqual(
                    compare_restored_observation(fixture.client, session, restored)[
                        "qualification"
                    ],
                    "current_observations_only",
                )
                with self.assertRaisesRegex(ReviewInputError, "native source"):
                    compare_restored_observation(
                        fixture.client, session, replace(restored)
                    )
                self.assertEqual(len(calls), 1)
                self.assertEqual(budget.calls, 1)
            else:
                with self.assertRaises(ReviewInputError):
                    load_original_enrollment(ledger, IDENTITY, now=STAMP)
            self.assertTrue(all(method == "GET" for method, _, _ in calls))

    def test_supplied_or_mutated_models_and_expired_receipts_never_issue(self):
        fixture, session, record = self.enrolled()
        for broker, candidate in (
            (fixture.client, replace(session)),
            (object(), session),
        ):
            with self.assertRaisesRegex(ReviewInputError, "source issuance"):
                current_observation(broker, candidate)
        session.attestation["current_observation"]["installation"]["id"] = 100
        with self.assertRaisesRegex(ReviewInputError, "changed"):
            current_observation(fixture.client, session)
        fixture, session = source_session()
        with patch(
            "review_sensei_github.trust_enrollment.time.time", return_value=NOW + 600
        ):
            with self.assertRaisesRegex(ReviewInputError, "expired"):
                current_observation(fixture.client, session)

    def test_identity_installation_and_permissions_changes_hold(self):
        _, _, record = self.enrolled()
        for section, field, value in (
            ("repository", "owner_id", 43),
            ("installation", "id", 100),
            ("installation", "repository_selection", "all"),
            (
                "installation",
                "permissions",
                {"metadata": "read", "pull_requests": "write", "contents": "read"},
            ),
        ):
            with self.subTest(section=section, field=field):
                fixture, session = source_session(
                    change=lambda grant: grant["current_observation"][
                        section
                    ].__setitem__(field, value)
                )
                with self.assertRaisesRegex(ReviewInputError, "observation changed"):
                    compare_current_observation(fixture.client, session, record)

    def test_absence_is_legacy_not_enrollment_and_original_is_immutable(self):
        original = SessionRecord.create(IDENTITY, now=STAMP)
        self.assertNotIn("trust_enrollment", original.to_dict())
        self.assertEqual(
            SessionRecord.from_dict(original.to_dict()).to_dict(), original.to_dict()
        )
        with self.assertRaisesRegex(ReviewInputError, "legacy"):
            original_observation(original)
        _, _, record = self.enrolled()
        with self.assertRaisesRegex(ReviewInputError, "removed or replaced"):
            record.evolve(trust_enrollment=None)
        changed = copy.deepcopy(record.trust_enrollment)
        changed["document"]["authority"]["current_observation"]["installation"][
            "id"
        ] = 100
        changed["sha256"] = hashlib.sha256(
            canonical_bytes(changed["document"])
        ).hexdigest()
        with self.assertRaisesRegex(ReviewInputError, "removed or replaced"):
            record.evolve(trust_enrollment=changed)
        null = original.to_dict() | {"trust_enrollment": None}
        with self.assertRaisesRegex(ReviewInputError, "null absence"):
            SessionRecord.from_dict(null)

    def test_tamper_foreign_scope_unknown_secret_fields_and_one_short_size_hold(self):
        fixture, session, record = self.enrolled()
        reference = copy.deepcopy(record.trust_enrollment)
        reference["document"]["app_id"] = 1000
        with self.assertRaisesRegex(ReviewInputError, "integrity"):
            normalize_reference(reference)
        reference = copy.deepcopy(record.trust_enrollment)
        reference["document"]["authority"]["token"] = "must-not-retain"
        reference["sha256"] = hashlib.sha256(
            canonical_bytes(reference["document"])
        ).hexdigest()
        with self.assertRaisesRegex(ReviewInputError, "fields"):
            normalize_reference(reference)
        with self.assertRaisesRegex(ReviewInputError, "scope"):
            prepare_original_enrollment(
                fixture.client,
                session,
                SessionRecord.create(
                    SessionIdentity("owner/repo", 8, repository_id=123), now=STAMP
                ),
            )
        original = SessionRecord.create(IDENTITY, now=STAMP)
        size = len(canonical_bytes(record.trust_enrollment))
        with patch("review_sensei.trust_enrollment.MAX_MANIFEST_BYTES", size - 1):
            with self.assertRaisesRegex(ReviewInputError, "unsupported"):
                prepare_original_enrollment(fixture.client, session, original)
        self.assertIsNone(original.trust_enrollment)
        self.assertEqual(len(fixture.calls), 1)

    def test_v3_source_remains_unenrolled_and_source_funding_failure_has_no_dispatch(
        self,
    ):
        from tests.test_run_authority import authority

        fixture = BrokerFixture(authority())
        session = fixture.client.open_session(
            "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
        )
        with self.assertRaisesRegex(ReviewInputError, "source issuance"):
            current_observation(fixture.client, session)
        fixture = BrokerFixture(observed("review")[0])
        fixture.client.before_request = lambda: (_ for _ in ()).throw(
            ReviewInputError("original allowance exhausted")
        )
        with self.assertRaisesRegex(
            GitHubBrokerClientError, "Broker request allowance is exhausted"
        ):
            fixture.client.open_session(
                "synthetic-oidc", repository_id=123, pull_request=7, head_sha=HEAD
            )
        self.assertEqual(fixture.calls, [])
