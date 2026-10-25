"""One finite hosted startup/recovery through real broker and session APIs.

External HTTP is synthetic. This fixture does not activate a workflow entry or
claim that every ReviewSensei phase already shares the new caller accounting.
"""

import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.api_run_owner import (
    ApiRunActionsEvidence,
    claim_hosted_startup,
)
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.http import MAX_PAGINATION_PAGES, GitHubHttp
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)

from review_sensei.api_work import (
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
    PendingApiRequest,
    StartupPlan,
    StartupSlot,
    initial_accounting,
)
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.configuration import parse_configuration_text
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from tests.fake_github_http import json_response

IDENTITY = SessionIdentity("owner/repo", 7, repository_id=123)
RUN = ApiRunIdentity(123, 456)
BASE, HEAD = "a" * 40, "b" * 40
CONFIG = b"schema: 1\napi_request_limit: 128\n"

# The prelude actually dispatches these seven caller requests. The ordinary
# native ledger path uses GitHubHttp.paginate's ten-physical-request ceiling,
# counting oversized failures and terminal pages. This is NOT the ledger's
# different shared-budget _fetch_pages envelope.
PREFIX = ("oidc", "session", "actions-token", "pull", "configuration", "run", "jobs")
PLAN = StartupPlan(
    (
        StartupSlot(
            "hosted",
            1,
            PREFIX
            + ("initial-discovery",) * MAX_PAGINATION_PAGES
            + ("create",)
            + ("initial-readback",) * MAX_PAGINATION_PAGES,
        ),
        StartupSlot(
            "hosted",
            2,
            PREFIX
            + ("ceased-job",) * 2
            + ("recovery-lists",) * (4 * MAX_PAGINATION_PAGES)
            + ("update",),
        ),
    )
)
# One later native replace: discovery, prewrite rediscovery, PATCH, readback.
NEXT_REPLACE_LIABILITY = 3 * MAX_PAGINATION_PAGES + 1


