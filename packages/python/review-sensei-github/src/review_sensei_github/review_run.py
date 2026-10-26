"""One source-owned review process; files are diagnostics, never handoff authority.

Infrastructure transports (Git checkout/install/artifact upload) are outside this
caller meter. All business HTTP uses the single startup/ordinary transport.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote
from urllib.request import Request, urlopen

from review_sensei.api_work import (
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
)
from review_sensei.baseline import ReviewBaseline, ReviewContextCacheKey
from review_sensei.cli import _parser as engine_parser
from review_sensei.cli import main as engine_main
from review_sensei.configuration import MAX_CONFIG_BYTES, load_configuration
from review_sensei.errors import ReviewInputError
from review_sensei.hosted import HostedPlan, plan_hosted_execution
from review_sensei.models import ReviewResult, ReviewTransaction
from review_sensei.session import SessionIdentity
from review_sensei.workflow import prepare_diff

from .api_run_host import (
    ApiRunReviewHooks,
    policy_bytes,
    review_startup_plan,
)
from .api_run_owner import ApiRunActionsEvidence
from .api_run_plan import owner_allocations
from .api_run_requests import ApiRunRequestTransport, ApiRunSessionLedger
from .application import GitHubApplication, GitHubWriteOptions
from .broker_client import BrokerClient
from .broker_control import BrokerDispatchAccounting
from .conversation import ConversationPublisher
from .errors import GitHubPublicationTransientError
from .http import GitHubHttp
from .learning_pr import LearningPRPublisher
from .publication import PublicationResult, ReviewPublisher
from .session_ledger import GitHubIssueCommentSessionLedger

_SHA = re.compile(r"[a-f0-9]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")

# Injectable only as the physical external service seam; never selected by JSON.
physical_opener = urlopen


@dataclass(frozen=True)
class CompletedAnalysis:
    result: ReviewResult = field(repr=False)
    baseline: ReviewBaseline | None
    current_key: ReviewContextCacheKey
    configuration_context: Mapping[str, object] = field(repr=False)
    evidence_context: Mapping[str, object] = field(repr=False)
    policy: Any
    diff: str = field(repr=False)
    changed_paths: tuple[str, ...]


class ReviewRunHooks(ApiRunReviewHooks):
    """Capture validated in-process engine values after durable checkpointing."""

    completed: CompletedAnalysis | None = None
    skipped_transaction: ReviewTransaction | None = None

    def skip_completed_analysis(self, ledger, record, decision):
        from review_sensei.convergence import RoundAdmissionDecision
        from review_sensei.session import SessionRecord

        if (
            ledger is not self.ledger
            or not isinstance(record, SessionRecord)
            or not isinstance(decision, RoundAdmissionDecision)
            or decision.handoff
            or decision.admit
            or decision.handoff_reason is not None
            or record.record_sha256 != self.ledger.caller._root
            or record.transaction is None
            or record.transaction.phase != "publication_succeeded"
            or record.transaction.base_sha != self.base_sha
            or record.transaction.head_sha != self.head_sha
            or record.reservation_id is not None
        ):
            return
        self.skipped_transaction = record.transaction

    def complete_analysis(
        self,
        *,
        ledger,
        identity,
        result,
        baseline,
        current_key,
        configuration_context,
        evidence_context,
        policy,
        diff,
        changed_paths,
    ):
        if (
            self.completed is not None
            or ledger is not self.ledger
            or identity != self.identity
            or not self._completion_started
            or not isinstance(result, ReviewResult)
            or not isinstance(result.transaction, ReviewTransaction)
            or not isinstance(current_key, ReviewContextCacheKey)
            or baseline is not None
            and not isinstance(baseline, ReviewBaseline)
            or not isinstance(configuration_context, Mapping)
        ):
            raise ReviewInputError(
                "review completion does not own the admitted process"
            )
        if (
            result.transaction.configuration_digest
            != ReviewTransaction.compute_configuration_digest(configuration_context)
        ):
            raise ReviewInputError("review completion configuration conflicts")
        self.completed = CompletedAnalysis(
            result,
            baseline,
            current_key,
            copy.deepcopy(dict(configuration_context)),
            copy.deepcopy(dict(evidence_context)),
            policy,
            diff,
            tuple(changed_paths),
        )


class ReviewRunLedger(ApiRunSessionLedger):
    """One funded exact readback on an uncertain write, never a replay."""

    def replace(self, identity, mutate, *, now=None):
        try:
            confirmed = super().replace(identity, mutate, now=now)
        except GitHubPublicationTransientError:
            operation = self.caller._checkpoint_operation
            if (
                self.caller._checkpoint is None
                or operation is None
                or not operation.write_attempted
            ):
                raise
            confirmed = self.caller.reconcile_checkpoint(self, identity)
        return self.caller.close_analysis_abort(self, identity, confirmed)


@dataclass(frozen=True)
class ReviewRun:
    caller: ApiRunRequestTransport
    ledger: ApiRunSessionLedger
    http: GitHubHttp
    broker: BrokerClient = field(repr=False)
    oidc: str = field(repr=False)
    identity: SessionIdentity
    plan: HostedPlan
    base_sha: str
    head_sha: str
    base_branch: str
    head_repository: str
    configuration_path: Path | None

    def execute(self, argv: list[str], *, publication_output: Path) -> int:
        if self.plan.runner_kind == "local":
            # Provider discovery is caller API work; inference remains governed
            # by the independent provider budget. Never send a GitHub credential.
            request = Request(
                self.plan.base_url.rstrip("/") + "/tags",
                headers={"Accept": "application/json"},
                method="GET",
            )
            with self.caller.open(request, timeout=10) as response:
                raw = response.read(512 * 1024 + 1)
                if getattr(response, "status", None) != 200 or len(raw) > 512 * 1024:
                    raise ReviewInputError("local model discovery is unavailable")
                models = json.loads(raw)
            if (
                not isinstance(models, dict)
                or not isinstance(models.get("models"), list)
                or not any(
                    isinstance(model, dict) and model.get("name") == self.plan.model
                    for model in models["models"]
                )
            ):
                raise ReviewInputError("planned local model is unavailable")
        hooks = ReviewRunHooks(
            self.ledger,
            self.identity,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            oidc_token=self.oidc,
        )
        code = engine_main(argv, hosted_hooks=hooks)
        if code != 0:
            return code
        # A later diagnostic file cannot replace the validated result or context.
        raw = policy_bytes(self.configuration_path)
        if (
            hashlib.sha256(raw).hexdigest()
            != self.caller.accounting.configuration.digest
        ):
            raise ReviewInputError("frozen policy configuration changed during review")
        analysis = hooks.completed
        if analysis is None:
            if hooks.skipped_transaction is None:
                raise ReviewInputError(
                    "review produced no completed in-process analysis"
                )
            current = _get(
                self.http,
                self.http.repository_path(
                    self.identity.repository, f"/pulls/{self.identity.pull_request}"
                ),
                self.ledger.token,
            )
            if (
                current.get("head", {}).get("sha") != self.head_sha
                or current.get("base", {}).get("sha") != self.base_sha
            ):
                raise ReviewInputError("already published review head or base changed")
            self.caller.begin_terminal("already-published")
            self.caller.finish_review_run(
                self.ledger, self.identity, hooks.skipped_transaction
            )
            publication_output.write_text(
                json.dumps(
                    {
                        "status": "already_published",
                        "review_id": None,
                        "diagnostic": None,
                    }
                )
                + "\n"
            )
            return 0
        application = GitHubApplication(
            broker=self.broker,
            http=self.http,
            reviewer=ReviewPublisher(http=self.http),
            learner=LearningPRPublisher(http=self.http),
            replier=ConversationPublisher(http=self.http),
            session_ledger=self.ledger,
        )
        publication: PublicationResult = hooks.publish_review(
            application,
            options=GitHubWriteOptions(
                github_writes=True,
                auto_review=True,
                auto_approve=self.plan.policy.reviews == "auto-approve",
                complete_inventory=True,
            ),
            oidc_token=self.oidc,
            repository=self.identity.repository,
            repository_id=self.identity.repository_id,
            pull_request=self.identity.pull_request,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            base_branch=self.base_branch,
            result=analysis.result,
            diff=analysis.diff,
            app_slug="reviewsensei[bot]",
            convergence_policy=analysis.policy,
            configuration_context=analysis.configuration_context,
            evidence_context=analysis.evidence_context,
            baseline=analysis.baseline,
            current_key=analysis.current_key,
            changed_paths=analysis.changed_paths,
        )
        if (
            publication.status
            in {"published", "already_published", "auto_approval_disabled"}
            and publication.diagnostic
            in {
                None,
                "human_adjudication_open",
                "approval_withheld",
                "review_incomplete",
            }
            and publication.thread_resolution != "unconfirmed"
        ):
            self.caller.finish_review_run(
                self.ledger, self.identity, analysis.result.transaction
            )
        publication_output.write_text(
            json.dumps(
                {
                    "status": publication.status,
                    "review_id": publication.review_id,
                    "diagnostic": publication.diagnostic,
                },
                indent=2,
            )
            + "\n"
        )
        return (
            0
            if publication.status
            in {"published", "already_published", "auto_approval_disabled"}
            else 1
        )


def _get(http: GitHubHttp, path: str, token: str) -> dict[str, Any]:
    status, value = http.request("GET", path, token=token)
    if status != 200 or not isinstance(value, dict):
        raise ReviewInputError("authenticated review startup evidence is unavailable")
    return value


def _configuration(
    http: GitHubHttp,
    repository: str,
    token: str,
    policy_commit: str,
    path: Path | None,
    repository_id: int,
) -> FrozenApiConfiguration:
    info = _get(http, http.repository_path(repository), token)
    if (
        type(info.get("id")) is not int
        or info["id"] != repository_id
        or info.get("full_name") != repository
        or info.get("fork") is not False
    ):
        raise ReviewInputError("authenticated repository policy identity conflicts")
    branch = info.get("default_branch")
    if (
        not isinstance(branch, str)
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
        or ".." in branch
        or "//" in branch
        or branch.startswith("/")
        or branch.endswith("/")
    ):
        raise ReviewInputError("default policy branch identity is invalid")
    reference = _get(
        http,
        http.repository_path(repository, "/git/ref/heads/" + quote(branch, safe="/")),
        token,
    )
    obj = reference.get("object")
    if (
        not isinstance(obj, dict)
        or obj.get("type") != "commit"
        or obj.get("sha") != policy_commit
    ):
        raise ReviewInputError("trusted policy commit changed before admission")
    status, document = http.request(
        "GET",
        http.repository_path(
            repository, "/contents/.reviewsensei.yml?ref=" + policy_commit
        ),
        token=token,
    )
    raw = b""
    if status == 200:
        if (
            not isinstance(document, dict)
            or document.get("type") != "file"
            or document.get("encoding") != "base64"
            or not isinstance(document.get("content"), str)
            or type(document.get("size")) is not int
            or not 0 <= document["size"] <= MAX_CONFIG_BYTES
        ):
            raise ReviewInputError("authenticated policy file is invalid")
        try:
            raw = base64.b64decode(document["content"].replace("\n", ""), validate=True)
        except ValueError as error:
            raise ReviewInputError("authenticated policy file is invalid") from error
        if len(raw) != document["size"]:
            raise ReviewInputError("authenticated policy file size conflicts")
    elif status != 404:
        raise ReviewInputError("authenticated policy file is unavailable")
    local = policy_bytes(path)
    if raw != local:
        raise ReviewInputError("local policy bytes differ from authenticated commit")
    configuration = load_configuration(path)
    return FrozenApiConfiguration(
        policy_commit, hashlib.sha256(raw).hexdigest(), configuration.api_request_limit
    )


def bootstrap(
    args: argparse.Namespace, *, run_directory: Path, policy_commit: str
) -> ReviewRun:
    """Authenticate one new/recovery owner; no caller document activates capacity."""
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event not in {"pull_request", "workflow_dispatch"}:
        raise ReviewInputError(
            "this review route requires a fresh authenticated PR or dispatch event"
        )
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    metadata_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if (
        not isinstance(args.repository, str)
        or _REPOSITORY.fullmatch(args.repository) is None
        or type(args.repository_id) is not int
        or args.repository_id < 1
        or type(args.pull_request) is not int
        or args.pull_request < 1
        or not re.fullmatch(r"[1-9][0-9]*", run_id)
        or attempt not in {"1", "2"}
        or not isinstance(metadata_token, str)
        or not metadata_token.strip()
    ):
        raise ReviewInputError(
            "review startup identity or read capability is unavailable"
        )
    # The trusted checkout is infrastructure; authenticate its exact policy SHA.
    try:
        checked_out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.PIPE
        ).strip()
    except (subprocess.SubprocessError, OSError) as error:
        raise ReviewInputError("trusted policy checkout is unavailable") from error
    if checked_out != policy_commit:
        raise ReviewInputError("trusted policy checkout identity conflicts")
    local_path = (
        Path(args.config) if args.config is not None else Path(".reviewsensei.yml")
    )
    if (
        local_path.is_symlink()
        or local_path.resolve() != (Path.cwd() / ".reviewsensei.yml").resolve()
    ):
        raise ReviewInputError(
            "review policy must come from the trusted repository root"
        )
    configuration_path: Path | None = local_path if local_path.is_file() else None
    object_name = policy_commit + ":.reviewsensei.yml"
    size = subprocess.run(
        ["git", "cat-file", "-s", object_name], capture_output=True, text=True
    )
    if size.returncode == 0:
        if (
            not size.stdout.strip().isdigit()
            or int(size.stdout.strip()) > MAX_CONFIG_BYTES
        ):
            raise ReviewInputError("trusted policy object exceeds its byte bound")
        committed = subprocess.check_output(
            ["git", "show", object_name], stderr=subprocess.PIPE
        )
        if committed != policy_bytes(configuration_path):
            raise ReviewInputError("policy working copy differs from its commit")
    elif configuration_path is not None:
        raise ReviewInputError("policy file is absent from its claimed commit")
    plan = plan_hosted_execution(
        load_configuration(configuration_path),
        cli_provider=args.provider,
        cli_model=args.model,
    )
    if (
        not plan.policy.writes
        or plan.policy.operation_entry != "enabled"
        or event == "pull_request"
        and not plan.policy.automatic_reviews
    ):
        raise ReviewInputError(
            "trusted policy does not enable the current review route"
        )
    job = plan.runner_kind
    startup = review_startup_plan(job)
    # Source-controlled startup and both execution owners must fit even before
    # authentication requests. A malformed/undersized local config cannot start
    # requests by relying on a later failure at the first write.
    owner_allocations(
        load_configuration(configuration_path).api_request_limit,
        startup.liability,
    )
    caller = ApiRunRequestTransport(
        directory=run_directory,
        run=ApiRunIdentity(args.repository_id, int(run_id)),
        plan=startup,
        job=job,
        attempt=int(attempt),
        opener=physical_opener,
    )
    budget = caller.budget_view
    operation_id = hashlib.sha256(
        f"review-run:{args.repository_id}:{run_id}".encode()
    ).hexdigest()
    from .publication_recovery import _PaginationObservationHttp

    http = _PaginationObservationHttp(GitHubHttp(opener=caller.open), budget)
    frozen = _configuration(
        http,
        args.repository,
        metadata_token,
        policy_commit,
        configuration_path,
        args.repository_id,
    )
    plan = plan_hosted_execution(
        load_configuration(configuration_path),
        cli_provider=args.provider,
        cli_model=args.model,
    )
    if (
        not plan.policy.writes
        or plan.policy.operation_entry != "enabled"
        or plan.runner_kind != job
        or event == "pull_request"
        and not plan.policy.automatic_reviews
    ):
        raise ReviewInputError(
            "authenticated policy does not enable this review process"
        )
    if plan.policy.learning == "pull-requests":
        raise ReviewInputError(
            "learning publication is not qualified in this lifecycle"
        )
    pull = _get(
        http,
        http.repository_path(args.repository, f"/pulls/{args.pull_request}"),
        metadata_token,
    )
    base, head = pull.get("base"), pull.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise ReviewInputError("live reviewed revision metadata is invalid")
    if args.base_sha is None:
        args.base_sha = base.get("sha")
    if args.head_sha is None:
        args.head_sha = head.get("sha")
    if (
        not isinstance(base, dict)
        or not isinstance(head, dict)
        or not isinstance(args.base_sha, str)
        or _SHA.fullmatch(args.base_sha) is None
        or not isinstance(args.head_sha, str)
        or _SHA.fullmatch(args.head_sha) is None
        or base.get("sha") != args.base_sha
        or head.get("sha") != args.head_sha
        or not isinstance(base.get("repo"), dict)
        or type(base["repo"].get("id")) is not int
        or base["repo"].get("id") != args.repository_id
        or base["repo"].get("full_name") != args.repository
        or type(pull.get("number")) is not int
        or pull.get("number") != args.pull_request
        or pull.get("state") != "open"
        or pull.get("draft") is not False
        or not isinstance(base.get("ref"), str)
        or re.fullmatch(r"[A-Za-z0-9._/-]+", base["ref"]) is None
        or ".." in base["ref"]
        or "//" in base["ref"]
        or not isinstance(head.get("repo"), dict)
        or not isinstance(head["repo"].get("full_name"), str)
        or _REPOSITORY.fullmatch(head["repo"]["full_name"]) is None
    ):
        raise ReviewInputError("live reviewed base or head conflicts")
    # Read-only authority/owner checks precede enrollment's durable witness.
    actions = BrokerClient(
        opener=caller.open,
        control=BrokerDispatchAccounting(budget, operation_id, 12, 1),
    )
    oidc = actions.request_oidc_token()
    actions_token = actions.exchange(oidc, capability="review_actions")
    evidence = ApiRunActionsEvidence(
        http, token=actions_token, command_run_id=run_id, job=job
    )
    owner = evidence.current_owner(
        SessionIdentity(
            args.repository, args.pull_request, repository_id=args.repository_id
        ),
        attempt=int(attempt),
        generation=0,
        event=event,
    )
    identity = SessionIdentity(
        args.repository, args.pull_request, repository_id=args.repository_id
    )
    broker = BrokerClient(
        opener=caller.open,
        control=BrokerDispatchAccounting(budget, operation_id, 30, 1),
    )
    session = broker.open_session(
        oidc,
        repository_id=args.repository_id,
        pull_request=args.pull_request,
        head_sha=args.head_sha,
    )
    if session.reservation_owner != {"run_id": run_id, "head_sha": args.head_sha}:
        raise ReviewInputError("broker review owner scope conflicts")
    native = GitHubIssueCommentSessionLedger(
        http,
        token=session.token,
        app_slug="reviewsensei[bot]",
        reservation_owner=session.reservation_owner,
    )
    loaded = native.load(identity)
    if int(attempt) == 1:
        try:
            if loaded.status == "missing":
                caller.initialize_and_activate(native, identity, frozen, owner)
            elif loaded.status == "ok" and loaded.record is not None:
                previous_root = loaded.record
                previous = ApiRunAccounting.from_document(
                    previous_root.api_run_accounting
                )
                if previous.identity == caller.run:
                    raise ReviewInputError("caller run already started")
                stopped = evidence.ceased_run_owner(
                    identity, previous.identity.run_id, previous.owner
                )

                def require_cessation():
                    if (
                        evidence.ceased_run_owner(
                            identity, previous.identity.run_id, previous.owner
                        )
                        != stopped
                    ):
                        raise ReviewInputError("previous caller cessation changed")

                caller.rollover_and_activate(
                    native, identity, frozen, owner, previous_root, require_cessation
                )
            else:
                raise ReviewInputError("previous caller root cannot be authenticated")
        except GitHubPublicationTransientError:
            if caller._activation is None or caller._last_method not in {
                "POST",
                "PATCH",
            }:
                raise
            caller.reconcile_activation(native, identity)
            caller.bind_terminal_reservation("hosted-terminal")
    else:
        if (
            loaded.status != "ok"
            or loaded.record is None
            or loaded.record.api_run_accounting is None
        ):
            raise ReviewInputError("original caller accounting cannot be established")
        previous = ApiRunAccounting.from_document(loaded.record.api_run_accounting)
        previous.require_binding(caller.run, frozen)
        if previous.terminal_transaction is not None:
            raise ReviewInputError("caller run is already terminal")
        prior = loaded.record
        if (
            prior.transaction is not None
            and (
                prior.transaction.base_sha != args.base_sha
                or prior.transaction.head_sha != args.head_sha
            )
            or prior.transaction is None
            and prior.reservation_id is not None
        ):
            raise ReviewInputError(
                "original reviewed transaction cannot be established"
            )
        stopped = evidence.ceased_owner(identity, previous.owner)
        owner = replace(owner, generation=previous.owner.generation + 1)

        def recover(record):
            if (
                record.record_sha256 != prior.record_sha256
                or evidence.ceased_owner(identity, previous.owner) != stopped
            ):
                raise ReviewInputError("review predecessor or checkpoint changed")
            allocation = owner_allocations(frozen.limit, caller.plan.liability)
            state = (
                previous.claim_startup(owner)
                .reserve("successor-review", allocation.successor_work)
                .reserve("successor-terminal", allocation.successor_terminal)
            )
            return record.evolve(
                generation=record.generation + 1, api_run_accounting=state.to_document()
            )

        caller.reserve_and_activate(
            native, identity, recover, block_id="successor-review"
        )
        caller.bind_terminal_reservation("successor-terminal")
    ledger = ReviewRunLedger(native, caller, identity)
    publication_broker = BrokerClient(
        opener=caller.open,
        control=BrokerDispatchAccounting(budget, operation_id, 10, 1),
    )
    return ReviewRun(
        caller,
        ledger,
        http,
        publication_broker,
        oidc,
        identity,
        plan,
        args.base_sha,
        args.head_sha,
        base["ref"],
        head.get("repo", {}).get("full_name", args.repository),
        configuration_path,
    )


def _has_option(argv: Sequence[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in argv)


def run_review(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="review-sensei-github review-run")
    parser.add_argument("--policy-commit", required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--publication-outcome", type=Path, required=True)
    parser.add_argument("engine_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(list(argv))
    core = options.engine_args
    if core and core[0] == "--":
        core = core[1:]
    if _SHA.fullmatch(options.policy_commit) is None:
        raise ReviewInputError("trusted policy commit is invalid")
    args = engine_parser(include_hosted=True).parse_args(core)
    if (
        args.session_ledger is not None
        or getattr(args, "local_session", False)
        or getattr(args, "rescan", False)
        or getattr(args, "review_remaining", False)
    ):
        raise ReviewInputError("this review lifecycle requires a new hosted review")
    if any(
        value is not None
        and (not isinstance(value, str) or _SHA.fullmatch(value) is None)
        for value in (args.base_sha, args.head_sha)
    ):
        raise ReviewInputError("review lifecycle requires exact base and head")
    selected = bootstrap(
        args, run_directory=options.run_directory, policy_commit=options.policy_commit
    )
    core = [*core]
    if not _has_option(core, "--base-sha"):
        core.extend(["--base-sha", selected.base_sha])
    if not _has_option(core, "--head-sha"):
        core.extend(["--head-sha", selected.head_sha])
    if args.config is None and selected.configuration_path is not None:
        core.extend(["--config", str(selected.configuration_path)])
    if args.diff is None:
        diff = options.run_directory / "review.patch"
        prepare_diff(
            repository=Path.cwd(),
            base_ref=selected.base_sha,
            head_ref=selected.head_sha,
            head_repository=selected.head_repository
            if selected.head_repository != selected.identity.repository
            else None,
            output=diff,
        )
        core.extend(["--diff", str(diff)])
    if not args.transaction:
        core.append("--transaction")
    if not args.github_session_ledger:
        core.append("--github-session-ledger")
    if args.output is None:
        core.extend(["--output", str(options.run_directory / "review.json")])
    if not _has_option(core, "--format"):
        core.extend(["--format", "json"])
    if not _has_option(core, "--exit-semantics"):
        core.extend(["--exit-semantics", "operational"])
    return selected.execute(core, publication_output=options.publication_outcome)
