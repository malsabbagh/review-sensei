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
from review_sensei.errors import ReviewInputError, ReviewSenseiError
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
from .run_authority import request_authority
from .session_ledger import GitHubIssueCommentSessionLedger

_SHA = re.compile(r"[a-f0-9]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")

# Injectable only as the physical external service seam; never selected by JSON.
physical_opener = urlopen


class StartupRunLedger(GitHubIssueCommentSessionLedger):
    """Read retained parts through the original prefunded startup transport.

    Initial comment discovery keeps its existing ten-request pagination bound;
    retained part reads share the same finite source-owned startup slot.
    """

    def __init__(
        self,
        http: GitHubHttp,
        *,
        caller: ApiRunRequestTransport,
        token: str,
        app_slug: str,
        reservation_owner: Mapping[str, object],
    ):
        super().__init__(
            http, token=token, app_slug=app_slug, reservation_owner=reservation_owner
        )
        self._startup_caller = caller
        self.evidence_budget = caller.budget_view

    def _fetch_pages(
        self, identity: SessionIdentity, *, since: str | None
    ) -> list[Any]:
        if (
            self._startup_caller._state is not None
            or self._quarantine_required
            or self._quarantine_full_discovery
        ):
            return super()._fetch_pages(identity, since=since)
        path = self._comments_path(identity)
        if since is not None:
            path += "?since=" + quote(since, safe="")
        return self.http.paginate(path=path, token=self.token)


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


