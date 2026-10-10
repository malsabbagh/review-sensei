"""One bounded provider execution mechanism for both review work modes.

Only validated batches enter the aggregate. Diagnostic records contain no
prompts, source patches, provider output, or exception messages.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from .budgets import EffectiveWorkBudget
from .errors import ProviderError, ReviewFormatError, ReviewInputError
from .evidence import evidence_digest
from .models import ProviderRequest, ProviderResponse
from .outcomes import ResourceBudgetTracker
from .planning import ReviewWorkPlan, WorkBatch
from .providers.base import ReviewProvider
from .validation import utf8_size, validate_bounded_text

T = TypeVar("T")

if TYPE_CHECKING:
    from .work_recovery import WorkRecoveryStore


@dataclass(frozen=True)
class CheckpointMutation:
    """Narrow grant request metadata; root generation never changes admission.

    The trusted host must fence source/head/session authority and issue a fresh
    one-attempt grant for every activation. This descriptor is not a grant.
    Digests contain no raw prompt or provider response.
    """

    reason: str
    batch_id: str | None = None
    request_digest: str | None = None
    dispatch_digest: str | None = None
    root_generation: int | None = None

    def __post_init__(self) -> None:
        if type(self.reason) is not str or self.reason not in {
            "admission",
            "admission-dispatch",
            "dispatch",
            "accounting",
            "accepted",
            "pending",
            "replay",
            "finalize",
        }:
            raise ReviewInputError("checkpoint mutation reason is invalid")
        if self.reason == "admission-dispatch" and (
            self.request_digest is None or self.dispatch_digest is None
        ):
            raise ReviewInputError(
                "combined admission requires exact request and dispatch digests"
            )
        for value in (self.batch_id, self.request_digest, self.dispatch_digest):
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ReviewInputError("checkpoint mutation digest is invalid")
        if self.root_generation is not None and (
            isinstance(self.root_generation, bool)
            or not isinstance(self.root_generation, int)
            or not 0 <= self.root_generation <= 2**31 - 1
        ):
            raise ReviewInputError("checkpoint mutation generation is invalid")


def provider_work_identity(provider: ReviewProvider) -> str:
    """Bind adapter/routing settings, deliberately excluding credentials."""
    policy = getattr(provider, "routing_policy", None)
    fields = (
        policy.identity_fields()
        if policy is not None and callable(getattr(policy, "identity_fields", None))
        else None
    )
    identity = {
        "adapter": type(provider).__module__ + "." + type(provider).__qualname__,
        "name": provider.name,
        "model": provider.model,
        "base_url": getattr(provider, "base_url", None),
        "timeout_seconds": getattr(provider, "timeout_seconds", None),
        "max_output_tokens": getattr(provider, "max_output_tokens", None),
        "allow_model_override": getattr(provider, "allow_model_override", True),
        "routing_policy": fields,
    }
    contract = getattr(provider, "completion_contract", None)
    if contract is not None:
        identity["completion_contract"] = contract
        identity["endpoint"] = getattr(provider, "endpoint", None)
    return evidence_digest(identity)


@dataclass(frozen=True)
class CallResult(Generic[T]):
    value: T | None = None
    response: ProviderResponse | None = None
    diagnostic: str | None = None


@dataclass(frozen=True)
class CompletedBatch(Generic[T]):
    batch: WorkBatch
    value: T
    request_digest: str


@dataclass(frozen=True)
class WorkExecution(Generic[T]):
    plan: ReviewWorkPlan
    completed: tuple[CompletedBatch[T], ...]
    pending: tuple[tuple[str, str], ...]
    tracker_identity: str
    attempted_ids: tuple[str, ...] = ()
    unknown_response_bytes: int = 0
    inflight_response_bytes: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.plan, ReviewWorkPlan)
            or not isinstance(self.completed, tuple)
            or not isinstance(self.pending, tuple)
        ):
            raise ReviewInputError("work execution aggregate is invalid")
        assigned: list[str] = []
        for item in self.completed:
            if (
                not isinstance(item, CompletedBatch)
                or item.batch not in self.plan.batches
            ):
                raise ReviewInputError("work execution completed batch is invalid")
            assigned.extend(item.batch.requirement_ids)
        assigned.extend(identity for identity, _ in self.pending)
        if len(set(assigned)) != len(assigned) or set(assigned) != {
            item.identity for item in self.plan.requirements
        }:
            raise ReviewInputError(
                "work execution must retain every requirement exactly once"
            )
        if (
            not isinstance(self.attempted_ids, tuple)
            or len(set(self.attempted_ids)) != len(self.attempted_ids)
            or not set(self.attempted_ids) <= set(assigned)
        ):
            raise ReviewInputError("work execution attempted identities are invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.unknown_response_bytes,
                self.inflight_response_bytes,
            )
        ):
            raise ReviewInputError("work execution output reservation is invalid")

    @property
    def pending_ids(self) -> tuple[str, ...]:
        return tuple(identity for identity, _ in self.pending)

    @property
    def response_bytes_reserved(self) -> int:
        return self.unknown_response_bytes + self.inflight_response_bytes


class WorkCheckpointStore(Protocol):
    """Host-authenticated durable receipts; every write must fence stale writers.

    load restores the original charged tracker and absolute deadline. Missing
    latest authority, authentication failure and incompatible receipts raise;
    only proven absence returns None. save must finish before dispatch/exit.
    A trusted host may explicitly enable coalesce_admission_dispatch=True only
    when its activation durably retains complete original admission and the
    charged unknown outcome together. This optional capability defaults off;
    it changes neither witnesses, grants, deadlines nor original allowances.
    """

    def load(
        self,
        plan: ReviewWorkPlan,
        tracker: ResourceBudgetTracker,
        budgets: EffectiveWorkBudget,
        decode: Callable[[object, WorkBatch], T],
        request_digests: Mapping[str, str | None],
    ) -> WorkExecution[T] | None: ...

    def save(
        self,
        execution: WorkExecution[T],
        tracker: ResourceBudgetTracker,
        encode: Callable[[T], object],
        request_digests: Mapping[str, str | None],
        *,
        mutation: CheckpointMutation | None = None,
    ) -> None: ...


@dataclass
class OutputAccounting:
    """Known tracker bytes and conservative unknown output remain distinct."""

    unknown_response_bytes: int = 0
    inflight_response_bytes: int = 0

    def uncertain(self) -> None:
        self.unknown_response_bytes += self.inflight_response_bytes
        self.inflight_response_bytes = 0


def execute_call(
    request: ProviderRequest,
    *,
    provider: ReviewProvider,
    validate: Callable[[ProviderResponse], T],
    tracker: ResourceBudgetTracker,
    budgets: EffectiveWorkBudget,
    correction: str = "",
    repairable_validation: Callable[[Exception], bool] | None = None,
    before_dispatch: Callable[[str], None] | None = None,
    after_accounting: Callable[[], None] | None = None,
    output_accounting: OutputAccounting | None = None,
    defer_response_accounting: bool = False,
) -> CallResult[T]:
    """Count every dispatch, bound correction and transport retries, fail closed."""
    tracker.budget.validate_against_limits(request.limits)
    if (
        budgets.resource_budget is not None
        and tracker.budget != budgets.resource_budget
    ):
        raise ReviewInputError(
            "work execution resource envelope changed after planning"
        )
    original = request
    output_accounting = output_accounting or OutputAccounting()
    corrected = False
    while True:
        if budgets.capabilities is not None:
            selected_model = (
                request.model
                if getattr(provider, "allow_model_override", True)
                else None
            ) or provider.model
            budgets.capabilities.require_provider(provider, model=selected_model)
        diagnostic = tracker.admit_call(request.prompt)
        if diagnostic:
            return CallResult(diagnostic=diagnostic)
        prompt_bytes = utf8_size(request.prompt, label="work prompt")
        if (
            not budgets.fits_prompt(
                request.prompt, output_tokens=request.max_output_tokens
            )
            or tracker.prompt_bytes + prompt_bytes > budgets.max_total_prompt_bytes
        ):
            return CallResult(diagnostic="prompt_budget")
        output_limit = min(
            request.max_response_bytes,
            budgets.batch_output_bytes,
            budgets.max_total_output_bytes
            - tracker.response_bytes
            - output_accounting.unknown_response_bytes,
        )
        # Reserve the bounded response before dispatch. Do not send a request
        # whose valid response could exceed the remaining aggregate allowance.
        if output_limit <= 0:
            return CallResult(diagnostic="output_budget")
        timeout = tracker.remaining_seconds()
        if request.timeout_seconds is not None:
            timeout = min(timeout, request.timeout_seconds)
        bounded = replace(
            request, max_response_bytes=output_limit, timeout_seconds=timeout
        )
        tracker.record_prompt_attempt(request.prompt)
        tracker.record_provider_call()
        output_accounting.inflight_response_bytes = output_limit
        if before_dispatch is not None:
            before_dispatch(
                evidence_digest(
                    {
                        "domain": "reviewsensei:work-dispatch:v1",
                        "provider_identity": provider_work_identity(provider),
                        "budget": budgets.digest,
                        "prompt": bounded.prompt,
                        "model": bounded.model,
                        "json_mode": bounded.json_mode,
                        "output_tokens": bounded.max_output_tokens,
                        "max_prompt_bytes": bounded.max_prompt_bytes,
                        "max_response_bytes": bounded.max_response_bytes,
                        "timeout_seconds": bounded.timeout_seconds,
                        "provider_call": tracker.provider_calls,
                    }
                )
            )
        try:
            response = provider.complete(bounded)
        except ProviderError as exc:
            output_accounting.uncertain()
            if after_accounting is not None:
                after_accounting()
            if not exc.transient:
                return CallResult(diagnostic="provider_failed")
            reason = tracker.admit_transport_retry(exc.retry_after_seconds)
            if reason:
                return CallResult(diagnostic=reason)
            tracker.sleep_transport_retry(exc.retry_after_seconds)
            if after_accounting is not None:
                after_accounting()
            continue
        except (ReviewFormatError, ReviewInputError):
            output_accounting.uncertain()
            return CallResult(diagnostic="invalid_provider_output")
        except Exception:
            output_accounting.uncertain()
            return CallResult(diagnostic="provider_failed")
        if not isinstance(response, ProviderResponse):
            output_accounting.uncertain()
            return CallResult(diagnostic="invalid_provider_output")
        tracker.record_response(response.text)
        output_accounting.inflight_response_bytes = 0
        if after_accounting is not None and not defer_response_accounting:
            after_accounting()
        if (
            tracker.response_bytes + output_accounting.unknown_response_bytes
            > budgets.max_total_output_bytes
        ):
            return CallResult(diagnostic="output_budget")
        if tracker.elapsed_ms() >= tracker.budget.timeout_ms:
            return CallResult(diagnostic="deadline_exceeded")
        try:
            validate_bounded_text(
                response.text,
                output_limit,
                label="provider response",
                allow_empty=False,
            )
            value = validate(response)
        except (ReviewFormatError, ReviewInputError, ValueError, TypeError) as exc:
            if (
                not correction
                or (
                    repairable_validation is not None and not repairable_validation(exc)
                )
                or corrected
                or tracker.structural_retries >= tracker.budget.max_retry_attempts
            ):
                return CallResult(diagnostic="invalid_provider_output")
            tracker.structural_retries += 1
            if after_accounting is not None:
                after_accounting()
            corrected = True
            request = replace(original, prompt=original.prompt + "\n\n" + correction)
            continue
        if tracker.elapsed_ms() >= tracker.budget.timeout_ms:
            return CallResult(diagnostic="deadline_exceeded")
        return CallResult(value=value, response=response)


def execute_plan(
    plan: ReviewWorkPlan,
    *,
    provider: ReviewProvider,
    render: Callable[[WorkBatch], ProviderRequest],
    validate: Callable[[ProviderResponse, WorkBatch], T],
    tracker: ResourceBudgetTracker,
    budgets: EffectiveWorkBudget,
    correction: str = "",
    accept: Callable[[CompletedBatch[T]], bool] | None = None,
    prior: WorkExecution[T] | None = None,
    revalidate_cached: Callable[[T, WorkBatch], bool] | None = None,
    repairable_validation: Callable[[Exception], bool] | None = None,
    recovery: WorkRecoveryStore | None = None,
    encode_recovery: Callable[[T], object] | None = None,
    decode_recovery: Callable[[object, WorkBatch], T] | None = None,
    checkpoint: WorkCheckpointStore | None = None,
) -> WorkExecution[T]:
    tracker.budget.validate_against_limits()
    if plan.budget_digest != budgets.digest or (
        budgets.resource_budget is not None
        and tracker.budget != budgets.resource_budget
    ):
        raise ReviewInputError("work execution budgets do not match the admitted plan")
    cached: dict[str, CompletedBatch[T]] = {}
    terminal_pending: dict[str, str] = {}
    requests: dict[str, ProviderRequest] = {}
    request_digests: dict[str, str | None] = {}
    checkpoint_loaded = False
    admission_checked = False
    recovery_admission_checked = False
    admitted_digests: Mapping[str, str | None] | None = None
    if checkpoint is not None and recovery is not None and recovery.enabled:
        raise ReviewInputError(
            "durable execution cannot use diagnostic recovery authority"
        )
    load_admission = getattr(checkpoint, "load_admission", None)
    if callable(load_admission):
        if (
            encode_recovery is None
            or decode_recovery is None
            or revalidate_cached is None
        ):
            raise ReviewInputError(
                "durable execution requires codecs and semantic validation"
            )
        admission = load_admission(plan, tracker, budgets, decode_recovery)
        admission_checked = True
        if admission is not None:
            prior, admitted_digests = admission
            checkpoint_loaded = True
            terminal_pending = {
                identity: reason
                for identity, reason in prior.pending
                if reason != "queued"
            }
    elif checkpoint is None and recovery is not None and recovery.enabled:
        load_recovery_admission = getattr(recovery, "load_admission", None)
        if callable(load_recovery_admission):
            if (
                encode_recovery is None
                or decode_recovery is None
                or revalidate_cached is None
            ):
                raise ReviewInputError(
                    "work recovery requires normalized codecs and fresh semantic validation"
                )
            admission = load_recovery_admission(plan, tracker, budgets, decode_recovery)
            recovery_admission_checked = True
            if admission is not None:
                prior, admitted_digests = admission
                terminal_pending = {
                    identity: reason
                    for identity, reason in prior.pending
                    if reason != "interrupted"
                }
    for batch in plan.batches:
        try:
            request = render(batch)
        except ReviewInputError:
            if tracker.elapsed_ms() >= tracker.budget.timeout_ms:
                raise ReviewInputError(
                    "work deadline exceeded before request binding; no dispatch authorized"
                ) from None
            request_digests[batch.batch_id] = None
            continue
        requests[batch.batch_id] = request
        request_digests[batch.batch_id] = evidence_digest(
            {
                "domain": "reviewsensei:work-request:v1",
                "prompt": request.prompt,
                "provider": provider.name,
                "provider_model": provider.model,
                "provider_identity": provider_work_identity(provider),
                "model": request.model,
                "json_mode": request.json_mode,
                "output_tokens": request.max_output_tokens,
                "max_prompt_bytes": request.max_prompt_bytes,
                "max_response_bytes": request.max_response_bytes,
                "budget": budgets.digest,
            }
        )
    if admitted_digests is not None and dict(admitted_digests) != request_digests:
        raise ReviewInputError(
            "work recovery request binding changed after admission"
            if recovery_admission_checked
            else "durable receipt request binding changed after admission"
        )
    if recovery is not None and recovery.enabled:
        if (
            encode_recovery is None
            or decode_recovery is None
            or revalidate_cached is None
        ):
            raise ReviewInputError(
                "work recovery requires normalized codecs and fresh semantic validation"
            )
        recovered = (
            None
            if recovery_admission_checked
            else recovery.load(plan, tracker, budgets, decode_recovery, request_digests)
        )
        if recovered is not None:
            prior = recovered
            # Only dispatches interrupted before a durable outcome may resume.
            # Restarting the same authenticated plan cannot create another
            # semantic or transport retry for an already rejected batch.
            terminal_pending = {
                identity: reason
                for identity, reason in recovered.pending
                if reason != "interrupted"
            }
    if checkpoint is not None:
        if (
            encode_recovery is None
            or decode_recovery is None
            or revalidate_cached is None
        ):
            raise ReviewInputError(
                "durable execution requires codecs and semantic validation"
            )
        recovered = (
            None
            if admission_checked
            else checkpoint.load(
                plan, tracker, budgets, decode_recovery, request_digests
            )
        )
        if recovered is not None:
            checkpoint_loaded = True
            prior = recovered
            # An unknown remote outcome must not be dispatched again. Queued
            # work can still run with the original remaining allowance.
            terminal_pending = {
                identity: reason
                for identity, reason in recovered.pending
                if reason != "queued"
            }
    if prior is not None:
        if (
            revalidate_cached is None
            or prior.tracker_identity != tracker.execution_identity
            or prior.plan.mode != plan.mode
            or prior.plan.bundle.snapshot != plan.bundle.snapshot
            or prior.plan.authority_digest != plan.authority_digest
            or prior.plan.budget_digest != plan.budget_digest
        ):
            raise ReviewInputError(
                "work continuation requires the original authority, snapshot, and resource budget"
            )
        cached = {item.batch.batch_id: item for item in prior.completed}
    completed: list[CompletedBatch[T]] = []
    pending = list(plan.unprocessed)
    attempted_ids = list(prior.attempted_ids) if prior is not None else []
    accounting = OutputAccounting(
        unknown_response_bytes=(
            prior.unknown_response_bytes + prior.inflight_response_bytes
        )
        if prior is not None
        else 0,
    )
    last_saved: tuple[WorkExecution[T], tuple[int, ...]] | None = None
    admission_pending = checkpoint is not None and not checkpoint_loaded
    coalesce_admission = getattr(checkpoint, "coalesce_admission_dispatch", False)
    if type(coalesce_admission) is not bool:
        raise ReviewInputError("checkpoint admission coalescing capability is invalid")

    def save_state(
        next_index: int,
        *,
        dispatched: bool = False,
        reason: str = "pending",
        batch: WorkBatch | None = None,
        dispatch_digest: str | None = None,
    ) -> None:
        nonlocal last_saved, admission_pending
        if encode_recovery is not None and (
            recovery is not None or checkpoint is not None
        ):
            retained_completed = list(completed)
            retained_ids: set[str] = set()
            if checkpoint is not None:
                for batch in plan.batches[next_index:]:
                    previous = cached.get(batch.batch_id)
                    if previous is not None:
                        if (
                            previous.batch != batch
                            or previous.request_digest
                            != request_digests[batch.batch_id]
                            or revalidate_cached is None
                            or not revalidate_cached(previous.value, batch)
                        ):
                            raise ReviewInputError(
                                "durable accepted receipt failed revalidation"
                            )
                        retained_completed.append(previous)
                        retained_ids.update(batch.requirement_ids)
            retained_pending = pending + [
                (
                    identity,
                    "interrupted"
                    if checkpoint is None
                    else (
                        "dispatch-outcome-unknown"
                        if dispatched and offset == next_index
                        else terminal_pending.get(identity, "queued")
                    ),
                )
                for offset, batch in enumerate(plan.batches[next_index:], next_index)
                for identity in batch.requirement_ids
                if identity not in retained_ids
            ]
            store = checkpoint if checkpoint is not None else recovery
            assert store is not None
            execution = WorkExecution(
                plan,
                tuple(retained_completed),
                tuple(sorted(retained_pending)),
                tracker.execution_identity,
                tuple(attempted_ids),
                accounting.unknown_response_bytes,
                accounting.inflight_response_bytes,
            )
            counters = tuple(
                getattr(tracker, name)
                for name in (
                    "provider_calls",
                    "transport_retries",
                    "structural_retries",
                    "prompt_bytes",
                    "response_bytes",
                )
            )
            signature = (execution, counters)
            # The last per-batch receipt already accounts for all obligations.
            # Repeating its activation cannot add durability or replenish time.
            if (
                checkpoint is not None
                and reason == "finalize"
                and last_saved == signature
            ):
                return
            if checkpoint is not None:
                if admission_pending:
                    # First charge contains the exact complete original
                    # admission as well as the conservative dispatch liability.
                    # Non-dispatch terminal states still admit with zero calls.
                    reason = "admission-dispatch" if dispatched else "admission"
                checkpoint.save(
                    execution,
                    tracker,
                    encode_recovery,
                    request_digests,
                    mutation=CheckpointMutation(
                        reason,
                        batch.batch_id if batch is not None else None,
                        request_digests.get(batch.batch_id)
                        if batch is not None
                        else None,
                        dispatch_digest,
                    ),
                )
                admission_pending = False
            else:
                store.save(execution, tracker, encode_recovery, request_digests)
            last_saved = signature

    # Default zero-call admission persists before side effects. The explicit
    # coalescing capability instead persists complete admission with the first
    # charge/reservation before inference. A terminal no-dispatch path still
    # persists admission, and no provider side effect precedes either save.
    if admission_pending and not coalesce_admission:
        save_state(0, reason="admission")

    for index, batch in enumerate(plan.batches):
        terminal_reasons = [
            terminal_pending[identity]
            for identity in batch.requirement_ids
            if identity in terminal_pending
        ]
        if terminal_reasons:
            pending.extend(
                (identity, terminal_pending.get(identity, terminal_reasons[0]))
                for identity in batch.requirement_ids
            )
            save_state(index + 1, batch=batch)
            continue
        planned_request = requests.get(batch.batch_id)
        if planned_request is None:
            pending.extend(
                (identity, "prompt_budget") for identity in batch.requirement_ids
            )
            save_state(index + 1, batch=batch)
            continue
        request = planned_request
        request_digest = request_digests[batch.batch_id]
        assert request_digest is not None
        previous = cached.get(batch.batch_id)
        if (
            previous is not None
            and previous.batch == batch
            and previous.request_digest == request_digest
            and revalidate_cached is not None
            and revalidate_cached(previous.value, batch)
        ):
            if accept is None or accept(previous):
                completed.append(previous)
                save_state(index + 1, reason="replay", batch=batch)
                continue
            pending.extend(
                (identity, "output_budget")
                for remaining in plan.batches[index:]
                for identity in remaining.requirement_ids
            )
            break

        current_dispatch: str | None = None

        def before_dispatch(dispatch_digest: str) -> None:
            nonlocal current_dispatch
            current_dispatch = dispatch_digest
            for identity in batch.requirement_ids:
                if identity not in attempted_ids:
                    attempted_ids.append(identity)
            save_state(
                index,
                dispatched=True,
                reason="dispatch",
                batch=batch,
                dispatch_digest=dispatch_digest,
            )

        result = execute_call(
            request,
            provider=provider,
            validate=lambda response: validate(response, batch),
            tracker=tracker,
            budgets=budgets,
            correction=correction,
            repairable_validation=repairable_validation,
            before_dispatch=before_dispatch,
            after_accounting=(
                (
                    lambda: save_state(
                        index,
                        dispatched=True,
                        reason="accounting",
                        batch=batch,
                        dispatch_digest=current_dispatch,
                    )
                )
                if checkpoint is not None
                else None
            ),
            output_accounting=accounting,
            defer_response_accounting=checkpoint is not None,
        )
        if result.value is None or result.diagnostic is not None:
            pending.extend(
                (identity, result.diagnostic or "invalid_provider_output")
                for identity in batch.requirement_ids
            )
        else:
            item = CompletedBatch(batch, result.value, request_digest)
            if accept is not None and not accept(item):
                pending.extend(
                    (identity, "output_budget")
                    for remaining in plan.batches[index:]
                    for identity in remaining.requirement_ids
                )
                break
            completed.append(item)
        save_state(
            index + 1,
            reason="accepted"
            if result.value is not None and result.diagnostic is None
            else "pending",
            batch=batch,
            dispatch_digest=current_dispatch,
        )
    save_state(len(plan.batches), reason="finalize")
    return WorkExecution(
        plan,
        tuple(completed),
        tuple(sorted(pending)),
        tracker.execution_identity,
        tuple(attempted_ids),
        accounting.unknown_response_bytes,
        accounting.inflight_response_bytes,
    )
