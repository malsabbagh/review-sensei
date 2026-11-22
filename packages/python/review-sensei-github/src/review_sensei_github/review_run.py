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
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Protocol, Sequence, TypedDict, cast
from urllib.parse import quote
from urllib.request import Request, urlopen

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    ApiRunIdentity,
    FrozenApiConfiguration,
)
from review_sensei.baseline import (
    ReviewBaseline,
    ReviewContextCacheKey,
    admission_context_document,
)
from review_sensei.cli import _parser as engine_parser
from review_sensei.cli import main as engine_main
from review_sensei.configuration import MAX_CONFIG_BYTES, load_configuration
from review_sensei.disposition import MAINTAINER_ACTIONS
from review_sensei.errors import ReviewInputError, ReviewSenseiError
from review_sensei.hosted import HostedPlan, plan_hosted_execution
from review_sensei.models import ReviewResult, ReviewTransaction
from review_sensei.outcomes import DEFAULT_RECOVERY_TTL_SECONDS
from review_sensei.recovery_context import (
    RecoveryContextStore,
    resolve_recovery_context_store,
)
from review_sensei.session import SessionIdentity, SessionRecord
from review_sensei.workflow import prepare_diff

from . import review_lifecycle_diagnostics as lifecycle_diagnostics
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
from .quarantine_capacity import _install_guarded_ledger_replaces
from .review_finalization_diagnostics import close_not_entered, close_review_run
from .run_authority import request_authority
from .session_ledger import GitHubIssueCommentSessionLedger

_SHA = re.compile(r"[a-f0-9]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")


def _read_cached_manifest_in_memory(
    ledger: Any,
    identity: SessionIdentity,
    manifest: object,
) -> object | None:
    """Decode inline or already-cached partitioned evidence with zero HTTP or cache writes."""
    from review_sensei.bounded_evidence import (
        MAX_PART_BYTES,
        AuthenticatedPart,
        canonical_bytes,
        read_inline_evidence,
        read_partitioned_evidence,
    )

    if not isinstance(manifest, dict):
        return None
    binding = manifest.get("binding")
    if not isinstance(binding, dict):
        raise ReviewInputError("finding lifecycle manifest binding is invalid")
    encoding = manifest.get("encoding")
    if encoding == "inline-json-v1":
        return read_inline_evidence(manifest, expected_binding=binding)
    if encoding != "partitioned-json-v1":
        return None
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    budget = getattr(ledger, "evidence_budget", None)
    verified_parts = getattr(ledger, "_verified_parts", None)
    comments_by_id = getattr(ledger, "_comments_by_id", None)
    part_is_listed = getattr(ledger, "_part_is_listed", None)
    if (
        budget is None
        or not isinstance(verified_parts, dict)
        or not isinstance(comments_by_id, dict)
        or not callable(part_is_listed)
    ):
        return None
    for ref in parts:
        if not isinstance(ref, dict):
            return None
        sid = ref.get("storage_id")
        if not isinstance(sid, str) or re.fullmatch(r"[1-9][0-9]{0,18}", sid) is None:
            return None
        if sid not in verified_parts and not part_is_listed(sid):
            return None

    def cached_reader(storage_id: str) -> AuthenticatedPart:
        cached = verified_parts.get(storage_id)
        if cached is not None:
            return cached
        item = comments_by_id.get(int(storage_id))
        owned = ledger._part_owner(item, identity)
        if type(owned.get("id")) is not int or owned.get("id") != int(storage_id):
            raise ReviewInputError("GitHub partition identity does not match")
        body = owned.get("body")
        if (
            not isinstance(body, str)
            or len(body.encode()) > MAX_PART_BYTES
            or not body.startswith("ReviewSensei immutable evidence part v1\n```json\n")
            or not body.endswith("\n```\n<!-- reviewsensei:evidence-part:v1 -->")
        ):
            raise ReviewInputError("GitHub partition framing is invalid")
        raw = body.split("\n```json\n", 1)[1].rsplit("\n```\n", 1)[0]
        try:
            document = json.loads(raw)
            if canonical_bytes(document).decode() != raw:
                raise ValueError("noncanonical part")
        except (ValueError, UnicodeError) as exc:
            raise ReviewInputError("GitHub partition JSON is invalid") from exc
        return AuthenticatedPart(document, ledger.evidence_producer())

    return read_partitioned_evidence(
        manifest,
        reader=cached_reader,
        expected_binding=binding,
        budget=budget,
    )


_CONFIRMED_WITHHELD_CLOSE_DIAGNOSTICS = frozenset(
    {
        "required_fixes_open",
        "human_adjudication_open",
        "qualification_unverified",
        "app_authored",
        "baseline_capacity_exceeded",
        "review_threads_incomplete",
        "historical_assessment_unverified",
        "quarantine_assessment_unverified",
    }
)
_NON_ZERO_CLOSED_DIAGNOSTICS = frozenset({"baseline_capacity_exceeded"})

# Injectable only as the physical external service seam; never selected by JSON.
physical_opener = urlopen


