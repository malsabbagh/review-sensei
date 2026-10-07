"""One bounded provider execution mechanism for both review work modes.

Only validated batches enter the aggregate. Diagnostic records contain no
prompts, source patches, provider output, or exception messages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Generic, TypeVar

from .budgets import EffectiveWorkBudget
from .errors import ProviderError, ReviewFormatError, ReviewInputError
from .evidence import evidence_digest
from .models import ProviderRequest, ProviderResponse
from .outcomes import ResourceBudgetTracker
from .planning import ReviewWorkPlan, WorkBatch
from .providers.base import ReviewProvider
from .validation import utf8_size, validate_bounded_text

T = TypeVar("T")


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
        except (ReviewFormatError, ReviewInputError, ValueError, TypeError):
            if (
                not correction
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
) -> WorkExecution[T]:
    tracker.budget.validate_against_limits()
    if plan.budget_digest != budgets.digest or (
        budgets.resource_budget is not None
        and tracker.budget != budgets.resource_budget
    ):
        raise ReviewInputError("work execution budgets do not match the admitted plan")
    cached: dict[str, CompletedBatch[T]] = {}
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
    for index, batch in enumerate(plan.batches):
        try:
            request = render(batch)
        except ReviewInputError:
            pending.extend(
                (identity, "prompt_budget") for identity in batch.requirement_ids
            )
            continue
        request_digest = evidence_digest(
            {
                "domain": "reviewsensei:work-request:v1",
                "prompt": request.prompt,
                "provider": provider.name,
                "provider_model": provider.model,
                "model": request.model,
                "json_mode": request.json_mode,
                "output_tokens": request.max_output_tokens,
                "max_prompt_bytes": request.max_prompt_bytes,
                "max_response_bytes": request.max_response_bytes,
                "budget": budgets.digest,
            }
        )
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
    return WorkExecution(
        plan, tuple(completed), tuple(sorted(pending)), tracker.execution_identity
    )
