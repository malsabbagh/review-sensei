"""One bounded provider execution mechanism for both review work modes.

Only validated batches enter the aggregate. Diagnostic records contain no
prompts, source patches, provider output, or exception messages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Generic, TypeVar

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


def provider_work_identity(provider: ReviewProvider) -> str:
    """Bind adapter/routing settings, deliberately excluding credentials."""
    policy = getattr(provider, "routing_policy", None)
    fields = (
        policy.identity_fields()
        if policy is not None and callable(getattr(policy, "identity_fields", None))
        else None
    )
    return evidence_digest(
        {
            "adapter": type(provider).__module__ + "." + type(provider).__qualname__,
            "name": provider.name,
            "model": provider.model,
            "base_url": getattr(provider, "base_url", None),
            "timeout_seconds": getattr(provider, "timeout_seconds", None),
            "max_output_tokens": getattr(provider, "max_output_tokens", None),
            "allow_model_override": getattr(provider, "allow_model_override", True),
            "routing_policy": fields,
        }
    )


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

    @property
    def pending_ids(self) -> tuple[str, ...]:
        return tuple(identity for identity, _ in self.pending)


def execute_call(
    request: ProviderRequest,
    *,
    provider: ReviewProvider,
    validate: Callable[[ProviderResponse], T],
    tracker: ResourceBudgetTracker,
    budgets: EffectiveWorkBudget,
    correction: str = "",
    repairable_validation: Callable[[Exception], bool] | None = None,
    before_dispatch: Callable[[], None] | None = None,
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
    corrected = False
    while True:
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
            budgets.max_total_output_bytes - tracker.response_bytes,
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
        if before_dispatch is not None:
            before_dispatch()
        try:
            response = provider.complete(bounded)
        except ProviderError as exc:
            if not exc.transient:
                return CallResult(diagnostic="provider_failed")
            reason = tracker.admit_transport_retry(exc.retry_after_seconds)
            if reason:
                return CallResult(diagnostic=reason)
            tracker.sleep_transport_retry(exc.retry_after_seconds)
            continue
        except (ReviewFormatError, ReviewInputError):
            return CallResult(diagnostic="invalid_provider_output")
        except Exception:
            return CallResult(diagnostic="provider_failed")
        if tracker.elapsed_ms() >= tracker.budget.timeout_ms:
            return CallResult(diagnostic="deadline_exceeded")
        if not isinstance(response, ProviderResponse):
            return CallResult(diagnostic="invalid_provider_output")
        tracker.record_response(response.text)
        if tracker.response_bytes > budgets.max_total_output_bytes:
            return CallResult(diagnostic="output_budget")
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
    for batch in plan.batches:
        try:
            request = render(batch)
        except ReviewInputError:
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
    if recovery is not None and recovery.enabled:
        if (
            encode_recovery is None
            or decode_recovery is None
            or revalidate_cached is None
        ):
            raise ReviewInputError(
                "work recovery requires normalized codecs and fresh semantic validation"
            )
        recovered = recovery.load(
            plan, tracker, budgets, decode_recovery, request_digests
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

    def save_state(next_index: int) -> None:
        if recovery is not None and encode_recovery is not None:
            retained_pending = pending + [
                (identity, "interrupted")
                for batch in plan.batches[next_index:]
                for identity in batch.requirement_ids
            ]
            recovery.save(
                WorkExecution(
                    plan,
                    tuple(completed),
                    tuple(sorted(retained_pending)),
                    tracker.execution_identity,
                ),
                tracker,
                encode_recovery,
                request_digests,
            )

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
            save_state(index + 1)
            continue
        planned_request = requests.get(batch.batch_id)
        if planned_request is None:
            pending.extend(
                (identity, "prompt_budget") for identity in batch.requirement_ids
            )
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
                save_state(index + 1)
                continue
            pending.extend(
                (identity, "output_budget")
                for remaining in plan.batches[index:]
                for identity in remaining.requirement_ids
            )
            break
        result = execute_call(
            request,
            provider=provider,
            validate=lambda response: validate(response, batch),
            tracker=tracker,
            budgets=budgets,
            correction=correction,
            repairable_validation=repairable_validation,
            before_dispatch=lambda: save_state(index),
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
        save_state(index + 1)
    save_state(len(plan.batches))
    return WorkExecution(
        plan, tuple(completed), tuple(sorted(pending)), tracker.execution_identity
    )
