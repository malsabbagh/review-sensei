"""Explicit analysis metadata windows, separate from long provider execution."""

from __future__ import annotations

from typing import Any, Callable

from .bounded_evidence import EvidenceReadBudget
from .errors import ReviewInputError


class AnalysisSessionPhases:
    """Own one admission and one completion budget, never refresh either."""

    def __init__(
        self,
        factory: Callable[[EvidenceReadBudget | None], Any],
        admission_budget: EvidenceReadBudget,
        *,
        completion_budget_factory: Callable[
            [], EvidenceReadBudget
        ] = EvidenceReadBudget,
    ) -> None:
        self._factory = factory
        self._completion_budget_factory = completion_budget_factory
        self._admission = factory(admission_budget)
        self._admission._analysis_phases = self
        self._completion: Any | None = None
        self._completion_started = False
        self._provider_started = False

    @property
    def admission(self) -> Any:
        return self._admission

    def begin_provider(self, ledger: Any) -> Any:
        if ledger is not self._admission or self._provider_started:
            raise ReviewInputError("analysis admission phase is already closed")
        assert self._admission.evidence_budget is not None
        self._admission.evidence_budget.check()
        self._admission._metadata_phase_closed = True
        self._provider_started = True
        # Fresh original connection settings, not a budget-stripped copy.
        # OperationHost owns its separate provider execution accounting.
        return self._factory(None)

    def completion(self, ledger: Any) -> Any:
        if (
            ledger is not self._admission
            and (self._completion is None or ledger is not self._completion)
        ) or not self._provider_started:
            raise ReviewInputError("analysis completion has no admitted provider phase")
        if not self._completion_started:
            self._completion_started = True
            self._completion = self._factory(self._completion_budget_factory())
            self._completion._analysis_phases = self
        if self._completion is None:
            raise ReviewInputError(
                "analysis completion initialization failed; no allowance reset"
            )
        return self._completion
