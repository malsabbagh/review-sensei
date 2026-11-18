"""Bounded initial status route. This planner alone grants no dispatch permit."""

from __future__ import annotations

import hashlib
import json

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError

from .api_run_plan import NATIVE_OPERATION_REQUESTS, READ_OPERATION_REQUESTS

# One fresh admission load, two root writes with refresh, two status loads,
# two exact source/head fences, and the existing grant verification exchange.
STATUS_WORK_CALLS = (
    3 * READ_OPERATION_REQUESTS + 2 * (NATIVE_OPERATION_REQUESTS + 1) + 5
)
STATUS_TERMINAL_CALLS = NATIVE_OPERATION_REQUESTS + 1


class _StatusRoute:
    def __init__(self, run, args, profile, require_source):
        from .review_run import ActionRunLedger, ReviewRun
        from .trust_enrollment import compare_current_observation

        require_source()
        caller = run.caller
        self.require_budget_source(caller)
        original = json.loads(profile.original)["authority"]
        if (
            type(run) is not ReviewRun
            or type(run.ledger) is not ActionRunLedger
            or run.ledger._quarantine_required
            or caller._terminal_reason is not None
            or caller.accounting.action is not None
            or caller._action_unresolved()
            or run.session_grant != profile.session.grant
            or run.session_authority != original
            or run.ledger.token != profile.session.token
            or run.http is not run.ledger.http
            or run.http.api_url != profile.api_origin
            or original["purpose"] != "command"
            or original["source"]["body_sha256"]
            != hashlib.sha256(args.comment_body.encode()).hexdigest()
            or original["repository"] != run.identity.repository
            or original["pull_request"] != run.identity.pull_request
            or original["head_sha"] != run.head_sha
        ):
            raise ReviewInputError("trust status original source scope is unsupported")
        from review_sensei.disposition import parse_maintainer_command

        command = parse_maintainer_command(
            args.comment_body, actor=original["actor"]["login"]
        )
        if command is None or command.action not in {"status", "pause", "continue"}:
            raise ReviewInputError(
                "trust status complete route does not admit other commands"
            )
        self.action = command.action
        # The business replace executes its source/head fence before the
        # independently bounded native operation, including on a lost response.
        extra = (2 + NATIVE_OPERATION_REQUESTS + 1) if self.action != "status" else 0
        # Continue reads the current collaborator permission exactly once.
        extra += int(self.action == "continue")
        caller.budget_view.preflight(STATUS_WORK_CALLS + extra)
        terminal = next(
            (
                b
                for b in caller.accounting.blocks
                if b.block_id == caller._terminal_block
            ),
            None,
        )
        if (
            terminal is None
            or terminal.issued_by != caller.accounting.owner
            or terminal.undispatched < STATUS_TERMINAL_CALLS
        ):
            raise EvidenceBudgetExhausted(
                "trust status terminal refresh and confirmation are unfunded"
            )
        run.ledger._list_refresh = True
        loaded = run.ledger.load(run.identity)
        record = loaded.record
        if (
            loaded.status != "ok"
            or record is None
            or record.record_sha256 != caller._root
            or record.finding_lifecycle is not None
            or record.disposition_snapshot is not None
            or record.quarantine_lineage is not None
            or record.historical_assessments
            or record.transaction is not None
            or record.reservation_id is not None
        ):
            raise ReviewInputError("trust status retained route is unsupported")
        from review_sensei.convergence import DEFAULT_MAX_FAILED_ATTEMPTS
        from review_sensei.session import failed_attempts_for_head

        if (
            self.action == "continue"
            and failed_attempts_for_head(record, run.head_sha)
            >= DEFAULT_MAX_FAILED_ATTEMPTS
        ):
            raise ReviewInputError("trust continue successor route is unsupported")
        compare_current_observation(profile.broker, profile.session, record)
        self.target_paused = self.action == "pause"
        self.changes_pause = self.action != "status" and (
            record.operator_paused != self.target_paused
        )
        self.expected_count = 3 + int(self.changes_pause)
        changed = json.loads(self.business(record))
        changed["operator_paused"] = self.target_paused
        self.changed_business = canonical_bytes(changed)
        self.run, self.profile, self.require_source = run, profile, require_source
        self.baseline = self.business(record)
        self.initial_root = record.record_sha256
        self.generation = record.generation
        self.count = 0
        self.intent = None
        self.body = args.comment_body
        self.source = canonical_bytes(original["source"])

        from review_sensei.api_work import ApiRunAccounting

        persisted = ApiRunAccounting.from_document(record.api_run_accounting)
        current = caller.accounting
        persisted.require_successor(current)
        prior_ids = {p.request_id for p in persisted.completed} | {
            p.request_id for b in persisted.blocks for p in b.pending
        }
        dispatched = sum(
            o["phase"] == "ordinary" and o["request_id"] not in prior_ids
            for o in caller.observations
        )
        old = {b.block_id: b for b in persisted.blocks}
        if (
            current.configuration != persisted.configuration
            or current.owner != persisted.owner
            or {b.block_id for b in current.blocks} != set(old)
            or any(
                b.issued_by != old[b.block_id].issued_by
                or b.undispatched
                != old[b.block_id].undispatched
                - (dispatched if b.block_id == caller._block else 0)
                for b in current.blocks
            )
        ):
            raise ReviewInputError("trust status original allocation changed")
        self._configuration, self._owner = current.configuration, current.owner
        self._work_block, self._terminal_block = caller._block, caller._terminal_block
        self._balances = {b.block_id: b.undispatched for b in current.blocks}
        self._seen = len(caller.observations)

    @staticmethod
    def require_budget_source(caller):
        from review_sensei.api_work import ApiRunAccounting

        from .api_run_budget import CallerRunBudget

        budget = caller.budget_view
        if (
            type(budget) is not CallerRunBudget
            or budget.caller is not caller
            or type(caller.accounting) is not ApiRunAccounting
            or budget._ordinary_hold != 0
            or any(
                name in budget.__dict__
                for name in dir(CallerRunBudget)
                if callable(getattr(CallerRunBudget, name))
            )
        ):
            raise ReviewInputError("trust status budget source changed")

    def require_accounting(self):
        caller = self.run.caller
        self.require_budget_source(caller)
        current = caller.accounting
        expected = self._balances.copy()
        for observation in caller.observations[self._seen :]:
            if observation["phase"] != "ordinary":
                raise ReviewInputError("trust status accounting phase changed")
            block = (
                self._terminal_block
                if observation["lifecycle_phase"] == "terminal"
                else self._work_block
            )
            expected[block] -= 1
        if (
            current.configuration != self._configuration
            or current.owner != self._owner
            or {b.block_id: b.undispatched for b in current.blocks} != expected
            or any(b.issued_by != self._owner for b in current.blocks)
            or caller._block
            != (
                self._terminal_block
                if caller._terminal_reason is not None
                else self._work_block
            )
        ):
            raise ReviewInputError("trust status source allocation changed")
        self._balances, self._seen = expected, len(caller.observations)

    @staticmethod
    def business(record):
        return canonical_bytes(
            {
                k: v
                for k, v in record.to_dict().items()
                if k
                not in {
                    "generation",
                    "updated_at",
                    "expires_at",
                    "record_sha256",
                    "api_run_accounting",
                }
            }
        )

    def require_request(self, caller, request):
        from .session_ledger import parse_session_comment

        self.require_source()
        self.require_accounting()
        run, profile = self.run, self.profile
        candidate = caller._checkpoint
        if (
            caller is not run.caller
            or self.count >= self.expected_count
            or candidate is None
        ):
            raise ReviewInputError("trust status native stage is unavailable")
        try:
            parsed = parse_session_comment(
                json.loads(request.data)["body"], identity=run.identity
            )
        except (ValueError, TypeError, KeyError):
            parsed = None
        expected_prefix = (
            profile.api_origin + f"/repos/{run.identity.repository}/issues/comments/"
        )
        if (
            request.get_method() != "PATCH"
            or not request.full_url.startswith(expected_prefix)
            or not request.full_url[len(expected_prefix) :].isdigit()
            or request.get_header("Authorization") != f"Bearer {profile.session.token}"
            or parsed is None
            or canonical_bytes(parsed.to_dict()) != canonical_bytes(candidate.to_dict())
            or self.business(parsed)
            != (
                self.changed_business
                if self.changes_pause and self.count >= 2
                else self.baseline
            )
            or parsed.generation != self.generation + self.count + 1
            or run.ledger.token != profile.session.token
        ):
            raise ReviewInputError("trust status native root binding changed")
        identifier = int(request.full_url[len(expected_prefix) :])
        current = run.ledger._comments_by_id.get(identifier)
        prior = (
            parse_session_comment(current.get("body"), identity=run.identity)
            if isinstance(current, dict)
            else None
        )
        if (
            not isinstance(current, dict)
            or prior is None
            or prior.record_sha256 != caller._root
            or current.get("user", {}).get("id") != run.ledger._evidence_author_id
            or current.get("user", {}).get("type") != "Bot"
        ):
            raise ReviewInputError("trust status native target changed")
        action = caller.accounting.action
        if action is None or canonical_bytes(action["intent"]["source"]) != self.source:
            raise ReviewInputError("trust status action source changed")
        if self.count == 0:
            if (
                action["intent"]["source_root"] != self.initial_root
                or action["phase"] != "pending"
                or action["execution_started"]
            ):
                raise ReviewInputError("trust status intent transition changed")
        elif self.intent != canonical_bytes(action["intent"]):
            raise ReviewInputError("trust status immutable intent changed")
        if self.count == 1 and (
            action["phase"] != "pending" or not action["execution_started"]
        ):
            raise ReviewInputError("trust status entry transition changed")
        if (
            self.changes_pause
            and self.count == 2
            and (action["phase"] != "pending" or not action["execution_started"])
        ):
            raise ReviewInputError("trust pause business transition changed")
        if self.count == self.expected_count - 1:
            proof = caller._action_result_proof
            if (
                action["phase"] != "terminal"
                or action["outcome"]
                != (
                    {"kind": "explicit_noop", "reason": "status"}
                    if self.action == "status"
                    else {"kind": "applied", "reason": None}
                )
                or proof is None
                or action["result"] != proof.manifest
            ):
                raise ReviewInputError("trust status terminal result changed")

    def consumed(self, caller, request):
        self.require_request(caller, request)
        if self.intent is None:
            self.intent = canonical_bytes(caller.accounting.action["intent"])
        self.count += 1


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