class ActionRunLedger(ApiRunSessionLedger):
    """Current action mutations require their fresh source and head fence."""

    business_guard: Any = None

    def __init__(
        self,
        native: GitHubIssueCommentSessionLedger,
        caller: ApiRunRequestTransport,
        identity: SessionIdentity,
    ):
        super().__init__(native, caller, identity)
        # The application receives this already authenticated caller-owned
        # ledger. Snapshot staging uses its existing conditional mutation and
        # physical caller budget, just as a newly constructed action ledger.
        self.enable_disposition_snapshot_writes = True

    def replace(self, identity, mutate, *, now=None):
        if self.business_guard is not None:
            self.business_guard()
        try:
            return super().replace(identity, mutate, now=now)
        except GitHubPublicationTransientError:
            operation = self.caller._checkpoint_operation
            if (
                self.caller._checkpoint is None
                or operation is None
                or not operation.write_attempted
            ):
                raise
            return self.caller.reconcile_checkpoint(self, identity)


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
    session_authority: Mapping[str, object] | None = None
    session_grant: str | None = field(default=None, repr=False)
    title: str = ""
    actions_token: str | None = field(default=None, repr=False)

    def retained_analysis(self, argv: list[str]) -> CompletedAnalysis | None:
        """Recover a stopped same-run owner's exact checkpoint, without inference."""
        if self.caller.accounting.owner.run_attempt != 2:
            return None
        loaded = self.ledger.load(self.identity)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError("successor checkpoint is unavailable")
        transaction = loaded.record.transaction
        if transaction is None or transaction.phase not in {
            "publication_pending",
            "publication_failed",
        }:
            return None
        from review_sensei.convergence import resolve_review_convergence_policy
        from review_sensei.diff import analyze_diff
        from review_sensei.validation import read_bounded_utf8

        from .publication_context_recovery import reconstruct_publication_contexts
        from .publication_recovery import recover_retained_review_result

        args = engine_parser(include_hosted=True).parse_args(argv)
        if args.diff is None:
            raise ReviewInputError("successor requires the exact original diff")
        diff = read_bounded_utf8(args.diff, maximum=1_048_576, label="original diff")
        policy = resolve_review_convergence_policy(mode=args.review_mode)
        contexts = reconstruct_publication_contexts(
            source_root=Path.cwd(),
            diff=diff,
            ledger=self.ledger,
            identity=self.identity,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy=policy,
        )
        assert self.identity.repository_id is not None
        result = recover_retained_review_result(
            http=self.http,
            token=self.ledger.token,
            repository=self.identity.repository,
            repository_id=self.identity.repository_id,
            pull_request=self.identity.pull_request,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            app_slug="reviewsensei[bot]",
            ledger=self.ledger,
            configuration_context=contexts.configuration,
            evidence_context=contexts.evidence,
            convergence_policy=policy,
            budget=self.caller.budget_view,
        )
        return CompletedAnalysis(
            result,
            contexts.baseline,
            contexts.current_key,
            contexts.configuration,
            contexts.evidence,
            policy,
            diff,
            tuple(analyze_diff(diff, allow_incomplete=True).changed_paths),
        )

    def execute(self, argv: list[str], *, publication_output: Path) -> int:
        retained = self.retained_analysis(argv)
        if retained is None and self.plan.runner_kind == "local":
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
            actions_token=self.actions_token,
        )
        if retained is None:
            code = engine_main(argv, hosted_hooks=hooks)
            if code != 0:
                return code
        else:
            # All values came from independently reconstructed trusted policy
            # and authenticated retained bytes. Diagnostic files confer no
            # handoff authority, and the provider is never instantiated.
            hooks.completed = retained
            hooks._completion_started = True
            self.caller.label_phase("completion")
            from .prior_rescan_publication import admitted_rescan

            loaded = self.ledger.load(self.identity)
            if loaded.record is not None:
                hooks.rescan_request = admitted_rescan(loaded.record)
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
            terminal_reason = None
            try:
                self.caller.begin_terminal("already-published")
                self.caller.finish_review_run(
                    self.ledger, self.identity, hooks.skipped_transaction
                )
            except ReviewSenseiError:
                terminal_reason = "terminal_close_unconfirmed"
            publication_output.write_text(
                json.dumps(
                    {
                        "status": "published_terminal_pending"
                        if terminal_reason
                        else "already_published",
                        "publication_status": "already_published",
                        "accounting_terminal": terminal_reason is None,
                        "terminal_reason": terminal_reason,
                        "review_id": None,
                        "diagnostic": None,
                    }
                )
                + "\n"
            )
            return 1 if terminal_reason else 0
        application = GitHubApplication(
            broker=self.broker,
            http=self.http,
            reviewer=ReviewPublisher(http=self.http),
            learner=LearningPRPublisher(http=self.http),
            replier=ConversationPublisher(http=self.http),
            session_ledger=self.ledger,
        )
        if hooks.rescan_request is not None:
            from .prior_rescan_publication import prior_reader_factory

            application.reviewer.finalizer.prior_publication_reader_factory = (
                prior_reader_factory(
                    self.ledger,
                    self.http,
                    hooks.rescan_request,
                    original_reference=hooks.prior_inventory_reference,
                )
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
        published = publication.status in {
            "published",
            "already_published",
            "auto_approval_disabled",
        }
        terminal_reason = (
            "thread_cleanup_unconfirmed"
            if publication.thread_resolution == "unconfirmed"
            else "publication_finalization_incomplete"
        )
        accounting_terminal = False
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
            try:
                self.caller.finish_review_run(
                    self.ledger, self.identity, analysis.result.transaction
                )
            except ReviewSenseiError:
                terminal_reason = "terminal_close_unconfirmed"
            else:
                accounting_terminal = True
                terminal_reason = None
        publication_output.write_text(
            json.dumps(
                {
                    "status": "published_terminal_pending"
                    if published and not accounting_terminal
                    else publication.status,
                    "publication_status": publication.status,
                    "accounting_terminal": accounting_terminal,
                    "terminal_reason": terminal_reason,
                    "review_id": publication.review_id,
                    "diagnostic": publication.diagnostic,
                },
                indent=2,
            )
            + "\n"
        )
        return 0 if published and accounting_terminal else 1


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
) -> tuple[FrozenApiConfiguration, str]:
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
    ), branch


