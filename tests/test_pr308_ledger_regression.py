"""Public-reader regression for the unchanged PR 308 five-disposition ledger.

Only a disposable file-backed fake GitHub transport can be mutated here. Broker
verification is explicitly synthetic; no original grant or signature is replayed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from review_sensei_github import BrokerClient, GitHubBrokerClientError, GitHubHttp
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    render_session_comment,
)

from review_sensei.disposition import apply_session_command, parse_maintainer_command
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord, read_session_baseline

FIXTURES = Path(__file__).parent / "fixtures" / "regressions"
FIXTURE = FIXTURES / "pr308-session-ledger.json"
EVIDENCE = FIXTURES / "pr308-session-ledger-evidence.json"
IDENTITY = SessionIdentity("malsabbagh/review-sensei", 308, 1336322077)
HEAD = "3fe7cfba58a50285b66740d91107718a03c26311"
NOW = datetime(2026, 10, 17, 22, 50, tzinfo=timezone.utc)
GRANT_RE = re.compile(r"reviewsensei:consumed-grant:v1 sha256=([a-f0-9]{64})")


def document(comment):
    return json.loads(re.search(r"```json\n(.*?)\n```", comment["body"], re.S)[1])


class Response:
    def __init__(self, value, status=200):
        self.status = status
        self.headers = {}
        self.body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return self.body[:size]


class DisposableComments:
    """Persist fake issue comments across reconstructed public reader objects."""

    def __init__(self, path):
        self.path = path
        self.live_head = HEAD
        self.calls = []
        self.before_list = None
        self.list_reads = 0
        self.interrupt = None

    def read(self):
        return json.loads(self.path.read_text())

    def write(self, comments):
        self.path.write_text(json.dumps(comments))

    def opener(self, request, timeout):
        target = urlparse(request.full_url)
        assert target.hostname == "api.github.test", "live network is forbidden"
        path = target.path
        self.calls.append((request.method, path))
        if request.method == "GET" and path.endswith("/issues/308/comments"):
            self.list_reads += 1
            if self.before_list is not None:
                self.before_list(self.list_reads)
            return Response(self.read())
        if request.method == "GET" and path.endswith("/pulls/308"):
            return Response({"head": {"sha": self.live_head}})
        if request.method == "PATCH" and path.endswith("/issues/comments/6236936357"):
            if self.interrupt == "before-store":
                return Response({}, 503)
            comments = self.read()
            comments[0]["body"] = json.loads(request.data)["body"]
            self.write(comments)
            if self.interrupt == "after-store":
                return Response({}, 503)
            return Response(comments[0])
        raise AssertionError(
            f"Unexpected disposable transport call: {request.method} {path}"
        )

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class PreservedLedgerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.original = json.loads(FIXTURE.read_text())
        self.expected = document(self.original)
        self.evidence = json.loads(EVIDENCE.read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = DisposableComments(Path(self.temp.name) / "comments.json")
        self.storage.write([self.original])

    def reader(self):
        # This unbound library reader preserves historical native state. It is
        # not an authenticated Actions mutation authority or a migration path.
        return GitHubIssueCommentSessionLedger(
            GitHubHttp(api_url="https://api.github.test", opener=self.storage.opener),
            token="synthetic-test-token",
            app_slug="reviewsensei[bot]",
        )

    def load(self):
        loaded = self.reader().load(IDENTITY, now=NOW)
        self.assertEqual(loaded.status, "ok", loaded.detail)
        return loaded.record

    def pause(self, reader=None):
        command = parse_maintainer_command(
            "@reviewsensei review pause", actor="synthetic-maintainer", head_sha=HEAD
        )
        return apply_session_command(
            reader or self.reader(), IDENTITY, command, now=NOW
        )

    def test_exact_fixture_origin_and_canonical_digest_are_preserved(self):
        self.assertEqual(
            hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            self.evidence["fixture_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(self.original["body"].encode()).hexdigest(),
            self.evidence["body_sha256"],
        )
        payload = {
            key: value for key, value in self.expected.items() if key != "record_sha256"
        }
        digest = hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        self.assertEqual(digest, self.expected["record_sha256"])
        self.assertIn(f"pr=308 gen=5 digest={digest}", self.original["body"])
        self.assertEqual(
            self.original["user"],
            {"id": 317764649, "login": "reviewsensei[bot]", "type": "Bot"},
        )
        self.assertLess(
            self.original["updated_at"], self.evidence["failing_run"]["created_at"]
        )
        self.assertEqual(self.evidence["failing_run"]["head_sha"], HEAD)
        self.assertEqual(
            self.evidence["rejection"],
            {
                "path": ["dispositions"],
                "validator": "maxItems",
                "released_limit": 4,
                "actual_items": 5,
            },
        )

    def test_public_reader_preserves_exact_five_decision_record(self):
        before = self.storage.path.read_bytes()
        loaded = self.load()
        self.assertEqual(loaded.to_dict(), self.expected)
        self.assertEqual(len(loaded.dispositions), 5)
        self.assertEqual(loaded.identity(), IDENTITY)
        self.assertEqual(
            loaded.transaction.head_sha, "67bbd8eb926261a3e027646830f8027e4facc56a"
        )
        self.assertEqual(loaded.transaction.phase, "publication_succeeded")
        self.assertEqual(
            (
                loaded.completed_initial_reviews,
                loaded.completed_verification_rounds,
                loaded.failed_attempts,
                loaded.generation,
            ),
            (1, 0, 0, 5),
        )
        self.assertEqual(self.storage.path.read_bytes(), before)
        self.assertFalse(self.storage.writes)
        self.assertEqual(self.load().record_sha256, loaded.record_sha256)
        baseline = read_session_baseline(self.reader(), loaded)
        self.assertEqual(baseline.cache_key.repository, IDENTITY.repository)
        self.assertEqual(baseline.cache_key.pull_request, IDENTITY.pull_request)
        self.assertEqual(baseline.cache_key.head_sha, loaded.transaction.head_sha)
        self.assertEqual(baseline.cache_key.base_sha, loaded.transaction.base_sha)
        self.assertEqual(baseline.policy_digest, loaded.transaction.policy_digest)
        self.assertEqual(len(baseline.findings), 18)

    def test_prior_head_decisions_do_not_approve_the_new_head(self):
        command = parse_maintainer_command(
            "@reviewsensei verify", actor="synthetic-maintainer", head_sha=HEAD
        )
        record, result = apply_session_command(
            self.reader(), IDENTITY, command, now=NOW
        )
        self.assertFalse(result.applied)
        self.assertIn("stale override", result.summary)
        self.assertEqual(record.to_dict(), self.expected)
        self.assertFalse(self.storage.writes)

    def test_invalid_nested_baseline_is_not_blessed_by_a_valid_outer_digest(self):
        invalid = copy.deepcopy(self.expected)
        invalid["convergence_history"]["baseline"]["sha256"] = "0" * 64
        payload = {
            key: value for key, value in invalid.items() if key != "record_sha256"
        }
        digest = hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        invalid["record_sha256"] = digest
        comment = copy.deepcopy(self.original)
        comment["body"] = re.sub(
            r"```json\n.*?\n```",
            "```json\n"
            + json.dumps(invalid, sort_keys=True, separators=(",", ":"))
            + "\n```",
            comment["body"],
            flags=re.S,
        ).replace(self.expected["record_sha256"], digest)
        self.storage.write([comment])
        before = self.storage.path.read_bytes()
        self.assertEqual(
            self.reader().load(IDENTITY, now=NOW).status, "integrity-failed"
        )
        self.assertEqual(self.storage.path.read_bytes(), before)
        self.assertFalse(self.storage.writes)

    def test_wrong_repository_slug_and_foreign_authors_cannot_supply_authority(self):
        other = SessionIdentity("other/repository", 308, IDENTITY.repository_id)
        self.assertEqual(self.reader().load(other, now=NOW).status, "integrity-failed")
        for user in (
            {"type": "User", "login": "reviewsensei[bot]", "id": 317764649},
            {"type": "Bot", "login": "other-app[bot]", "id": 317764650},
        ):
            with self.subTest(user=user):
                comment = copy.deepcopy(self.original)
                comment["user"] = user
                self.storage.write([comment])
                self.assertEqual(
                    self.reader().load(IDENTITY, now=NOW).status, "missing"
                )
        self.assertFalse(self.storage.writes)

    def test_unbound_library_restart_preserves_history_without_inventing_grants(
        self,
    ):
        initial_grants = set(GRANT_RE.findall(self.original["body"]))
        ledger = self.reader()
        updated, result = self.pause(ledger)
        self.assertTrue(result.applied)
        self.assertEqual(updated.generation, 6)
        self.assertTrue(updated.operator_paused)
        after = self.load()  # Reconstructs the HTTP client and ledger reader.
        self.assertEqual(after, updated)
        for key in (
            "repository",
            "repository_id",
            "pull_request",
            "dispositions",
            "transaction",
            "convergence_history",
            "completed_initial_reviews",
            "completed_verification_rounds",
            "failed_attempts",
            "last_committed_reservation_id",
        ):
            self.assertEqual(after.to_dict()[key], self.expected[key], key)
        all_grants = set(GRANT_RE.findall(self.storage.read()[0]["body"]))
        self.assertEqual(initial_grants, all_grants)
        before_retry = self.storage.path.read_bytes()
        writes = len(self.storage.writes)
        duplicate, result = self.pause(self.reader())
        self.assertTrue(result.applied)
        self.assertEqual(duplicate, after)
        self.assertEqual(self.storage.path.read_bytes(), before_retry)
        self.assertEqual(len(self.storage.writes), writes)

    def test_supported_round_commit_restart_and_duplicate_do_not_double_count(self):
        reservation = "d" * 64
        reserved = self.reader().reserve(
            IDENTITY,
            slot="verification",
            reservation_id=reservation,
            expected_generation=5,
            head_sha=HEAD,
            now=NOW,
        )
        self.assertEqual(reserved.generation, 6)
        self.assertEqual(reserved.completed_verification_rounds, 0)
        self.assertIsNone(
            reserved.transaction
        )  # Completed prior transaction is retired on new reservation.
        self.assertEqual(self.load(), reserved)
        committed = self.reader().commit(
            IDENTITY, reservation_id=reservation, expected_generation=6, now=NOW
        )
        self.assertEqual(
            (
                committed.completed_initial_reviews,
                committed.completed_verification_rounds,
                committed.failed_attempts,
            ),
            (1, 1, 0),
        )
        self.assertEqual(committed.generation, 7)
        self.assertEqual(list(committed.dispositions), self.expected["dispositions"])
        self.assertEqual(committed.last_committed_reservation_id, reservation)
        self.assertIsNone(committed.reservation_id)
        self.assertEqual(self.load(), committed)
        before = self.storage.path.read_bytes()
        duplicate = self.reader().commit(
            IDENTITY, reservation_id=reservation, expected_generation=6, now=NOW
        )
        self.assertEqual(duplicate, committed)
        # Unbound library no-ops preserve the historical grant inventory.
        self.assertEqual(
            document(self.storage.read()[0]), document(json.loads(before)[0])
        )
        self.assertEqual(
            set(GRANT_RE.findall(json.loads(before)[0]["body"])),
            set(GRANT_RE.findall(self.storage.read()[0]["body"])),
        )

    def test_tampering_and_wrong_identity_fail_closed_without_repair(self):
        edits = {
            "payload": lambda body: body.replace('"generation":5', '"generation":6'),
            "digest": lambda body: body.replace(
                self.expected["record_sha256"], "0" * 64
            ),
            "generation": lambda body: body.replace("pr=308 gen=5", "pr=308 gen=6"),
            "repository_id": lambda body: body.replace(
                "repo=1336322077 pr=308", "repo=1336322078 pr=308"
            ),
            "pull_request": lambda body: body.replace("pr=308 gen=5", "pr=309 gen=5"),
        }
        for label, edit in edits.items():
            with self.subTest(label=label):
                comment = copy.deepcopy(self.original)
                comment["body"] = edit(comment["body"])
                self.assertNotEqual(comment["body"], self.original["body"])
                self.storage.write([comment])
                before = self.storage.path.read_bytes()
                self.assertEqual(
                    self.reader().load(IDENTITY, now=NOW).status, "integrity-failed"
                )
                with self.assertRaises(ReviewInputError):
                    self.pause()
                self.assertEqual(self.storage.path.read_bytes(), before)
        self.assertFalse(self.storage.writes)

    def test_duplicate_roots_are_conflict_not_a_new_session(self):
        duplicate = copy.deepcopy(self.original)
        duplicate["id"] += 1
        self.storage.write([self.original, duplicate])
        self.assertEqual(self.reader().load(IDENTITY, now=NOW).status, "conflict")
        with self.assertRaises(ReviewInputError):
            self.pause()
        self.assertFalse(self.storage.writes)

    def test_conflicting_generation_preserves_concurrent_writer(self):
        def concurrent(read_number):
            if read_number == 2:
                current = SessionRecord.from_dict(self.expected).evolve(
                    generation=6, operator_paused=True, now=NOW
                )
                comment = copy.deepcopy(self.original)
                comment["body"] = render_session_comment(
                    repository_id=IDENTITY.repository_id,
                    pull_request=308,
                    record=current,
                    consumed_grants=tuple(GRANT_RE.findall(self.original["body"])),
                )
                self.storage.write([comment])

        self.storage.before_list = concurrent
        with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
            self.pause()
        self.assertFalse(self.storage.writes)
        self.assertEqual(self.load().generation, 6)
        self.assertEqual(list(self.load().dispositions), self.expected["dispositions"])

    def test_current_initializer_refuses_historical_root_without_importing_allowance(
        self,
    ):
        from review_sensei_github.api_run_host import HOSTED_API_STARTUP_PLAN
        from review_sensei_github.api_run_requests import ApiRunRequestTransport

        from review_sensei.api_work import (
            ApiExecutionOwner,
            ApiRunIdentity,
            FrozenApiConfiguration,
        )

        before = self.storage.path.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            caller = ApiRunRequestTransport(
                directory=Path(directory),
                run=ApiRunIdentity(IDENTITY.repository_id, 900000002),
                plan=HOSTED_API_STARTUP_PLAN,
                attempt=1,
                opener=self.storage.opener,
            )
            ledger = GitHubIssueCommentSessionLedger(
                GitHubHttp(api_url="https://api.github.test", opener=caller.open),
                token="synthetic",
                app_slug="reviewsensei[bot]",
            )
            with self.assertRaisesRegex(ReviewInputError, "already exists"):
                caller.initialize_and_activate(
                    ledger,
                    IDENTITY,
                    FrozenApiConfiguration("b" * 40, "c" * 64, 512),
                    ApiExecutionOwner(1, "hosted", 0),
                )
            self.assertTrue(caller.observations)
            self.assertIsNone(caller._state)
        self.assertFalse(self.storage.writes)
        self.assertEqual(self.storage.path.read_bytes(), before)
        self.assertEqual(self.load().to_dict(), self.expected)

    def test_phase_bound_authority_is_retired_for_every_version_before_http(self):
        before = self.storage.path.read_bytes()
        for version in (1, 2, 3):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    ReviewInputError, "phase ledgers are retired"
                ):
                    GitHubIssueCommentSessionLedger(
                        GitHubHttp(opener=self.storage.opener),
                        token="synthetic",
                        broker=object(),
                        session_grant="synthetic-never-replayed",
                        session_attestation={"version": version},
                        head_sha=HEAD,
                    )
        self.assertFalse(self.storage.calls)
        self.assertEqual(self.storage.path.read_bytes(), before)

    def test_real_current_broker_rejects_incomplete_or_wrong_scope_verification(self):
        from test_github_broker_client import current_authority, current_grant

        authority = current_authority(
            "command", repository_id=IDENTITY.repository_id, head_sha=HEAD
        )
        authority.update(repository=IDENTITY.repository, pull_request=308)
        grant = current_grant(authority)
        responses = (
            {},
            {"session_attestation": {"version": 1}},
            {"session_attestation": {**grant, "head_sha": "f" * 40}},
        )
        for response in responses:
            with self.subTest(response=response):
                calls = []

                def broker_opener(request, timeout):
                    calls.append(request)
                    self.assertTrue(request.full_url.endswith("/session-grant"))
                    return Response(response)

                broker = BrokerClient(
                    broker_url="https://broker.github.test/api/github/token",
                    opener=broker_opener,
                    authority=authority,
                )
                with self.assertRaises(GitHubBrokerClientError):
                    broker.verify_session_grant("sg1.1780000000000." + "A" * 43, grant)
                self.assertEqual(len(calls), 1)
        self.assertFalse(self.storage.writes)
        self.assertEqual(self.load().to_dict(), self.expected)

    def test_unbound_library_interrupted_write_requires_readback(self):
        for mode, persisted in (("before-store", False), ("after-store", True)):
            with self.subTest(mode=mode):
                self.storage.write([self.original])
                self.storage.interrupt = mode
                with self.assertRaises(GitHubPublicationTransientError):
                    self.pause(self.reader())
                self.storage.interrupt = None
                record = self.load()
                self.assertEqual(record.operator_paused, persisted)
                self.assertEqual(record.generation, 6 if persisted else 5)
                self.assertEqual(
                    list(record.dispositions), self.expected["dispositions"]
                )
                self.assertEqual(
                    set(GRANT_RE.findall(self.storage.read()[0]["body"])),
                    set(GRANT_RE.findall(self.original["body"])),
                )
        self.assertEqual(
            hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            self.evidence["fixture_sha256"],
        )


class CurrentActionPreservationTests(unittest.TestCase):
    """Current Actions behavior is qualified independently of historical bytes."""

    def test_current_pause_survives_reconstruction_and_stale_head_refuses(self):
        from review_sensei_github.session_ledger import parse_session_comment
        from test_action_run import CommandEntryFixture
        from test_api_work_persistence import IDENTITY as CURRENT_IDENTITY

        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory)
            self.assertEqual(f.invoke_command(), 0, f.error)
            original = parse_session_comment(
                f.comment["body"], identity=CURRENT_IDENTITY
            )
            self.assertTrue(original.operator_paused)
            self.assertEqual(original.api_run_accounting["action"]["phase"], "terminal")
            f.select_command("@reviewsensei review status", new_run=True)
            self.assertEqual(f.invoke_command(), 0, f.error)
            current = parse_session_comment(
                f.comment["body"], identity=CURRENT_IDENTITY
            )
            self.assertTrue(current.operator_paused)
            self.assertEqual(f.business_writes, 1)
            self.assertEqual(
                current.completed_initial_reviews, original.completed_initial_reviews
            )
            self.assertEqual(
                current.completed_verification_rounds,
                original.completed_verification_rounds,
            )
        with tempfile.TemporaryDirectory() as directory:
            f = CommandEntryFixture(directory)
            f.requested_head = "f" * 40
            self.assertNotEqual(f.invoke_command(), 0)
            self.assertIsNone(f.comment)
            self.assertEqual(f.business_writes, 0)

    def test_current_unknown_business_write_retains_charge_and_never_replays(self):
        from review_sensei_github.session_ledger import parse_session_comment
        from test_action_run import CommandEntryFixture
        from test_api_work_persistence import IDENTITY as CURRENT_IDENTITY

        for outcome in ("stored", "absent"):
            with (
                self.subTest(outcome=outcome),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CommandEntryFixture(directory)
                f.action_fault = "business-" + outcome
                status = f.invoke_command()
                record = parse_session_comment(
                    f.comment["body"], identity=CURRENT_IDENTITY
                )
                self.assertEqual(status == 0, outcome == "stored", f.error)
                self.assertEqual(record.operator_paused, outcome == "stored")
                self.assertEqual(
                    record.api_run_accounting["action"]["phase"],
                    "terminal" if outcome == "stored" else "pending",
                )
                self.assertGreaterEqual(f.accounted.accounting.uncertain, 1)
                self.assertEqual(f.business_writes, 1)
                if outcome == "absent":
                    retained = f.comment["body"]
                    f.select_command("@reviewsensei review status", new_run=True)
                    self.assertNotEqual(f.invoke_command(), 0)
                    self.assertEqual(f.comment["body"], retained)
                    self.assertEqual(f.business_writes, 1)


if __name__ == "__main__":
    unittest.main()
