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
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from review_sensei_github.api_run_owner import (
    ApiRunActionsEvidence,
)
from review_sensei_github.api_run_requests import (
    NATIVE_REPLACE_CALLER_REQUESTS,
    ApiRunRequestTransport,
)
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.errors import GitHubPublicationTransientError
from review_sensei_github.http import (
    MAX_GITHUB_RESPONSE_BYTES,
    MAX_PAGINATION_PAGES,
    GitHubHttp,
)
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
from tests.fake_github_http import FakeHTTPResponse, json_response

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
NEXT_REPLACE_LIABILITY = NATIVE_REPLACE_CALLER_REQUESTS
# Qualification input only. This does not change DEFAULT_API_REQUEST_LIMIT.
QUALIFICATION_LIMIT = PLAN.liability + 2 * NEXT_REPLACE_LIABILITY


class StartupFixture:
    def __init__(self, directory, *, fanout=30, limit=128):
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
        self.config_bytes = f"schema: 1\napi_request_limit: {limit}\n".encode()
        self.http = None
        self.accounted = None
        self.ledger = None
        self.transports = []
        self.history_count = 0
        self.oversized_first_page = False
        self.list_requests = []
        self.write_modes = []

    def _external_open(self, request, timeout):
        target = urlsplit(request.full_url)
        path, method = target.path, request.method
        # A real pre-dispatch debit must already exist before fake external I/O.
        assert (
            len(self.accounted.observations)
            == sum(a == self.attempt for a, _, _ in self.trace) + 1
        )
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
                    "dispatched": min(
                        self.fanout, payload["control_budget"]["max_dispatches"]
                    ),
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
            query = parse_qs(target.query)
            size, page = (
                int(query.get("per_page", ["100"])[0]),
                int(query.get("page", ["1"])[0]),
            )
            self.list_requests.append((self.attempt, size, page))
            if self.oversized_first_page and size == 100:
                return FakeHTTPResponse(b"x" * (MAX_GITHUB_RESPONSE_BYTES + 1))
            rows = [
                {
                    "id": 1000 + i,
                    "body": "history",
                    "user": {"type": "User", "login": "human", "id": 42},
                }
                for i in range(self.history_count)
            ]
            if self.comment is not None:
                rows.append(copy.deepcopy(self.comment))
            return json_response(rows[(page - 1) * size : page * size])
        if method in {"POST", "PATCH"}:
            self.write_modes.append(
                (
                    self.attempt,
                    self.accounted.observations[-1]["phase"],
                    self.accounted.usable,
                )
            )
            payload = json.loads(request.data)
            if self.fault == "unknown-absent":
                self.fault = None
                raise TimeoutError("synthetic lost write response")
            self.comment = {
                "id": 1_000_000,
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
        self.accounted = ApiRunRequestTransport(
            directory=self.directory,
            run=RUN,
            plan=PLAN,
            attempt=self.attempt,
            opener=self._external_open,
        )
        self.transports.append(self.accounted)
        self.http = GitHubHttp(
            api_url="https://api.github.test", opener=self.accounted.open
        )
        budget = EvidenceReadBudget()
        client = BrokerClient(
            broker_url="https://broker.github.test/api/github/token",
            opener=self.accounted.open,
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
        actions_client = BrokerClient(
            broker_url="https://broker.github.test/api/github/token",
            opener=self.accounted.open,
            control=BrokerDispatchAccounting(budget, "c" * 64, 12, 1),
        )
        actions_token = actions_client.exchange(oidc, capability="review_actions")
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
        self.ledger = ledger
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

    def recover(self, *, reserve_successor=False):
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
            if reserve_successor:
                state = state.reserve("successor", NEXT_REPLACE_LIABILITY)
            return record.evolve(
                generation=record.generation + 1, api_run_accounting=state.to_document()
            )

        if reserve_successor:
            return self.accounted.reserve_and_activate(
                ledger, IDENTITY, mutate, block_id="successor"
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


class ApiPredispatchQualificationTests(unittest.TestCase):
    def test_expired_response_remains_charged_and_cannot_be_read_as_fresh(self):
        clock = [10.0]

        class SlowResponse(FakeHTTPResponse):
            def read(self, size):
                clock[0] = 12.0
                return super().read(size)

        with tempfile.TemporaryDirectory() as directory:
            transport = ApiRunRequestTransport(
                directory=Path(directory),
                run=RUN,
                plan=PLAN,
                attempt=1,
                opener=lambda *a, **k: SlowResponse(b"{}"),
                clock=lambda: clock[0],
            )
            response = transport.open(Request("https://fixture.test"), timeout=1)
            for _ in range(2):
                with self.assertRaisesRegex(
                    ReviewInputError, "original request deadline"
                ):
                    response.read(100)
            self.assertEqual(transport.startup_dispatches, 1)
            self.assertEqual(transport.observations[0]["outcome"], "expired")
            self.assertEqual(transport.observations[0]["deadline_unix_ms"], 11000)

    def test_standard_transport_refuses_hidden_redirect_after_one_debit(self):
        seen = []

        def builder(handler):
            def physical(request, timeout):
                seen.append("first-request")
                handler.redirect_request(
                    request,
                    SimpleNamespace(close=lambda: seen.append("closed")),
                    302,
                    "redirect",
                    {},
                    "https://invalid.test/secret-sentinel",
                )

            return SimpleNamespace(open=physical)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "review_sensei_github.api_run_requests.build_opener",
                side_effect=builder,
            ),
        ):
            transport = ApiRunRequestTransport(
                directory=Path(directory), run=RUN, plan=PLAN, attempt=1, opener=urlopen
            )
            with self.assertRaisesRegex(ReviewInputError, "redirects") as denied:
                transport.open(Request("https://initial.test"), timeout=1)
            self.assertEqual(seen, ["first-request", "closed"])
            self.assertEqual(transport.startup_dispatches, 1)
            self.assertEqual(len(transport.observations), 1)
            self.assertNotIn("secret-sentinel", str(denied.exception))

    def test_128_refuses_successor_reservation_without_refunding_predecessor(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartupFixture(directory)
            original = fixture.initial()
            body = fixture.comment["body"]
            with self.assertRaisesRegex(ReviewInputError, "exceeds"):
                fixture.recover(reserve_successor=True)
            self.assertEqual(original.remaining, 19)
            self.assertEqual(NEXT_REPLACE_LIABILITY - original.remaining, 12)
            self.assertEqual(fixture.comment["body"], body)
            self.assertFalse(
                any(a == 2 and method == "PATCH" for a, method, _ in fixture.trace)
            )
            self.assertEqual(fixture.accounted.usable, 0)
            self.assertEqual(
                sum(t.startup_dispatches for t in fixture.transports),
                len(fixture.trace),
            )
            before = len(fixture.trace)
            with self.assertRaisesRegex(ReviewInputError, "already attempted"):
                fixture.accounted.reserve_and_activate(
                    fixture.ledger, IDENTITY, lambda r: r, block_id="smaller"
                )
            self.assertEqual(len(fixture.trace), before)

    def test_test_only_140_confirms_reservation_before_spending_and_executes_native_replace(
        self,
    ):
        self.assertEqual(QUALIFICATION_LIMIT, 140)
        self.assertEqual(parse_configuration_text("schema: 1\n").api_request_limit, 128)
        for fanout in (9, 12, 30):
            with (
                self.subTest(fanout=fanout),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = StartupFixture(
                    directory, limit=QUALIFICATION_LIMIT, fanout=fanout
                )
                first = fixture.initial()
                activated = fixture.recover(reserve_successor=True)
                self.assertEqual(len(fixture.trace), 24)
                self.assertEqual(activated.remaining, 0)
                self.assertEqual(fixture.accounted.usable, 31)
                self.assertEqual(
                    fixture.write_modes, [(1, "startup", 0), (2, "startup", 0)]
                )
                checkpoint = fixture.accounted.replace_checkpoint(
                    fixture.ledger, IDENTITY
                )
                self.assertEqual(len(fixture.trace), 28)
                live = fixture.accounted.accounting
                self.assertEqual(live.confirmed, 4)
                self.assertEqual(live.uncertain, 0)
                self.assertEqual(live.remaining, 0)
                self.assertEqual(fixture.accounted.usable, 27)
                self.assertEqual(live.blocks[0], first.blocks[0])
                persisted = ApiRunAccounting.from_document(
                    checkpoint.api_run_accounting
                )
                persisted.require_successor(live)
                # Persisted conservative remainder is never restored as new
                # live capacity; two terminal HTTP effects occurred after the
                # checkpoint bytes were sealed, within the prepaid block.
                self.assertEqual(persisted.remaining, live.remaining)
                self.assertEqual(
                    sum(
                        o["phase"] == "ordinary" for o in fixture.accounted.observations
                    ),
                    4,
                )
                before = len(fixture.trace)
                with self.assertRaisesRegex(ReviewInputError, "unfunded"):
                    fixture.accounted.replace_checkpoint(fixture.ledger, IDENTITY)
                self.assertEqual(len(fixture.trace), before)
                with self.assertRaises(ReviewInputError):
                    fixture.accounted.reconcile_activation(fixture.ledger, IDENTITY)
                self.assertEqual(len(fixture.trace), before)

    def test_tenth_terminal_page_and_last_prepaid_request_complete_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartupFixture(directory, limit=QUALIFICATION_LIMIT)
            fixture.history_count = 998
            first = fixture.initial()
            self.assertEqual(len(fixture.trace), len(PLAN.slots[0].requests))
            fixture.recover(reserve_successor=True)
            self.assertEqual(len(fixture.trace), PLAN.liability)
            fixture.accounted.replace_checkpoint(fixture.ledger, IDENTITY)
            self.assertEqual(
                len(fixture.trace), PLAN.liability + NEXT_REPLACE_LIABILITY
            )
            self.assertEqual(fixture.accounted.usable, 0)
            self.assertEqual(fixture.accounted.accounting.confirmed, 31)
            self.assertEqual(fixture.accounted.accounting.blocks[0], first.blocks[0])
            before = len(fixture.trace)
            with self.assertRaises(ReviewInputError):
                fixture.http.request(
                    "GET", "/repos/owner/repo/pulls/7", token="synthetic"
                )
            self.assertEqual(len(fixture.trace), before)

    def test_full_tenth_page_refuses_and_startup_limit_blocks_any_eleventh_tail_request(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartupFixture(directory, limit=QUALIFICATION_LIMIT)
            fixture.history_count = 999
            with self.assertRaises(ReviewInputError):
                fixture.initial()
            self.assertEqual(len(fixture.trace), len(PLAN.slots[0].requests))
            self.assertEqual(fixture.accounted.startup_dispatches, 28)
            self.assertEqual(fixture.accounted.usable, 0)
            before = len(fixture.trace)
            with self.assertRaisesRegex(ReviewInputError, "startup caller allowance"):
                fixture.http.request(
                    "GET", "/repos/owner/repo/pulls/7", token="synthetic"
                )
            self.assertEqual(len(fixture.trace), before)
            self.assertEqual(
                sum(
                    m == "POST" and p.endswith("/issues/7/comments")
                    for _, m, p in fixture.trace
                ),
                1,
            )

    def test_oversized_response_fallback_is_debited_before_each_physical_request(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartupFixture(directory, limit=QUALIFICATION_LIMIT)
            fixture.history_count = 98
            fixture.oversized_first_page = True
            fixture.initial()
            self.assertEqual(len(fixture.trace), 14)
            self.assertEqual(fixture.accounted.startup_dispatches, 14)
            self.assertEqual(
                fixture.list_requests[:3], [(1, 100, 1), (1, 50, 1), (1, 50, 2)]
            )
            fixture.recover(reserve_successor=True)
            before = len(fixture.trace)
            fixture.accounted.replace_checkpoint(fixture.ledger, IDENTITY)
            self.assertEqual(len(fixture.trace) - before, 10)
            self.assertEqual(fixture.accounted.accounting.confirmed, 10)
            self.assertEqual(fixture.accounted.usable, 21)

    def test_unknown_reservation_write_activates_only_matching_readback_once(self):
        for fault in ("unknown-stored", "unknown-absent"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = StartupFixture(directory, limit=QUALIFICATION_LIMIT)
                fixture.initial()
                fixture.fault = fault
                with self.assertRaises(GitHubPublicationTransientError):
                    fixture.recover(reserve_successor=True)
                self.assertEqual(fixture.accounted.usable, 0)
                if fault == "unknown-stored":
                    fixture.accounted.reconcile_activation(fixture.ledger, IDENTITY)
                    self.assertEqual(fixture.accounted.usable, 31)
                else:
                    with self.assertRaisesRegex(ReviewInputError, "unconfirmed"):
                        fixture.accounted.reconcile_activation(fixture.ledger, IDENTITY)
                    self.assertEqual(fixture.accounted.usable, 0)
                self.assertEqual(len(fixture.trace), 24)
                with self.assertRaises(ReviewInputError):
                    fixture.accounted.reconcile_activation(fixture.ledger, IDENTITY)
                self.assertEqual(len(fixture.trace), 24)
                self.assertEqual(sum(m == "PATCH" for _, m, _ in fixture.trace), 1)

    def test_unknown_useful_checkpoint_retains_request_charge_and_never_reposts(self):
        for fault in ("unknown-stored", "unknown-absent"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = StartupFixture(directory, limit=QUALIFICATION_LIMIT)
                first = fixture.initial()
                fixture.recover(reserve_successor=True)
                fixture.fault = fault
                with self.assertRaises(GitHubPublicationTransientError):
                    fixture.accounted.replace_checkpoint(fixture.ledger, IDENTITY)
                if fault == "unknown-stored":
                    fixture.accounted.reconcile_checkpoint(fixture.ledger, IDENTITY)
                else:
                    with self.assertRaisesRegex(ReviewInputError, "unconfirmed"):
                        fixture.accounted.reconcile_checkpoint(fixture.ledger, IDENTITY)
                self.assertEqual(len(fixture.trace), 28)
                live = fixture.accounted.accounting
                self.assertEqual(live.confirmed, 3)
                self.assertEqual(live.uncertain, 1)
                self.assertEqual(live.blocks[0], first.blocks[0])
                self.assertEqual(live.remaining, 0)
                self.assertEqual(fixture.accounted.usable, 27)
                with self.assertRaises(ReviewInputError):
                    fixture.accounted.replace_checkpoint(fixture.ledger, IDENTITY)
                with self.assertRaises(ReviewInputError):
                    fixture.accounted.reconcile_checkpoint(fixture.ledger, IDENTITY)
                self.assertEqual(len(fixture.trace), 28)
                self.assertEqual(sum(m == "PATCH" for _, m, _ in fixture.trace), 2)