def bootstrap(
    args: argparse.Namespace,
    *,
    run_directory: Path,
    policy_commit: str,
    purpose: str = "review",
    source: Mapping[str, object] | None = None,
    quarantine_predecessor_comment_id: int | None = None,
) -> ReviewRun:
    """Authenticate one new/recovery owner; no caller document activates capacity."""
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    allowed_events = {
        "review": {"pull_request", "workflow_dispatch", "issue_comment"},
        "command": {"issue_comment"},
        "reply": {"issue_comment", "pull_request_review_comment"},
    }
    if purpose not in allowed_events or event not in allowed_events[purpose]:
        raise ReviewInputError(
            "this review route requires a fresh authenticated PR or dispatch event"
        )
    if (
        purpose == "review"
        and event == "issue_comment"
        and not (args.rescan or args.review_remaining)
    ):
        raise ReviewInputError(
            "issue-comment review requires authenticated explicit intent"
        )
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    metadata_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if quarantine_predecessor_comment_id is not None and (
        purpose != "review"
        or event != "workflow_dispatch"
        or attempt != "1"
        or type(quarantine_predecessor_comment_id) is not int
        or not 1 <= quarantine_predecessor_comment_id <= 9_007_199_254_740_991
        or args.rescan
        or args.review_remaining
    ):
        raise ReviewInputError(
            "quarantine requires one explicit first dispatch and no reanalysis intent"
        )
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
        or purpose == "reply"
        and not plan.policy.mentions
        or event == "pull_request"
        and not plan.policy.automatic_reviews
    ):
        raise ReviewInputError(
            "trusted policy does not enable the current review route"
        )
    job = "command" if purpose == "command" else plan.runner_kind
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
    frozen, policy_branch = _configuration(
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
        or purpose == "reply"
        and not plan.policy.mentions
        or (purpose != "command" and plan.runner_kind != job)
        or event == "pull_request"
        and not plan.policy.automatic_reviews
    ):
        raise ReviewInputError(
            "authenticated policy does not enable this review process"
        )
    if purpose == "review" and plan.policy.learning == "pull-requests":
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
        or head["repo"]["full_name"] != args.repository
        or head["repo"].get("fork") is not False
    ):
        raise ReviewInputError("live reviewed base or head conflicts")
    title = pull.get("title", "")
    if not isinstance(title, str) or len(title.encode("utf-8")) > 256:
        raise ReviewInputError("live pull request title exceeds its 256-byte bound")
    if getattr(args, "title", None) not in {None, "", title}:
        raise ReviewInputError("review title differs from the live pull request")
    # Read-only authority/owner checks precede enrollment's durable witness.
    authority = request_authority(
        purpose=purpose,
        repository=args.repository,
        repository_id=args.repository_id,
        pull_request=args.pull_request,
        head_sha=args.head_sha,
        job=job,
        source=source,
    )
    actions = BrokerClient(
        opener=caller.open,
        control=BrokerDispatchAccounting(budget, operation_id, 60, 60),
        authority=authority,
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
        control=BrokerDispatchAccounting(budget, operation_id, 60, 60),
        authority=authority,
    )
    session = broker.open_session(
        oidc,
        repository_id=args.repository_id,
        pull_request=args.pull_request,
        head_sha=args.head_sha,
    )
    if session.reservation_owner != {"run_id": run_id, "head_sha": args.head_sha}:
        raise ReviewInputError("broker review owner scope conflicts")
    native = StartupRunLedger(
        http,
        caller=caller,
        token=session.token,
        app_slug="reviewsensei[bot]",
        reservation_owner=session.reservation_owner,
    )
    native.authenticated_app_id = session.app_id
    # Capacity uses the existing check capability's read permission, never a
    # workflow token that may intentionally lack Checks. This requests no new
    # installation permission and stays on the same physical caller budget.
    native._quarantine_check_token_provider = lambda: broker.exchange(
        oidc, capability="check_publish"
    )
    from .quarantine_activation import authorize_creation_slot, registrations_for
    from .quarantine_evidence import CurrentReadScope
    from .quarantine_origin import QuarantineOriginReader

    native._quarantine_policy_branch = policy_branch
    registrations = registrations_for(identity)
    native._quarantine_full_discovery = bool(registrations)
    native._quarantine_capacity_cursor = any(
        "published_tail" in row or "prepublication" in row for row in registrations
    )
    if purpose == "command" and source is not None:
        # Capacity only: live source/actor authorization still precedes every
        # command effect. Replay updates this same bounded receipt object.
        native._quarantine_command_receipt_marker = (
            "<!-- reviewsensei:command-receipt:v1 "
            f"source={source['id']} digest={hashlib.sha256(args.comment_body.encode()).hexdigest()} -->"
        )
    native._quarantine_native_tail_parts = (
        3 if purpose == "command" else 8 if purpose == "reply" else 2
    )
    if purpose == "review" or (
        purpose == "reply" and getattr(args, "enable_broader_review", False)
    ):
        from .quarantine_capacity import full_review_comment_tail

        native._quarantine_native_comment_bounds = full_review_comment_tail()
    native._quarantine_origin_reader = QuarantineOriginReader(
        metadata_token=metadata_token,
        actions_token=actions_token,
        current_scope=CurrentReadScope(
            native, identity, args.base_sha, args.head_sha, metadata_token
        ),
    )
    if quarantine_predecessor_comment_id is not None:
        event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
        with event_path.open("rb") as stream:
            event_bytes = stream.read(1_048_577)
        if len(event_bytes) > 1_048_576:
            raise ReviewInputError("quarantine explicit event exceeds its bound")
        native._quarantine_creation_slot = authorize_creation_slot(
            http=http,
            token=actions_token,
            metadata_token=metadata_token,
            identity=identity,
            run_id=caller.run.run_id,
            policy_commit=policy_commit,
            predecessor_comment_id=quarantine_predecessor_comment_id,
            event=json.loads(event_bytes),
            actor=os.environ.get("GITHUB_ACTOR"),
            triggering_actor=os.environ.get("GITHUB_TRIGGERING_ACTOR"),
        )
    loaded = native.load(identity)
    if int(attempt) == 1:
        try:
            if quarantine_predecessor_comment_id is not None:
                from .quarantine_creation import create_quarantine, prepare_quarantine

                if loaded.status != "ok" or loaded.record is None:
                    raise ReviewInputError(
                        "explicit quarantine predecessor cannot be authenticated"
                    )
                prepared = prepare_quarantine(
                    ledger=native,
                    identity=identity,
                    previous=loaded.record,
                    slot=native._quarantine_creation_slot,
                    configuration=frozen,
                    owner=owner,
                    metadata_token=metadata_token,
                    actions_token=actions_token,
                )
                create_quarantine(prepared, actions_token=actions_token)
            elif loaded.status == "missing":
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
            if quarantine_predecessor_comment_id is not None:
                raise
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
        if (
            previous.terminal_transaction is not None
            or previous.action is not None
            and previous.action["phase"] == "terminal"
        ):
            raise ReviewInputError("caller run is already terminal")
        prior = loaded.record
        if (
            purpose == "review"
            and prior.transaction is not None
            and (
                prior.transaction.base_sha != args.base_sha
                or prior.transaction.head_sha != args.head_sha
            )
            or purpose == "review"
            and prior.transaction is None
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
            # Recover against the predecessor's frozen startup and codec.
            # claim_startup preserves that plan; a newly derived caller plan
            # must not change either owner's already frozen allocation basis.
            allocation = owner_allocations(
                frozen.limit,
                previous.startup.liability,
                legacy=previous.version == "caller-requests-v1",
            )
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
    ledger = (ReviewRunLedger if purpose == "review" else ActionRunLedger)(
        native, caller, identity
    )
    publication_broker = BrokerClient(
        opener=caller.open,
        control=BrokerDispatchAccounting(budget, operation_id, 60, 60),
        authority=authority,
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
        session.attestation,
        session.grant,
        title,
        actions_token,
    )


def _has_option(argv: Sequence[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in argv)


def run_review(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="review-sensei-github review-run")
    parser.add_argument("--policy-commit", required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--publication-outcome", type=Path, required=True)
    parser.add_argument("--quarantine-predecessor-comment-id", type=int)
    parser.add_argument("engine_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(list(argv))
    core = options.engine_args
    if core and core[0] == "--":
        core = core[1:]
    if _SHA.fullmatch(options.policy_commit) is None:
        raise ReviewInputError("trusted policy commit is invalid")
    args = engine_parser(include_hosted=True).parse_args(core)
    if args.session_ledger is not None or getattr(args, "local_session", False):
        raise ReviewInputError("this review lifecycle requires a new hosted review")
    if any(
        value is not None
        and (not isinstance(value, str) or _SHA.fullmatch(value) is None)
        for value in (args.base_sha, args.head_sha)
    ):
        raise ReviewInputError("review lifecycle requires exact base and head")
    selected = bootstrap(
        args,
        run_directory=options.run_directory,
        policy_commit=options.policy_commit,
        quarantine_predecessor_comment_id=options.quarantine_predecessor_comment_id,
    )
    core = [*core]
    if not _has_option(core, "--base-sha"):
        core.extend(["--base-sha", selected.base_sha])
    if not _has_option(core, "--head-sha"):
        core.extend(["--head-sha", selected.head_sha])
    if not _has_option(core, "--title") and selected.title:
        core.append("--title=" + selected.title)
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