class StartupFixture:
    def __init__(self, directory, *, fanout=30):
        self.directory = Path(directory)
        self.fanout = fanout
        self.attempt = 1
        self.comment = None
        self.trace = []
        self.fault = None
        self.old_active = False
        self.incomplete_jobs = False
        self.changed_old_job = False
        self.old_job_reads = 0
        self.config_bytes = CONFIG
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def open(self, request, timeout):
        target = urlsplit(request.full_url)
        path, method = target.path, request.method
        self.trace.append((self.attempt, method, path))
        if target.hostname == "oidc.github.test":
            return json_response({"value": "synthetic-oidc"})
        if target.hostname == "broker.github.test":
            payload = json.loads(request.data)
            capability = payload["capability"]
            value = {
                "token": "synthetic-" + capability,
                "capability": capability,
                "control_receipt": {
                    **payload["control_budget"],
                    "dispatched": self.fanout,
                },
            }
            if capability == "review_session":
                value.update(
                    session_state="known" if self.comment else "enrolled",
                    reservation_owner={"run_id": "456", "head_sha": HEAD},
                )
            return json_response(value)
        if path.endswith("/pulls/7"):
            return json_response(
                {
                    "number": 7,
                    "head": {"sha": HEAD},
                    "base": {
                        "sha": BASE,
                        "repo": {"id": 123, "full_name": "owner/repo"},
                    },
                }
            )
        if path.endswith("/contents/.reviewsensei.yml"):
            # A separate existing workflow read capability, not an enlarged
            # review_session token. No contents permission is added here.
            assert (
                request.get_header("Authorization") == "Bearer synthetic-workflow-read"
            )
            return json_response(
                {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(self.config_bytes).decode(),
                    "size": len(self.config_bytes),
                }
            )
        if path.endswith("/actions/runs/456"):
            return json_response(
                {
                    "id": 456,
                    "repository": {"id": 123, "full_name": "owner/repo"},
                    "run_attempt": self.attempt,
                    "status": "in_progress",
                }
            )
        if "/attempts/" in path and path.endswith("/jobs"):
            attempt = int(path.split("/attempts/")[1].split("/")[0])
            if attempt < self.attempt:
                self.old_job_reads += 1
            completed_at = (
                "2026-10-25T20:01:00Z"
                if self.changed_old_job and self.old_job_reads > 1
                else "2026-10-25T20:00:00Z"
            )
            active = attempt == self.attempt or self.old_active
            return json_response(
                {
                    "total_count": 2 if self.incomplete_jobs else 1,
                    "jobs": [
                        {
                            "id": 900 + attempt,
                            "run_id": 456,
                            "run_attempt": attempt,
                            "name": "ReviewSensei / hosted",
                            "status": "in_progress" if active else "completed",
                            "conclusion": None if active else "failure",
                            "completed_at": None if active else completed_at,
                        }
                    ],
                }
            )
        if path.endswith("/issues/7/comments") and method == "GET":
            return json_response(
                [] if self.comment is None else [copy.deepcopy(self.comment)]
            )
        if method in {"POST", "PATCH"}:
            payload = json.loads(request.data)
            if self.fault == "unknown-absent":
                self.fault = None
                raise TimeoutError("synthetic lost write response")
            self.comment = {
                "id": 77,
                "body": payload["body"],
                "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
            }
            if self.fault == "unknown-stored":
                self.fault = None
                raise TimeoutError("synthetic lost write response")
            return json_response(
                copy.deepcopy(self.comment), status=201 if method == "POST" else 200
            )
        raise AssertionError((method, path))

    def prelude(self):
        # Local claim precedes every HTTP request. Third attempts are rejected
        # by the source-owned slot type before even this file can be created.
        claim_hosted_startup(self.directory, RUN, self.attempt)
        budget = EvidenceReadBudget()
        client = BrokerClient(
            broker_url="https://broker.github.test/api/github/token",
            opener=self.open,
            control=BrokerDispatchAccounting(budget, "c" * 64, 30, 1),
        )
        with patch.dict(
            os.environ,
            {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.github.test/token?fixture=1",
            },
            clear=True,
        ):
            oidc = client.request_oidc_token()
        session = client.open_session(
            oidc, repository_id=123, pull_request=7, head_sha=HEAD
        )
        if session.reservation_owner != {"run_id": "456", "head_sha": HEAD}:
            raise ReviewInputError("broker owner scope conflicts")
        actions_token = client.exchange(oidc, capability="review_actions")
        _, pull = self.http.request(
            "GET", "/repos/owner/repo/pulls/7", token=session.token
        )
        assert pull["head"]["sha"] == HEAD and pull["base"]["repo"]["id"] == 123
        _, document = self.http.request(
            "GET",
            f"/repos/owner/repo/contents/.reviewsensei.yml?ref={pull['base']['sha']}",
            token="synthetic-workflow-read",
        )
        raw = base64.b64decode(document["content"], validate=True)
        configuration = FrozenApiConfiguration(
            pull["base"]["sha"],
            hashlib.sha256(raw).hexdigest(),
            parse_configuration_text(raw.decode()).api_request_limit,
        )
        PLAN.require_limit(configuration.limit)
        evidence = ApiRunActionsEvidence(
            self.http,
            token=actions_token,
            command_run_id=session.reservation_owner["run_id"],
        )
        owner = evidence.current_owner(IDENTITY, attempt=self.attempt, generation=0)
        ledger = GitHubIssueCommentSessionLedger(
            self.http, token=session.token, app_slug="reviewsensei[bot]"
        )
        return ledger, configuration, evidence, owner

    def initial(self):
        ledger, configuration, _evidence, owner = self.prelude()
        state = initial_accounting(
            RUN, configuration, owner, PLAN, prior_nonstartup_liability_upper_bound=0
        ).reserve("first-owner", NEXT_REPLACE_LIABILITY)
        record = ledger.initialize_with_mutation(
            IDENTITY, lambda r: r.evolve(api_run_accounting=state.to_document())
        )
        return ApiRunAccounting.from_document(record.api_run_accounting)

    def recover(self):
        self.attempt = 2
        ledger, configuration, evidence, owner = self.prelude()
        loaded = ledger.load(IDENTITY)
        if (
            loaded.status != "ok"
            or loaded.record is None
            or loaded.record.api_run_accounting is None
        ):
            # Absence cannot authorize a replacement POST after an unknown
            # first write, even when the predecessor has stopped.
            raise ReviewInputError("prior API accounting cannot be established")
        previous = ApiRunAccounting.from_document(loaded.record.api_run_accounting)
        previous.require_binding(RUN, configuration)
        stopped = evidence.ceased_owner(IDENTITY, previous.owner)
        owner = replace(owner, generation=previous.owner.generation + 1)

        def mutate(record):
            if (
                record.record_sha256 != loaded.record.record_sha256
                or evidence.ceased_owner(IDENTITY, previous.owner) != stopped
            ):
                raise ReviewInputError("API owner handoff evidence changed")
            state = previous.claim_startup(owner)
            return record.evolve(
                generation=record.generation + 1, api_run_accounting=state.to_document()
            )

        confirmed = ledger.replace(IDENTITY, mutate)
        return ApiRunAccounting.from_document(confirmed.api_run_accounting)