class _PreRolloverMigrationHold(ReviewInputError):
    """Internal pre-rollover hold emitted as a structured migration handoff."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _write_migration_hold_outcome(publication_output: Path, *, reason: str) -> int:
    migration_outcome: dict[str, object] = {
        "status": "handoff",
        "publication_status": "handoff",
        "accounting_terminal": False,
        "terminal_reason": reason,
        "review_id": None,
        "diagnostic": "durable_finding_inventory_recovery_required",
    }
    lifecycle_diagnostics.publication(migration_outcome)
    publication_output.write_text(json.dumps(migration_outcome, indent=2) + "\n")
    return 1


def _is_ordinary_terminal_predecessor(
    record: SessionRecord, accounting: ApiRunAccounting
) -> bool:
    """Return True only when an ordinary predecessor run closed cleanly."""
    if (
        accounting.uncertain != 0
        or accounting.analysis_abort is not None
        or record.reservation_id is not None
        or record.reserved_slot is not None
    ):
        return False
    if accounting.action is not None:
        return accounting.action.get("phase") == "terminal"
    return (
        record.transaction is not None
        and record.transaction.phase
        in {"publication_succeeded", "publication_suppressed"}
        and accounting.terminal_transaction == record.transaction.transaction_id
    )


def _expected_transaction_digests(
    args: argparse.Namespace,
    *,
    configuration_path: Path | None,
) -> tuple[str, str]:
    """Compute exact (policy_digest, configuration_digest) matching review_sensei.cli."""
    from review_sensei import cli as core_cli
    from review_sensei.cli import (
        _CANONICAL_BACKEND_ADAPTERS,
        _adapter_for_provider,
        _cli_option_set,
        _configured_resource_budget,
        _explicit_cli_value,
        _provider_settings_from_args,
        _review_environ,
        _transaction_provider_identity,
    )
    from review_sensei.configuration import resolve_inference
    from review_sensei.convergence import resolve_review_convergence_policy
    from review_sensei.models import (
        build_transaction_configuration_context,
        transaction_stage_identity,
    )
    from review_sensei.outcomes import ResourceBudget
    from review_sensei.providers.openrouter import OpenRouterRoutingPolicy
    from review_sensei.providers.profiles import get_provider_profile
    from review_sensei.service import DEFAULT_STAGES, review_work_policy_digest
    from review_sensei.stages import (
        category_catalog_for_configured_stages,
        load_stages_from_dir,
    )

    work_configuration = load_configuration(configuration_path)
    categories_dir = getattr(args, "categories_dir", None)
    stages_dir = getattr(args, "stages_dir", None)
    if categories_dir and not stages_dir:
        raise ReviewInputError("--categories-dir requires --stages-dir")
    stages = None
    if stages_dir:
        stages = load_stages_from_dir(
            stages_dir,
            category_catalog=category_catalog_for_configured_stages(categories_dir),
        )
    policy = resolve_review_convergence_policy(
        mode=getattr(args, "review_mode", None),
        quality=work_configuration.review.quality,
    )
    resolved_args = copy.copy(args)
    explicit = getattr(resolved_args, "_explicit_cli_options", None)
    if explicit is None:
        explicit = {
            f"--{flag}"
            for flag, attr in (
                ("provider", "provider"),
                ("model", "model"),
                ("base-url", "base_url"),
                ("api-key-env", "api_key_env"),
                ("timeout-seconds", "timeout_seconds"),
            )
            if getattr(resolved_args, attr, None) is not None
        }
        resolved_args._explicit_cli_options = explicit
    for attr, default in (
        ("provider", None),
        ("model", None),
        ("base_url", None),
        ("api_key_env", None),
        ("timeout_seconds", None),
        ("allow_custom_endpoint", False),
        ("fixture_response", None),
    ):
        if not hasattr(resolved_args, attr):
            setattr(resolved_args, attr, default)
    argv_stub: list[str] = []
    cli_provider = _explicit_cli_value(resolved_args, argv_stub, "provider")
    cli_model = _explicit_cli_value(resolved_args, argv_stub, "model")
    cli_base_url = _explicit_cli_value(resolved_args, argv_stub, "base-url")
    cli_api_key_env = _explicit_cli_value(resolved_args, argv_stub, "api-key-env")
    for option, value in (
        ("--provider", cli_provider),
        ("--model", cli_model),
        ("--base-url", cli_base_url),
        ("--api-key-env", cli_api_key_env),
    ):
        if isinstance(value, str) and not value.strip():
            raise ReviewInputError(f"{option} requires a non-empty value")
    if getattr(resolved_args, "profile", None):
        profile = get_provider_profile(str(resolved_args.profile))
        adapter = _adapter_for_provider(getattr(resolved_args, "provider", None))
        if adapter is not None and adapter != profile.provider:
            raise ReviewInputError(
                f"--provider {resolved_args.provider} does not match profile "
                f"'{profile.name}' (requires {profile.provider})"
            )
        resolved_args.provider = profile.provider
        if not _cli_option_set(argv_stub, "--model", explicit=explicit):
            resolved_args.model = profile.model
        provider_settings = _provider_settings_from_args(
            resolved_args,
            api_key=None,
            fixture_response=getattr(resolved_args, "fixture_response", None),
            argv=argv_stub,
        )
    else:
        resolved = resolve_inference(
            work_configuration,
            cli_provider=cli_provider if isinstance(cli_provider, str) else None,
            cli_model=cli_model if isinstance(cli_model, str) else None,
            environ=_review_environ(),
        )
        resolved_args.provider = _CANONICAL_BACKEND_ADAPTERS[resolved.backend]
        resolved_args.model = (
            cli_model if isinstance(cli_model, str) else resolved.model
        )
        resolved_args.base_url = (
            cli_base_url if isinstance(cli_base_url, str) else None
        ) or resolved.base_url
        resolved_args.allow_custom_endpoint = bool(
            getattr(resolved_args, "allow_custom_endpoint", False)
        ) or (resolved.authorizes_custom_endpoint(resolved_args.base_url))
        resolved_args.api_key_env = (
            cli_api_key_env if isinstance(cli_api_key_env, str) else None
        ) or resolved.credential_env
        cli_timeout = _explicit_cli_value(resolved_args, argv_stub, "timeout-seconds")
        resolved_args.timeout_seconds = (
            cli_timeout
            if isinstance(cli_timeout, (int, float))
            else resolved.timeout_seconds
        )
        provider_settings = _provider_settings_from_args(
            resolved_args,
            api_key=None,
            fixture_response=getattr(resolved_args, "fixture_response", None),
            argv=argv_stub,
            openrouter_policy=(
                OpenRouterRoutingPolicy(resolved.upstream_provider)
                if resolved.backend == "openrouter"
                and resolved.upstream_provider is not None
                else None
            ),
        )
    transaction_provider_identity, transaction_model = _transaction_provider_identity(
        provider_settings
    )
    work_budgets = work_configuration.advanced.review_work
    resource_budget = _configured_resource_budget(
        ResourceBudget.for_limits(core_cli.DEFAULT_REVIEW_LIMITS), work_configuration
    )
    work_policy_digest = review_work_policy_digest(
        work_budgets,
        resource_budget,
        None,
        decision=work_configuration.decision,
    )
    orchestrate = (
        bool(getattr(resolved_args, "orchestrate_large_changes", False))
        or work_budgets.mode == "unified"
    )
    effective_stages = stages if stages is not None else DEFAULT_STAGES
    stage_identity, category_policy = transaction_stage_identity(effective_stages)
    transaction_configuration_context = build_transaction_configuration_context(
        provider=transaction_provider_identity,
        model=transaction_model,
        stages=stage_identity,
        category_policy=category_policy,
        publication_mode=policy.mode,
        orchestration_enabled=orchestrate,
        work_policy_digest=work_policy_digest,
    )
    return (
        policy.digest(),
        ReviewTransaction.compute_configuration_digest(
            transaction_configuration_context
        ),
    )


class RolloverService(Protocol):
    """Internal one-bootstrap dependency; no CLI or document conveys this service.

    Implementations preserve native predecessor authentication, cessation and
    exact owned activation readback, returning the confirmed caller accounting.
    """

    def __call__(
        self,
        caller: ApiRunRequestTransport,
        ledger: GitHubIssueCommentSessionLedger,
        identity: SessionIdentity,
        configuration: FrozenApiConfiguration,
        owner: ApiExecutionOwner,
        previous: SessionRecord,
        require_cessation: Callable[[], None],
        /,
    ) -> ApiRunAccounting: ...


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


from .trust_enrollment import _install_native_source  # noqa: E402

_install_native_source(StartupRunLedger)
del _install_native_source


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

    def validate_before_checkpoint(
        self,
        *,
        ledger,
        identity,
        result,
        baseline,
        current_key,
        policy,
        diff,
        changed_paths,
    ):
        """Run purely in-memory pre-publication validation ahead of checkpointing."""
        if ledger is not self.ledger or identity != self.identity:
            raise ReviewInputError(
                "pre-checkpoint validation does not own the admitted process"
            )
        caller = self.ledger.caller
        before_observations = len(caller.observations)
        caller._pre_checkpoint_validation_failed = False
        try:
            from review_sensei.finding_drift import (
                DriftEvaluationResult,
                project_drift_review_result,
            )
            from review_sensei.finding_lifecycle import decode_lifecycle_state

            from .finding_lifecycle import _unpack_journal, _validate
            from .finding_lifecycle_dictionary import is_dictionary, unpack
            from .publication import ReviewPublisher, _reject_conflicting_instances
            from .quarantine_namespace import inherited_namespace_is_historical

            _reject_conflicting_instances(result.comments)
            publisher = ReviewPublisher(http=self.ledger.http)
            prepared = publisher.prepare(
                result=result,
                diff=diff,
                head_sha=self.head_sha,
                convergence_policy=policy,
                baseline=baseline,
                current_key=current_key,
                changed_paths=changed_paths,
            )
            publisher._validate_locations(prepared.result, diff)
            effect_record = caller._effect_record
            if effect_record is None or self.identity.repository_id is None:
                raise ReviewInputError(
                    "pre-checkpoint validation requires an admitted session record"
                )
            if effect_record.quarantine_lineage is not None:
                return
            repo_key = (self.identity.repository_id, self.identity.pull_request)
            if effect_record.finding_lifecycle is not None:
                manifest = effect_record.finding_lifecycle.get("manifest")
                envelope = _read_cached_manifest_in_memory(
                    self.ledger, self.identity, manifest
                )
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("schema_version") != "finding-lifecycle-evidence-v2"
                ):
                    return
                assert isinstance(manifest, dict)
                binding = manifest["binding"]
                packed = envelope.get("lifecycle")
                if isinstance(packed, dict) and is_dictionary(packed):
                    dict_manifest = packed.get("dictionary_manifest")
                    base = _read_cached_manifest_in_memory(
                        self.ledger, self.identity, dict_manifest
                    )
                    if base is None:
                        return
                    document = unpack(packed, base=base, current_binding=binding)
                else:
                    document = _validate(_unpack_journal(packed))
                restored = decode_lifecycle_state(document["lifecycle"])
                if inherited_namespace_is_historical(
                    self.ledger, effect_record, restored.state, document
                ):
                    return
                if not isinstance(restored.state, DriftEvaluationResult):
                    return
                publisher._drift_states[repo_key] = restored.state
            elif ReviewRun._record_requires_finding_inventory_migration(effect_record):
                return
            planned, _ = publisher._plan_drift_round(
                repository_id=self.identity.repository_id,
                pull_request=self.identity.pull_request,
                head_sha=self.head_sha,
                base_sha=self.base_sha,
                result=prepared.result,
                diff=diff,
                baseline=baseline,
                convergence_policy=policy,
                authorized_dispositions=(),
                max_active_findings=prepared.result.limits.max_comments,
                confirmed_fixes=(),
                human_overrides=(),
                coverage_acceptance=None,
                authorized_rescan=None,
                enable_drift_tracking=True,
                drift_descriptors=(),
                host_outdated_threads=frozenset(),
                host_resolved_threads=frozenset(),
            )
            if planned is not None:
                project_drift_review_result(prepared.result, planned)
        except BaseException:
            if (
                len(caller.observations) == before_observations
                and not caller._external_business_write_attempted
                and not caller._provider_effect_unresolved
            ):
                caller._pre_checkpoint_validation_failed = True
            raise

    def record_pre_external_write_settlement(
        self,
        *,
        ledger,
        identity,
        settled_record,
        base_sha,
        head_sha,
        transaction_id=None,
    ):
        """Emit authenticated settlement witness after pre-checkpoint failure cleanup."""
        if ledger is not self.ledger or identity != self.identity:
            raise ReviewInputError(
                "settlement witness emission does not own the admitted process"
            )
        if not ledger.caller._pre_checkpoint_validation_failed:
            raise ReviewInputError(
                "pre-checkpoint settlement witness requires pure validation failure"
            )
        from .publication_failure_recovery import emit_pre_external_write_settlement

        return emit_pre_external_write_settlement(
            ledger=ledger,
            caller=ledger.caller,
            identity=identity,
            settled_record=settled_record,
            boundary="pre_checkpoint_validation",
            base_sha=base_sha,
            head_sha=head_sha,
            transaction_id=transaction_id,
        )


class ReviewRunLedger(ApiRunSessionLedger):
    """One funded exact readback on an uncertain write, never a replay."""

    def __init__(self, native, caller, identity):
        super().__init__(native, caller, identity)
        # Reader-first source integration: the current admitted review always
        # retains its complete finding lifecycle using the existing owner,
        # evidence destination and original physical caller meter.
        self.enable_finding_lifecycle_writes = True

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
        self.enable_finding_lifecycle_writes = True

    @property
    def supports_paged_disposition_snapshot_writes(self) -> bool:
        return bool(self._quarantine_required)

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
    restart_id: str | None = None
    recovery_context_store: RecoveryContextStore | None = field(
        default=None, repr=False
    )

    def retained_analysis(self, argv: list[str]) -> CompletedAnalysis | None:
        """Recover a stopped same-run owner's exact checkpoint, without inference."""
        if self.caller.accounting.owner.run_attempt != 2:
            return None
        loaded = self.ledger.load(self.identity)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError("successor checkpoint is unavailable")
        self.caller._require_ledger(self.ledger, self.identity)
        if loaded.record.record_sha256 != self.caller._root:
            raise ReviewInputError("successor checkpoint changed")
        transaction = loaded.record.transaction
        from .api_run_history import inherited_preprovider_reference

        inherited = inherited_preprovider_reference(
            loaded.record, self.caller.accounting
        )
        if inherited is not None:
            raise ReviewInputError(
                "inherited enrollment has no proven provider non-entry or root-write settlement"
            )
        if (
            transaction is not None
            and transaction.phase == "publication_succeeded"
            and self.caller.accounting.terminal_transaction is None
        ):
            raise ReviewInputError(
                "successor publication tail is unverified; authenticated predecessor completion is required"
            )
        if transaction is None or transaction.phase not in {
            "publication_pending",
            "publication_failed",
        }:
            return None
        from review_sensei.cli import _env_flag, _explicit_cli_value
        from review_sensei.convergence import resolve_review_convergence_policy
        from review_sensei.diff import analyze_diff
        from review_sensei.validation import read_bounded_utf8

        from .publication_context_recovery import (
            bundle_from_reconstructed_contexts,
            measure_staged_recovery_footprint,
            reconstruct_publication_contexts,
            resolve_recovery_placement_mode,
            verify_stored_recovery_context,
        )
        from .publication_recovery import recover_retained_review_result

        args = engine_parser(include_hosted=True).parse_args(argv)
        if args.diff is None:
            raise ReviewInputError("successor requires the exact original diff")
        diff = read_bounded_utf8(args.diff, maximum=1_048_576, label="original diff")
        policy = resolve_review_convergence_policy(
            mode=args.review_mode, quality=self.plan.policy.review_quality
        )
        accounting = self.caller.accounting
        own_startup = sum(
            len(slot.requests)
            for slot in accounting.startup.slots
            if slot.job == accounting.owner.job
            and slot.attempt == accounting.owner.run_attempt
        )
        other_startup = accounting.startup.liability - own_startup
        already_consumed = (
            other_startup + accounting.prior_nonstartup_liability + accounting.confirmed
        )
        unknown_usage = accounting.uncertain
        active_reserved = sum(
            block.undispatched
            for block in accounting.blocks
            if block.issued_by != accounting.owner
        )
        (
            current_root_bytes,
            intermediate_root_bytes,
            current_comment_bytes,
            result_part_count,
            total_decoded_bytes,
        ) = measure_staged_recovery_footprint(
            loaded.record, ledger=self.ledger, identity=self.identity
        )
        ancestor_hashes: tuple[str, ...] = ()
        ancestor_runs: tuple[int, ...] = ()
        if accounting.previous_record is not None:
            ancestor_hashes = (str(accounting.previous_record["root_sha256"]),)
            ancestor_runs = (int(cast(int, accounting.previous_record["run_id"])),)
        runtime_only_flags = _capture_runtime_only_flags(args, argv)
        raw_cli_provider = _explicit_cli_value(args, argv, "provider")
        raw_cli_model = _explicit_cli_value(args, argv, "model")
        cli_provider = raw_cli_provider if isinstance(raw_cli_provider, str) else None
        cli_model = raw_cli_model if isinstance(raw_cli_model, str) else None
        symbol_context_enabled = bool(
            getattr(args, "enable_symbol_context", False)
        ) or _env_flag("REVIEWSENSEI_ENABLE_SYMBOL_CONTEXT")
        symbol_allowed_paths = tuple(
            getattr(args, "symbol_context_allowed_path", ()) or ()
        )
        placement_mode = resolve_recovery_placement_mode(
            publication_inventory="complete", review_mode=policy.mode
        )
        contexts = reconstruct_publication_contexts(
            source_root=Path.cwd(),
            diff=diff,
            ledger=self.ledger,
            identity=self.identity,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy=policy,
            cli_provider=cli_provider,
            cli_model=cli_model,
            environ=os.environ,
            evidence_policy=getattr(args, "evidence_policy", "legacy"),
            snapshot_sha256=getattr(args, "snapshot_sha256", None),
            enable_symbol_context=symbol_context_enabled,
            symbol_context_allowed_paths=symbol_allowed_paths or None,
            symbol_context_max_files=(
                args.symbol_context_max_files
                if _has_option(argv, "--symbol-context-max-files")
                else None
            ),
            symbol_context_max_bytes=(
                args.symbol_context_max_bytes
                if _has_option(argv, "--symbol-context-max-bytes")
                else None
            ),
            symbol_context_max_depth=(
                args.symbol_context_max_depth
                if _has_option(argv, "--symbol-context-max-depth")
                else None
            ),
            stages_dir=getattr(args, "stages_dir", None),
            categories_dir=getattr(args, "categories_dir", None),
            # review_sensei.cli only loads custom stages when --stages-dir is explicit.
            use_customization_directories=False,
            orchestrate_large_changes=bool(
                getattr(args, "orchestrate_large_changes", False)
            ),
            recovery_store=self.recovery_context_store,
            policy_commit=accounting.configuration.commit,
            base_ref=self.base_branch,
            placement_mode=placement_mode,
            producer="github-actions-review-run",
            current_run_id=self.caller.run.run_id,
            attempt=accounting.owner.run_attempt,
            runtime_only_flags=runtime_only_flags,
            current_root_sha256=loaded.record.record_sha256,
            ancestor_record_sha256s=ancestor_hashes,
            ancestor_run_ids=ancestor_runs,
            current_root_bytes=current_root_bytes,
            intermediate_root_bytes=intermediate_root_bytes,
            current_comment_bytes=current_comment_bytes,
            result_part_count=result_part_count,
            total_decoded_bytes=total_decoded_bytes,
            original_api_limit=accounting.configuration.limit,
            already_consumed_requests=already_consumed,
            unknown_usage_requests=unknown_usage,
            active_reserved_requests=active_reserved,
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
        if self.recovery_context_store is not None:
            (
                rec_root_bytes,
                rec_intermediate_root_bytes,
                rec_comment_bytes,
                rec_part_count,
                rec_decoded_bytes,
            ) = measure_staged_recovery_footprint(
                loaded.record,
                ledger=self.ledger,
                identity=self.identity,
                recovered_result=result,
            )
            if rec_decoded_bytes > total_decoded_bytes:
                expected_bundle = bundle_from_reconstructed_contexts(
                    contexts,
                    source_root=Path.cwd(),
                    diff=diff,
                    ledger=self.ledger,
                    identity=self.identity,
                    base_ref=self.base_branch,
                    policy_commit=accounting.configuration.commit,
                    placement_mode=placement_mode,
                    producer="github-actions-review-run",
                    run_id=self.caller.run.run_id,
                    attempt=accounting.owner.run_attempt,
                    runtime_only_flags=runtime_only_flags,
                    _preloaded_record=loaded.record,
                )
                verify_stored_recovery_context(
                    self.recovery_context_store,
                    expected_bundle=expected_bundle,
                    current_run_id=self.caller.run.run_id,
                    current_root_sha256=loaded.record.record_sha256,
                    ancestor_record_sha256s=ancestor_hashes,
                    ancestor_run_ids=ancestor_runs,
                    current_root_bytes=rec_root_bytes,
                    intermediate_root_bytes=rec_intermediate_root_bytes,
                    current_comment_bytes=rec_comment_bytes,
                    result_part_count=max(result_part_count, rec_part_count),
                    total_decoded_bytes=rec_decoded_bytes,
                    original_api_limit=accounting.configuration.limit,
                    already_consumed_requests=already_consumed,
                    unknown_usage_requests=unknown_usage,
                    active_reserved_requests=active_reserved,
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

    @staticmethod
    def _record_requires_finding_inventory_migration(record: SessionRecord) -> bool:
        return record.finding_lifecycle is None and (
            record.completed_initial_reviews > 0
            or record.completed_verification_rounds > 0
            or record.convergence_history is not None
            or record.transaction is not None
            or record.publication_staging is not None
        )

    def finding_inventory_migration_required(self) -> bool:
        # Reuse the exact authenticated enrollment root, not a new census or
        # diagnostic file. Missing retained inventory is not an empty inventory.
        record = self.caller._effect_record
        if record is None or record.record_sha256 != self.caller._root:
            raise ReviewInputError(
                "authenticated finding inventory admission is unavailable"
            )
        return self._record_requires_finding_inventory_migration(record)

    def execute(self, argv: list[str], *, publication_output: Path) -> int:
        lifecycle_diagnostics.phase("analysis")
        retained = self.retained_analysis(argv)
        if retained is None and self.finding_inventory_migration_required():
            return _write_migration_hold_outcome(
                publication_output, reason="finding_inventory_migration_required"
            )
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
        lifecycle_diagnostics.analysis(analysis)
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
            lifecycle_diagnostics.publication(
                {
                    "status": "already_published",
                    "publication_status": "already_published",
                }
            )
            terminal_reason = None
            lifecycle_diagnostics.phase("caller_close")
            try:
                try:
                    self.caller.begin_terminal("already-published")
                except BaseException:
                    close_not_entered("terminal_entry_failed")
                    raise
                close_review_run(
                    self.caller, self.ledger, self.identity, hooks.skipped_transaction
                )
            except ReviewSenseiError:
                terminal_reason = "terminal_close_unconfirmed"
            outcome: dict[str, object] = {
                "status": "published_terminal_pending"
                if terminal_reason
                else "already_published",
                "publication_status": "already_published",
                "accounting_terminal": terminal_reason is None,
                "terminal_reason": terminal_reason,
                "review_id": None,
                "diagnostic": None,
            }
            lifecycle_diagnostics.publication(outcome)
            lifecycle_diagnostics.phase("outcome_write")
            publication_output.write_text(json.dumps(outcome) + "\n")
            return 1 if terminal_reason else 0
        if (
            retained is None
            and self.recovery_context_store is not None
            and analysis.result.transaction is not None
        ):
            from .publication_context_recovery import (
                capture_recovery_context_bundle,
                resolve_recovery_placement_mode,
            )

            capture_args = engine_parser(include_hosted=True).parse_args(argv)
            capture_recovery_context_bundle(
                self.recovery_context_store,
                transaction=analysis.result.transaction,
                configuration_context=analysis.configuration_context,
                admission_context=admission_context_document(
                    analysis.baseline, analysis.current_key
                ),
                evidence_context=analysis.evidence_context,
                decision_policy_digest=self.plan.decision.digest(),
                diff=analysis.diff,
                policy_commit=self.caller.accounting.configuration.commit,
                base_ref=self.base_branch,
                repository_id=self.identity.repository_id,
                placement_mode=resolve_recovery_placement_mode(
                    publication_inventory="complete",
                    review_mode=analysis.policy.mode,
                ),
                accounting_version=self.caller.accounting.version,
                producer="github-actions-review-run",
                run_id=self.caller.run.run_id,
                attempt=self.caller.accounting.owner.run_attempt,
                runtime_only_flags=_capture_runtime_only_flags(capture_args, argv),
            )
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
        from .trust_enrollment import enroll_application_trust_snapshot

        observed_trust = enroll_application_trust_snapshot(application, self)
        lifecycle_diagnostics.phase("publication")
        try:
            publication: PublicationResult = hooks.publish_review(
                application,
                options=GitHubWriteOptions(
                    github_writes=True,
                    auto_review=True,
                    auto_approve=(
                        self.restart_id is None
                        and self.plan.policy.reviews == "auto-approve"
                    ),
                    complete_inventory=True,
                    max_active_findings=analysis.result.limits.max_comments,
                    enable_drift_tracking=True,
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
                observed_trust=observed_trust,
            )
        except ReviewSenseiError:
            # A durable native intent may stop a later effect before the
            # publisher returns. Preserve an outcome without promoting the
            # attempted publication or its journal into a confirmed receipt.
            observed = getattr(application, "_last_publication_result", None)
            confirmed_review = (
                observed.review_id
                if isinstance(observed, PublicationResult)
                and observed.status
                in {"published", "already_published", "auto_approval_disabled"}
                and type(observed.review_id) is int
                and observed.review_id > 0
                else None
            )
            outcome = {
                "status": "published_terminal_pending"
                if confirmed_review is not None
                else "publication_incomplete",
                "publication_status": "published"
                if confirmed_review is not None
                else "unknown",
                "accounting_terminal": False,
                "terminal_reason": "publication_interrupted",
                "review_id": confirmed_review,
                "diagnostic": "publication_outcome_incomplete",
            }
            lifecycle_diagnostics.publication(outcome)
            publication_output.write_text(json.dumps(outcome, indent=2) + "\n")
            raise
        lifecycle_diagnostics.publication(
            {
                "status": publication.status,
                "publication_status": publication.status,
                "diagnostic": publication.diagnostic,
            }
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
        from .action_completion import finalization_confirmed
        from .check_accounting import denied_accounting_details

        check_rejection = denied_accounting_details(
            publication, self.caller, self.identity, analysis.result
        )
        if (
            publication.status
            in {"published", "already_published", "auto_approval_disabled"}
            and (
                check_rejection is not None
                or (
                    publication.diagnostic in _CONFIRMED_WITHHELD_CLOSE_DIAGNOSTICS
                    and finalization_confirmed(publication)
                )
                or publication.diagnostic
                in {
                    None,
                    "approval_withheld",
                    "review_incomplete",
                }
            )
            and publication.thread_resolution != "unconfirmed"
        ):
            lifecycle_diagnostics.phase("caller_close")
            try:
                close_review_run(
                    self.caller, self.ledger, self.identity, analysis.result.transaction
                )
            except ReviewSenseiError:
                terminal_reason = "terminal_close_unconfirmed"
            else:
                accounting_terminal = True
                terminal_reason = None
        else:
            close_not_entered(terminal_reason)
        outcome = {
            "status": "published_terminal_pending"
            if published and not accounting_terminal
            else publication.status,
            "publication_status": publication.status,
            "accounting_terminal": accounting_terminal,
            "terminal_reason": terminal_reason,
            "review_id": publication.review_id,
            "diagnostic": publication.diagnostic,
        }
        if check_rejection is not None:
            outcome["check_failure"] = check_rejection
        lifecycle_diagnostics.publication(outcome)
        lifecycle_diagnostics.phase("outcome_write")
        publication_output.write_text(json.dumps(outcome, indent=2) + "\n")
        return (
            0
            if published
            and accounting_terminal
            and check_rejection is None
            and publication.diagnostic not in _NON_ZERO_CLOSED_DIAGNOSTICS
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
) -> tuple[FrozenApiConfiguration, str]:
    lifecycle_diagnostics.step("repository_policy")
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
    lifecycle_diagnostics.step("default_policy_ref")
    reference = _get(
        http,
        http.repository_path(repository, "/git/ref/heads/" + quote(branch, safe="/")),
        token,
    )
    obj = reference.get("object")
    if isinstance(obj, dict) and obj.get("type") == "commit":
        lifecycle_diagnostics.observed_policy(branch, obj.get("sha"))
    if (
        not isinstance(obj, dict)
        or obj.get("type") != "commit"
        or obj.get("sha") != policy_commit
    ):
        raise ReviewInputError("trusted policy commit changed before admission")
    lifecycle_diagnostics.step("policy_content")
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
    digest = hashlib.sha256(raw).hexdigest()
    lifecycle_diagnostics.observed_policy(branch, policy_commit, digest)
    return FrozenApiConfiguration(
        policy_commit, digest, configuration.api_request_limit
    ), branch


def _prepare_review_input(
    args: argparse.Namespace,
    *,
    directory: Path,
    configuration_path: Path | None,
    head_repository: str,
) -> None:
    """Reject locally invalid input before any durable caller enrollment.

    This is input validation, not an analysis/abort authority. The engine still
    validates its input independently. Generated and supplied inputs use the
    same configured engine mode and its existing input bounds.
    """
    from review_sensei.diff import analyze_diff
    from review_sensei.planning import DEFAULT_TOTAL_WORK_BUDGET, plan_change
    from review_sensei.validation import DEFAULT_REVIEW_LIMITS, read_bounded_utf8

    work = load_configuration(configuration_path).advanced.review_work
    orchestrate = bool(getattr(args, "orchestrate_large_changes", False))
    if args.diff is None:
        args.diff = directory / "review.patch"
        prepare_diff(
            repository=Path.cwd(),
            base_ref=args.base_sha,
            head_ref=args.head_sha,
            head_repository=head_repository
            if head_repository != args.repository
            else None,
            output=args.diff,
            aggregate_work=work.mode == "unified" or orchestrate,
        )
    if work.mode == "unified" or orchestrate:
        budget = DEFAULT_TOTAL_WORK_BUDGET
        diff = read_bounded_utf8(
            args.diff, maximum=budget.max_total_diff_bytes, label="diff"
        )
        if work.mode == "unified":
            analyze_diff(
                diff,
                limits=DEFAULT_REVIEW_LIMITS,
                allow_incomplete=True,
                max_bytes=budget.max_total_diff_bytes,
                max_lines=budget.max_total_diff_lines,
                max_files=budget.max_total_files,
                max_hunks=budget.max_total_hunks,
            )
        else:
            plan_change(
                diff,
                limits=DEFAULT_REVIEW_LIMITS,
                orchestrate=True,
                work_budget=budget,
            )
    else:
        diff = read_bounded_utf8(
            args.diff, maximum=DEFAULT_REVIEW_LIMITS.max_diff_bytes, label="diff"
        )
        analyze_diff(diff, limits=DEFAULT_REVIEW_LIMITS)


def bootstrap(
    args: argparse.Namespace,
    *,
    run_directory: Path,
    policy_commit: str,
    purpose: str = "review",
    source: Mapping[str, object] | None = None,
    quarantine_predecessor_comment_id: int | None = None,
    restart_id: str | None = None,
    recovery_context_dir: Path | None = None,
    recovery_context_key_file: Path | None = None,
    recovery_context_ttl_seconds: int = DEFAULT_RECOVERY_TTL_SECONDS,
    rollover_service: RolloverService | None = None,
    enable_current_trust_enrollment: bool = False,
    observe_original_actor_role: bool = False,
    input_directory: Path | None = None,
    command_action: str | None = None,
) -> ReviewRun:
    """Authenticate one new/recovery owner; no caller document activates capacity."""
    if purpose == "review" and args.diff is None and input_directory is None:
        raise ReviewInputError("review requires an owned generated input workspace")
    if type(observe_original_actor_role) is not bool or (
        observe_original_actor_role and not enable_current_trust_enrollment
    ):
        raise ReviewInputError(
            "original actor-role observation requires opted v4 enrollment"
        )
    if command_action is not None and (
        purpose != "command"
        or type(command_action) is not str
        or command_action not in MAINTAINER_ACTIONS
    ):
        raise ReviewInputError("command action semantic target is invalid")
    lifecycle_diagnostics.phase("bootstrap")
    lifecycle_diagnostics.step("policy_checkout")
    lifecycle_diagnostics.requested(args, policy_commit)
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
    from .controlled_restart import entry_registration

    restart_registration = entry_registration(
        repository=args.repository,
        repository_id=args.repository_id,
        pull_request=args.pull_request,
        request_id=restart_id,
        purpose=purpose,
        policy_commit=policy_commit,
        base_sha=args.base_sha,
        head_sha=args.head_sha,
    )
    if restart_registration is not None and (
        quarantine_predecessor_comment_id is not None
        or args.rescan
        or args.review_remaining
    ):
        raise ReviewInputError(
            "controlled restart cannot continue or migrate an old session"
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
    recovery_context_store = resolve_recovery_context_store(
        recovery_context_dir,
        recovery_context_key_file,
        artifacts_policy=plan.policy.artifacts,
        ttl_seconds=recovery_context_ttl_seconds,
    )
    job = "command" if purpose == "command" else plan.runner_kind
    if restart_registration is not None and job != restart_registration["job"]:
        raise ReviewInputError("controlled restart runner differs from its designation")
    from .quarantine_activation import registrations_for

    startup = review_startup_plan(
        job,
        quarantine_v3=any(
            row["schema_version"] == "quarantine-published-activation-v3"
            for row in registrations_for(
                SessionIdentity(
                    args.repository, args.pull_request, repository_id=args.repository_id
                )
            )
        ),
    )
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
    lifecycle_diagnostics.caller(caller)
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
    lifecycle_diagnostics.step("live_pr")
    pull = _get(
        http,
        http.repository_path(args.repository, f"/pulls/{args.pull_request}"),
        metadata_token,
    )
    base, head = pull.get("base"), pull.get("head")
    lifecycle_diagnostics.observed(pull)
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
    if purpose in {"command", "reply"}:
        setattr(
            caller,
            "_accepted_semantic_target",
            (purpose, args.base_sha, args.head_sha, command_action),
        )
    title = pull.get("title", "")
    if not isinstance(title, str) or len(title.encode("utf-8")) > 256:
        raise ReviewInputError("live pull request title exceeds its 256-byte bound")
    if getattr(args, "title", None) not in {None, "", title}:
        raise ReviewInputError("review title differs from the live pull request")
    if purpose == "review":
        lifecycle_diagnostics.step("review_input")
        _prepare_review_input(
            args,
            directory=run_directory if input_directory is None else input_directory,
            configuration_path=configuration_path,
            head_repository=head["repo"]["full_name"],
        )
    # Read-only authority/owner checks precede enrollment's durable witness.
    lifecycle_diagnostics.step("run_authority")
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
    lifecycle_diagnostics.step("broker_oidc")
    oidc = actions.request_oidc_token()
    lifecycle_diagnostics.step("broker_actions")
    actions_token = actions.exchange(oidc, capability="review_actions")
    evidence = ApiRunActionsEvidence(
        http, token=actions_token, command_run_id=run_id, job=job
    )
    lifecycle_diagnostics.step("actions_owner")
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
    session_broker = (
        BrokerClient(
            opener=caller.open,
            control=BrokerDispatchAccounting(budget, operation_id, 60, 60),
            authority={**authority, "version": 4},
        )
        if enable_current_trust_enrollment
        else broker
    )
    lifecycle_diagnostics.step("broker_session")
    session = session_broker.open_session(
        oidc,
        repository_id=args.repository_id,
        pull_request=args.pull_request,
        head_sha=args.head_sha,
        **({"observe_actor_role": True} if observe_original_actor_role else {}),
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
        policy_commit=policy_commit,
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
    if restart_registration is not None:
        from .controlled_restart import install_admission, prepare_admission

        proof = prepare_admission(
            ledger=native,
            identity=identity,
            registration=restart_registration,
            actions_token=actions_token,
            directory=run_directory,
        )
        install_admission(native, proof)
    lifecycle_diagnostics.step("ledger_discovery")
    from .trust_enrollment import compare_restored_observation, load_original_enrollment

    if enable_current_trust_enrollment:
        loaded, original_trust = load_original_enrollment(native, identity)
    else:
        loaded, original_trust = native.load(identity), None
    if loaded.status == "ok" and loaded.record is not None:
        if loaded.record.trust_enrollment is not None:
            if not enable_current_trust_enrollment:
                raise ReviewInputError(
                    "trust enrolled current observation unavailable; v4 opt-in required"
                )
            compare_restored_observation(session_broker, session, original_trust)
        elif enable_current_trust_enrollment:
            raise ReviewInputError(
                "trust original enrollment unavailable; legacy migration required"
            )
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
                lifecycle_diagnostics.step("initial_enrollment")
                caller.initialize_and_activate(
                    native,
                    identity,
                    frozen,
                    owner,
                    trust_source=(session_broker, session)
                    if enable_current_trust_enrollment
                    else None,
                )
            elif loaded.status == "ok" and loaded.record is not None:
                previous_root = loaded.record
                previous = ApiRunAccounting.from_document(
                    previous_root.api_run_accounting
                )
                lifecycle_diagnostics.predecessor(previous_root, previous)
                lifecycle_diagnostics.step("predecessor_admission")
                if previous.identity == caller.run:
                    raise ReviewInputError("caller run already started")
                historical_candidate = (
                    purpose == "review"
                    and previous.terminal_transaction is None
                    and previous.action is None
                    and previous.analysis_abort is None
                    and previous_root.transaction is not None
                    and previous_root.transaction.phase == "publication_succeeded"
                )
                pre_write_settlement_candidate = (
                    not historical_candidate
                    and previous.action is None
                    and previous.analysis_abort is None
                    and previous.terminal_transaction is None
                    and previous_root.reservation_id is None
                    and previous_root.reserved_slot is None
                    and (
                        (
                            previous_root.transaction is None
                            and previous_root.failed_attempts > 0
                            and isinstance(previous_root.failed_attempts_head_sha, str)
                        )
                        or (
                            previous_root.transaction is not None
                            and previous_root.transaction.phase == "publication_failed"
                        )
                    )
                )

                def observe_cessation():
                    return evidence.ceased_run_owner(
                        identity,
                        previous.identity.run_id,
                        previous.owner,
                        complete_terminal_inventory=(
                            previous.analysis_abort is not None
                            or pre_write_settlement_candidate
                        ),
                    )

                stopped = None if historical_candidate else observe_cessation()

                def require_cessation():
                    if observe_cessation() != stopped:
                        raise ReviewInputError("previous caller cessation changed")

                if historical_candidate:
                    from .historical_completion import (
                        authenticate_historical_publication,
                    )

                    def authenticate(record):
                        return authenticate_historical_publication(
                            caller=caller,
                            ledger=native,
                            identity=identity,
                            configuration=frozen,
                            owner=owner,
                            previous=record,
                            actions=evidence,
                            current_base=args.base_sha,
                            current_head=args.head_sha,
                            pull=pull,
                            authority=session.attestation,
                        )

                    caller.rollover_historical_and_activate(
                        native, identity, frozen, owner, previous_root, authenticate
                    )
                else:
                    if rollover_service is None:
                        if (
                            purpose == "review"
                            and restart_registration is None
                            and _is_ordinary_terminal_predecessor(
                                previous_root, previous
                            )
                        ):
                            if ReviewRun._record_requires_finding_inventory_migration(
                                previous_root
                            ):
                                raise _PreRolloverMigrationHold(
                                    "finding_inventory_migration_required"
                                )
                            from review_sensei.finding_lifecycle_store import (
                                normalize_reference,
                            )

                            lifecycle_ref = normalize_reference(
                                previous_root.finding_lifecycle
                            )
                            if lifecycle_ref is not None:
                                binding = lifecycle_ref["manifest"]["binding"]
                                expected_policy, expected_configuration = (
                                    _expected_transaction_digests(
                                        args, configuration_path=configuration_path
                                    )
                                )
                                if (
                                    binding["policy_digest"] != expected_policy
                                    or binding["configuration_digest"]
                                    != expected_configuration
                                ):
                                    raise _PreRolloverMigrationHold(
                                        "finding_inventory_policy_drift"
                                    )
                        caller.rollover_and_activate(
                            native,
                            identity,
                            frozen,
                            owner,
                            previous_root,
                            require_cessation,
                        )
                    else:
                        rollover_service(
                            caller,
                            native,
                            identity,
                            frozen,
                            owner,
                            previous_root,
                            require_cessation,
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
        lifecycle_diagnostics.predecessor(loaded.record, previous)
        lifecycle_diagnostics.step("predecessor_admission")
        previous.require_binding(caller.run, frozen)
        if (
            previous.terminal_transaction is not None
            or previous.action is not None
            and previous.action["phase"] == "terminal"
        ):
            raise ReviewInputError("caller run is already terminal")
        prior = loaded.record
        from .api_run_history import inherited_preprovider_reference

        inherited = (
            inherited_preprovider_reference(prior, previous)
            if purpose == "review"
            else None
        )
        if inherited is not None:
            # Remote absence of a reservation is indistinguishable from a
            # dispatched, unstored root PATCH. Never claim successor ownership
            # or provider permission from this inherited snapshot alone.
            raise ReviewInputError(
                "inherited enrollment has no proven provider non-entry or root-write settlement"
            )
        elif (
            purpose == "review"
            and previous.previous_record is not None
            and previous.previous_record.get("schema_version")
            == "caller-run-reference-v2"
            and prior.transaction is not None
            and prior.transaction.phase == "analysis"
        ):
            raise ReviewInputError("new transaction has no proven provider non-entry")
        if (
            purpose == "review"
            and inherited is None
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
    if restart_registration is not None:
        proof.bind_runtime(ledger)
    publication_broker = BrokerClient(
        opener=caller.open,
        control=BrokerDispatchAccounting(budget, operation_id, 60, 60),
        authority=authority,
    )
    head_repo = head.get("repo")
    head_repo_full_name = (
        head_repo.get("full_name") if isinstance(head_repo, dict) else None
    )
    resolved_head_repository = (
        head_repo_full_name
        if isinstance(head_repo_full_name, str) and head_repo_full_name.strip()
        else args.repository
    )
    result = ReviewRun(
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
        resolved_head_repository,
        configuration_path,
        session.attestation,
        session.grant,
        title,
        actions_token,
        restart_id,
        recovery_context_store,
    )
    lifecycle_diagnostics.admitted(result)
    return result


def _has_option(argv: Sequence[str], name: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in argv)


def _capture_runtime_only_flags(
    args: argparse.Namespace, argv: Sequence[str]
) -> dict[str, bool]:
    """Classify runtime-only custom stage and CLI/env override flags consistently."""

    from review_sensei.cli import _env_flag

    has_custom_stages = bool(
        getattr(args, "stages_dir", None) is not None
        or getattr(args, "categories_dir", None) is not None
        or _has_option(argv, "--stages-dir")
        or _has_option(argv, "--categories-dir")
        or os.environ.get("REVIEWSENSEI_STAGES_DIR", "").strip()
        or os.environ.get("REVIEWSENSEI_CATEGORIES_DIR", "").strip()
    )
    has_cli_overrides = bool(
        any(
            _has_option(argv, opt)
            for opt in (
                "--provider",
                "--model",
                "--profile",
                "--base-url",
                "--api-key-env",
                "--timeout-seconds",
                "--allow-custom-endpoint",
                "--allow-unqualified-profile",
                "--enable-symbol-context",
                "--symbol-context-allowed-path",
                "--symbol-context-max-files",
                "--symbol-context-max-bytes",
                "--symbol-context-max-depth",
                "--orchestrate-large-changes",
                "--evidence-policy",
                "--snapshot-sha256",
            )
        )
        or getattr(args, "enable_symbol_context", False)
        or getattr(args, "orchestrate_large_changes", False)
        or bool(getattr(args, "symbol_context_allowed_path", ()) or ())
        or os.environ.get("REVIEWSENSEI_PROVIDER", "").strip()
        or os.environ.get("REVIEWSENSEI_MODEL", "").strip()
        or os.environ.get("REVIEWSENSEI_PROFILE", "").strip()
        or os.environ.get("OLLAMA_MODEL", "").strip()
        or os.environ.get("OLLAMA_BASE_URL", "").strip()
        or _env_flag("REVIEWSENSEI_ENABLE_SYMBOL_CONTEXT")
    )
    return {
        "custom_stages": has_custom_stages,
        "cli_overrides": has_cli_overrides,
    }


def run_review(argv: Sequence[str], *, controlled_restart: bool = False) -> int:
    parser = argparse.ArgumentParser(prog="review-sensei-github review-run")
    parser.add_argument("--policy-commit", required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--publication-outcome", type=Path, required=True)
    parser.add_argument("--quarantine-predecessor-comment-id", type=int)
    parser.add_argument(
        "--enable-current-trust-enrollment",
        action="store_true",
        help="Opt in to original observed trust enrollment with a compatible v4 broker; not continuous trust history or effect authorization.",
    )
    parser.add_argument(
        "--observe-original-actor-role",
        action="store_true",
        default=False,
        help="Opt in to observing the original actor role via v4 broker (requires --enable-current-trust-enrollment); default-off.",
    )
    parser.add_argument("--recovery-context-dir", type=Path, default=None)
    parser.add_argument("--recovery-context-key-file", type=Path, default=None)
    parser.add_argument(
        "--recovery-context-ttl-seconds",
        type=int,
        default=DEFAULT_RECOVERY_TTL_SECONDS,
    )
    if controlled_restart:
        parser.add_argument("--restart-id", required=True)
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
    if controlled_restart and args.diff is not None:
        raise ReviewInputError(
            "controlled restart requires a freshly prepared current full diff"
        )
    # Keep generated inputs separate from existing runner files, including when
    # later predecessor admission refuses. Retain every original startup claim
    # and restart archive. Supplied inputs remain caller-owned and untouched.
    # Abrupt process death leaves only ephemeral generated source storage.
    workspace = (
        TemporaryDirectory(
            prefix="review-sensei-restart-"
            if controlled_restart
            else "review-sensei-input-",
            dir=os.environ.get("RUNNER_TEMP") or None,
        )
        if args.diff is None
        else nullcontext(str(options.run_directory))
    )
    with workspace as working_directory:
        if controlled_restart and Path(working_directory).resolve().is_relative_to(
            options.run_directory.resolve()
        ):
            raise ReviewInputError(
                "controlled restart diff cannot use retained history storage"
            )
        try:

            class _ActorRoleKwargs(TypedDict, total=False):
                observe_original_actor_role: bool

            actor_role_kwargs: _ActorRoleKwargs = (
                {"observe_original_actor_role": True}
                if options.observe_original_actor_role
                else {}
            )
            selected = bootstrap(
                args,
                run_directory=options.run_directory,
                policy_commit=options.policy_commit,
                quarantine_predecessor_comment_id=options.quarantine_predecessor_comment_id,
                restart_id=getattr(options, "restart_id", None),
                recovery_context_dir=options.recovery_context_dir,
                recovery_context_key_file=options.recovery_context_key_file,
                recovery_context_ttl_seconds=options.recovery_context_ttl_seconds,
                enable_current_trust_enrollment=options.enable_current_trust_enrollment,
                **actor_role_kwargs,
                input_directory=Path(working_directory),
            )
        except _PreRolloverMigrationHold as error:
            return _write_migration_hold_outcome(
                options.publication_outcome, reason=error.reason
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
        if not _has_option(core, "--diff"):
            core.extend(["--diff", str(args.diff)])
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


# Pin genuine subclass writers once at source module initialization.

_install_guarded_ledger_replaces(ReviewRunLedger, ActionRunLedger)

from .guarded_hot_namespace import install_run as _install_hot_namespace  # noqa: E402

_install_hot_namespace(ReviewRun)
del _install_hot_namespace


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
