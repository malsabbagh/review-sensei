"""One trusted admission policy for discovery and human reassessment work.

Byte ceilings never certify model context capacity. A qualified capability is
an operator-owned, offline contract, not information supplied by a model or PR.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .errors import ReviewInputError
from .outcomes import ResourceBudget
from .validation import DEFAULT_REVIEW_LIMITS, ReviewLimits, utf8_size

WORK_MODES = frozenset({"discovery", "reassessment"})
LEGACY_ASSESSMENT_PROMPT_BYTES = 48 * 1024
ASSESSMENT_OUTPUT_BYTES = 16 * 1024
MAX_TOTAL_PROMPT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_OUTPUT_BYTES = 1024 * 1024


def _positive(value: int, maximum: int, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ReviewInputError(f"{label} exceeds the supported resource bounds")


def provider_output_tokens(provider: object) -> int | None:
    """Read the configured adapter output cap, never infer model context size."""
    value = getattr(provider, "max_output_tokens", None)
    if value is not None:
        _positive(value, 16384, "provider output token ceiling")
    return value


@dataclass(frozen=True)
class ProviderCapabilities:
    """Trusted qualified context and output limits for one selected model.

    The UTF-8 byte upper bound must have been qualified for this tokenizer before
    ``qualified`` is true. No four-characters-per-token estimate or live discovery
    is used. Unqualified capabilities never expand existing prompt admission.
    """

    context_tokens: int
    max_output_tokens: int
    qualified: bool = False
    framing_tokens: int = 256
    estimator: str = "utf8-byte-upper-bound"
    provider_name: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        _positive(self.context_tokens, 4_194_304, "provider context tokens")
        _positive(self.max_output_tokens, 16_384, "provider output tokens")
        _positive(self.framing_tokens, 16_384, "provider framing tokens")
        if (
            not isinstance(self.qualified, bool)
            or self.estimator != "utf8-byte-upper-bound"
        ):
            raise ReviewInputError("provider capability qualification is invalid")
        if self.max_output_tokens + self.framing_tokens >= self.context_tokens:
            raise ReviewInputError("provider capability has no input capacity")
        if self.qualified and (
            not isinstance(self.provider_name, str)
            or not self.provider_name.strip()
            or not isinstance(self.model, str)
            or not self.model.strip()
        ):
            raise ReviewInputError(
                "qualified capability requires exact provider and model"
            )

    def require_identity(self, *, provider_name: str, model: str | None) -> None:
        if self.qualified and (
            self.provider_name != provider_name or self.model != model
        ):
            raise ReviewInputError(
                "qualified capability does not match selected provider and model"
            )

    def fits(self, prompt: str, *, output_tokens: int | None = None) -> bool:
        if not self.qualified:
            return False
        reserved = self.max_output_tokens if output_tokens is None else output_tokens
        _positive(reserved, self.max_output_tokens, "reserved output tokens")
        margin = max(512, (self.context_tokens + 19) // 20)
        return (
            utf8_size(prompt, label="provider input")
            + self.framing_tokens
            + reserved
            + margin
            <= self.context_tokens
        )


@dataclass(frozen=True)
class EffectiveWorkBudget:
    batch_diff_bytes: int
    batch_prompt_bytes: int
    batch_output_bytes: int
    max_total_prompt_bytes: int
    max_total_output_bytes: int
    max_findings_per_batch: int
    provider_qualified: bool
    capabilities: ProviderCapabilities | None = None
    max_output_tokens: int | None = None
    resource_budget: ResourceBudget | None = None

    def __post_init__(self) -> None:
        for name, maximum in (
            ("batch_diff_bytes", DEFAULT_REVIEW_LIMITS.max_diff_bytes),
            ("batch_prompt_bytes", DEFAULT_REVIEW_LIMITS.max_prompt_bytes),
            ("batch_output_bytes", DEFAULT_REVIEW_LIMITS.max_provider_response_bytes),
            ("max_total_prompt_bytes", MAX_TOTAL_PROMPT_BYTES),
            ("max_total_output_bytes", MAX_TOTAL_OUTPUT_BYTES),
            ("max_findings_per_batch", 20),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= maximum
            ):
                raise ReviewInputError("effective work budget is invalid")
        if not isinstance(
            self.provider_qualified, bool
        ) or self.provider_qualified != bool(
            self.capabilities and self.capabilities.qualified
        ):
            raise ReviewInputError("effective provider qualification is invalid")
        if self.max_output_tokens is not None:
            _positive(self.max_output_tokens, 16384, "effective output tokens")
        if self.resource_budget is not None:
            if not isinstance(self.resource_budget, ResourceBudget):
                raise ReviewInputError("effective resource budget is invalid")
            self.resource_budget.validate_against_limits()

    @property
    def digest(self) -> str:
        from .evidence import evidence_digest

        return evidence_digest(asdict(self))

    def fits_prompt(self, prompt: str, *, output_tokens: int | None = None) -> bool:
        if utf8_size(prompt, label="work prompt") > self.batch_prompt_bytes:
            return False
        return not self.provider_qualified or (
            self.capabilities is not None
            and self.capabilities.fits(prompt, output_tokens=output_tokens)
        )


@dataclass(frozen=True)
class ReviewWorkBudgets:
    """Opt-in targets intersected with existing public and run ceilings."""

    mode: str = "legacy"
    batch_diff_bytes: int = 128 * 1024
    batch_prompt_bytes: int = 256 * 1024
    max_total_prompt_bytes: int = MAX_TOTAL_PROMPT_BYTES
    max_total_output_bytes: int = MAX_TOTAL_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if self.mode not in {"legacy", "unified"}:
            raise ReviewInputError("review work mode is invalid")
        for label, maximum in (
            ("batch_diff_bytes", DEFAULT_REVIEW_LIMITS.max_diff_bytes),
            ("batch_prompt_bytes", DEFAULT_REVIEW_LIMITS.max_prompt_bytes),
            ("max_total_prompt_bytes", MAX_TOTAL_PROMPT_BYTES),
            ("max_total_output_bytes", MAX_TOTAL_OUTPUT_BYTES),
        ):
            _positive(getattr(self, label), maximum, f"review work {label}")

    def effective(
        self,
        *,
        limits: ReviewLimits,
        resource: ResourceBudget,
        mode: str,
        capabilities: ProviderCapabilities | None = None,
        output_tokens: int | None = None,
    ) -> EffectiveWorkBudget:
        if mode not in WORK_MODES:
            raise ReviewInputError("review work requirement mode is invalid")
        resource.validate_against_limits(limits)
        if capabilities is not None and not isinstance(
            capabilities, ProviderCapabilities
        ):
            raise ReviewInputError("review work provider capabilities are invalid")
        qualified = capabilities is not None and capabilities.qualified
        if output_tokens is not None:
            _positive(output_tokens, 16384, "provider output token ceiling")
        if capabilities is not None:
            output_tokens = (
                min(output_tokens, capabilities.max_output_tokens)
                if output_tokens is not None
                else capabilities.max_output_tokens
            )
        prompt = min(
            self.batch_prompt_bytes, limits.max_prompt_bytes, resource.max_prompt_bytes
        )
        output = min(limits.max_provider_response_bytes, resource.max_output_bytes)
        if mode == "reassessment":
            output = min(output, ASSESSMENT_OUTPUT_BYTES)
            if not qualified:
                prompt = min(prompt, LEGACY_ASSESSMENT_PROMPT_BYTES)
        elif self.mode == "unified" and not qualified:
            prompt = min(prompt, LEGACY_ASSESSMENT_PROMPT_BYTES)
        findings = 4
        if output_tokens is not None:
            # Reserve framing and at least 512 output tokens per decision. This
            # controls packing; strict normalized response validation still
            # rejects an unexpectedly large or truncated provider result.
            findings = min(findings, max(0, (output_tokens - 256) // 512))
        return EffectiveWorkBudget(
            batch_diff_bytes=min(self.batch_diff_bytes, limits.max_diff_bytes),
            batch_prompt_bytes=prompt,
            batch_output_bytes=output,
            max_total_prompt_bytes=self.max_total_prompt_bytes,
            max_total_output_bytes=self.max_total_output_bytes,
            max_findings_per_batch=findings,
            provider_qualified=qualified,
            capabilities=capabilities,
            max_output_tokens=output_tokens,
            resource_budget=resource,
        )