class ApiStartupPersistenceTests(unittest.TestCase):
    def test_actual_startup_and_one_recovery_preserve_one_allowance(self):
        for fanout in (9, 12, 30):
            with (
                self.subTest(fanout=fanout),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = StartupFixture(directory, fanout=fanout)
                state = fixture.initial()
                self.assertEqual(len(fixture.trace), 10)
                self.assertEqual(PLAN.liability, 78)
                self.assertEqual(state.confirmed, 0)
                self.assertEqual(state.remaining, 19)
                restored = fixture.recover()
                self.assertEqual(len(fixture.trace), 24)
                self.assertEqual(restored.identity, state.identity)
                self.assertEqual(restored.configuration, state.configuration)
                self.assertEqual(restored.remaining, state.remaining)
                with self.assertRaisesRegex(ReviewInputError, "issuing owner"):
                    restored.dispatch(
                        "first-owner", PendingApiRequest("wrong-owner", 100)
                    )
                self.assertEqual(
                    sum(
                        method == "POST" and path.endswith("/issues/7/comments")
                        for _, method, path in fixture.trace
                    ),
                    1,
                )

    def test_unknown_initial_post_is_not_repeated_and_retained_record_can_recover(self):
        for fault in ("unknown-stored", "unknown-absent"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = StartupFixture(directory)
                fixture.fault = fault
                with self.assertRaises(GitHubPublicationTransientError):
                    fixture.initial()
                if fault == "unknown-stored":
                    state = fixture.recover()
                    self.assertEqual(state.remaining, 19)
                else:
                    with self.assertRaisesRegex(
                        ReviewInputError, "cannot be established"
                    ):
                        fixture.recover()
                self.assertEqual(
                    sum(
                        method == "POST" and path.endswith("/issues/7/comments")
                        for _, method, path in fixture.trace
                    ),
                    1,
                )

    def test_same_slot_restart_and_third_attempt_refuse_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartupFixture(directory)
            fixture.initial()
            count = len(fixture.trace)
            with self.assertRaisesRegex(ReviewInputError, "slot"):
                fixture.initial()
            self.assertEqual(len(fixture.trace), count)
            fixture.attempt = 3
            with self.assertRaises(ReviewInputError):
                fixture.prelude()
            self.assertEqual(len(fixture.trace), count)

    def test_active_predecessor_or_incomplete_job_inventory_never_writes(self):
        for fault in ("active", "incomplete"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = StartupFixture(directory)
                fixture.initial()
                old_body = fixture.comment["body"]
                fixture.old_active = fault == "active"
                fixture.incomplete_jobs = fault == "incomplete"
                with self.assertRaises(ReviewInputError):
                    fixture.recover()
                self.assertEqual(fixture.comment["body"], old_body)
                self.assertFalse(
                    any(method == "PATCH" for _, method, _ in fixture.trace)
                )

    def test_changed_cessation_observation_refuses_before_owner_write(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartupFixture(directory)
            fixture.initial()
            before = fixture.comment["body"]
            fixture.changed_old_job = True
            with self.assertRaisesRegex(ReviewInputError, "changed"):
                fixture.recover()
            self.assertEqual(fixture.comment["body"], before)
            self.assertFalse(any(method == "PATCH" for _, method, _ in fixture.trace))

    def test_changed_trusted_configuration_does_not_enlarge_restored_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartupFixture(directory)
            initial = fixture.initial()
            before = fixture.comment["body"]
            fixture.config_bytes = b"schema: 1\napi_request_limit: 256\n"
            with self.assertRaisesRegex(ReviewInputError, "configuration conflicts"):
                fixture.recover()
            self.assertEqual(fixture.comment["body"], before)
            self.assertEqual(initial.configuration.limit, 128)
            self.assertFalse(any(method == "PATCH" for _, method, _ in fixture.trace))

    def test_lost_owner_write_response_is_read_back_without_repeating_patch(self):
        for fault in ("unknown-stored", "unknown-absent"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = StartupFixture(directory)
                original = fixture.initial()
                fixture.fault = fault
                with self.assertRaises(GitHubPublicationTransientError):
                    fixture.recover()
                # Substitute the missing native success-readback slot with an
                # exact owned reconciliation read, without another window.
                ledger = GitHubIssueCommentSessionLedger(
                    fixture.http,
                    token="synthetic-review_session",
                    app_slug="reviewsensei[bot]",
                )
                readback = ledger.load(IDENTITY)
                retained = ApiRunAccounting.from_document(
                    readback.record.api_run_accounting
                )
                self.assertEqual(
                    retained.owner.run_attempt, 2 if fault == "unknown-stored" else 1
                )
                self.assertEqual(retained.remaining, original.remaining)
                self.assertEqual(len(fixture.trace), 24)
                self.assertEqual(
                    sum(method == "PATCH" for _, method, _ in fixture.trace), 1
                )
                fixture.attempt = 3
                with self.assertRaises(ReviewInputError):
                    fixture.prelude()
                self.assertEqual(len(fixture.trace), 24)

    def test_accounting_is_inside_existing_session_digest_and_schema_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartupFixture(directory)
            fixture.initial()
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            document = record.to_dict()
            document["api_run_accounting"]["configuration"]["limit"] = 256
            with self.assertRaisesRegex(ReviewInputError, "integrity"):
                SessionRecord.from_dict(document)
            old = record.to_dict()
            old["api_run_accounting"]["version"] = "operation-128-v1"
            with self.assertRaises(ReviewInputError):
                SessionRecord.from_dict(old)
            with self.assertRaisesRegex(ReviewInputError, "removed"):
                record.evolve(api_run_accounting=None)
            state = ApiRunAccounting.from_document(record.api_run_accounting)
            with self.assertRaisesRegex(ReviewInputError, "discard"):
                record.evolve(
                    api_run_accounting=replace(state, blocks=()).to_document()
                )
