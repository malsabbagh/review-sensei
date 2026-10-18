"""Trusted, retained-evidence authority for one effective OpenRouter runtime.

Construct this at the host composition boundary, independently of PR content,
model output and the record under test. A receipt is a bounded consistency
binding, not a signature or proof that an operator's reports are truthful.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from review_sensei.errors import ReviewInputError
from review_sensei.evaluation import engine_digest, prompt_digest
from review_sensei.models import (
    ReviewResult,
    ReviewTransaction,
    transaction_stage_identity,
)
from review_sensei.openrouter_qualification import (
    MAX_EVIDENCE_ARTIFACT_BYTES,
    OpenRouterQualificationTarget,
    load_supported_openrouter_qualification,
)
from review_sensei.stages import Stage
from review_sensei.validation import DEFAULT_REVIEW_LIMITS, ReviewLimits

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")


def _json_bytes(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewInputError("qualification authority requires JSON inputs") from exc
    if len(raw) > MAX_EVIDENCE_ARTIFACT_BYTES:
        raise ReviewInputError("qualification authority input exceeds size limit")
    return raw


def _object(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > MAX_EVIDENCE_ARTIFACT_BYTES:
        raise ReviewInputError("qualification authority artifact exceeds size limit")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ReviewInputError("qualification authority artifact is invalid") from exc
    if not isinstance(value, dict):
        raise ReviewInputError("qualification authority artifact must be an object")
    return value


@dataclass(frozen=True, init=False, repr=False)
class TrustedOpenRouterQualification:
    """Immutable trusted inputs, rechecked for every publication/finalization.

    All ``expected_*`` values and the effective ``runtime_configuration`` must
    come from the host's approved configuration/evaluation inventory, never
    from the qualification document being checked. Evaluation configuration
    and runtime transaction configuration have different digest contracts;
    the host explicitly attests their applicability instead of equating them.
    ``runtime_stages``/``runtime_limits`` must be the actual analysis inputs.
    A separate instance is required when any effective configuration changes.
    """

    _document: bytes
    _artifacts: tuple[bytes, ...]
    _target: OpenRouterQualificationTarget
    _evaluated_at: str
    _reproducibility: bytes
    _engine: str
    _prompt: str
    _evaluation_configuration: str
    _corpus: str
    _runtime_configuration: bytes
    _runtime_digest: str
    _stages: tuple[Stage, ...]
    _limits: ReviewLimits

    def __init__(
        self,
        *,
        qualification_document: bytes,
        report_artifacts: Sequence[bytes],
        expected_target: OpenRouterQualificationTarget,
        expected_evaluated_at: str,
        expected_reproducibility: Mapping[str, Any],
        expected_engine_digest: str,
        expected_prompt_digest: str,
        expected_evaluation_configuration_digest: str,
        expected_corpus_digest: str,
        runtime_configuration: Mapping[str, object],
        runtime_stages: Sequence[Stage] | None = None,
        runtime_limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
    ) -> None:
        from review_sensei.service import DEFAULT_STAGES

        for digest in (
            expected_engine_digest,
            expected_prompt_digest,
            expected_evaluation_configuration_digest,
            expected_corpus_digest,
        ):
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ReviewInputError("qualification authority digest is invalid")
        if not isinstance(expected_target, OpenRouterQualificationTarget):
            raise ReviewInputError("qualification authority target is invalid")
        artifacts = tuple(report_artifacts)
        if not 3 <= len(artifacts) <= 32:
            raise ReviewInputError("qualification authority requires 3 to 32 reports")
        _object(qualification_document)
        for artifact in artifacts:
            _object(artifact)
        runtime_digest = ReviewTransaction.compute_configuration_digest(
            runtime_configuration
        )
        runtime = _object(_json_bytes(runtime_configuration))
        provider = runtime["provider"]
        stages = tuple(DEFAULT_STAGES if runtime_stages is None else runtime_stages)
        if not stages or any(not isinstance(stage, Stage) for stage in stages):
            raise ReviewInputError("qualification authority runtime stages invalid")
        stage_identity, categories = transaction_stage_identity(stages)
        if (
            provider["name"] != "openrouter"
            or runtime["model"] != expected_target.model
            or provider["base_url"] != expected_target.base_url
            or provider["openrouter_policy"]
            != dict(expected_target.routing_policy().identity_fields())
            or runtime["stages"] != stage_identity
            or runtime["category_policy"] != categories
            or any(stage.provider_profile is not None for stage in stages)
        ):
            raise ReviewInputError("qualification authority runtime target differs")
        values = {
            "_document": qualification_document,
            "_artifacts": artifacts,
            "_target": expected_target,
            "_evaluated_at": expected_evaluated_at,
            "_reproducibility": _json_bytes(expected_reproducibility),
            "_engine": expected_engine_digest,
            "_prompt": expected_prompt_digest,
            "_evaluation_configuration": expected_evaluation_configuration_digest,
            "_corpus": expected_corpus_digest,
            "_runtime_configuration": _json_bytes(runtime),
            "_runtime_digest": runtime_digest,
            "_stages": stages,
            "_limits": runtime_limits,
        }
        for key, value in values.items():
            object.__setattr__(self, key, value)
        self._checked_identity()

    def _checked_identity(self) -> str:
        checked = load_supported_openrouter_qualification(
            _object(self._document),
            [_object(raw) for raw in self._artifacts],
            report_artifacts=self._artifacts,
            expected_target=self._target,
            expected_evaluated_at=self._evaluated_at,
            expected_reproducibility=_object(self._reproducibility),
        )
        promotion = checked.promotion
        if (
            promotion.engine_digest != self._engine
            or promotion.prompt_digest != self._prompt
            or promotion.configuration_digest != self._evaluation_configuration
            or promotion.corpus_digest != self._corpus
            or engine_digest(limits=self._limits) != self._engine
            or prompt_digest(self._stages) != self._prompt
        ):
            raise ReviewInputError(
                "qualification authority evaluation identity differs"
            )
        return hashlib.sha256(
            _json_bytes(
                {
                    "protocol": "openrouter-runtime-qualification-v1",
                    "record": hashlib.sha256(self._document).hexdigest(),
                    "reports": list(checked.evidence_references),
                    "runtime_configuration_digest": self._runtime_digest,
                    "engine_digest": self._engine,
                    "prompt_digest": self._prompt,
                    "evaluation_configuration_digest": self._evaluation_configuration,
                    "corpus_digest": self._corpus,
                }
            )
        ).hexdigest()

    def _binding(
        self,
        *,
        repository: str,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        result_digest: str,
    ) -> str:
        """Recheck retained bytes and bind their authority to one publication."""
        if (
            not isinstance(repository, str)
            or not repository.strip()
            or isinstance(pull_request, bool)
            or not isinstance(pull_request, int)
            or pull_request < 1
            or not _SHA.fullmatch(base_sha)
            or not _SHA.fullmatch(head_sha)
            or not _SHA256.fullmatch(result_digest)
        ):
            raise ReviewInputError(
                "qualification authority publication identity invalid"
            )
        return hashlib.sha256(
            _json_bytes(
                {
                    "authority": self._checked_identity(),
                    "repository": repository,
                    "pull_request": pull_request,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "result_digest": result_digest,
                }
            )
        ).hexdigest()

    def issue_binding(
        self,
        *,
        result: ReviewResult,
        configuration_context: Mapping[str, object] | None,
        publication_mode: str,
        repository: str,
        pull_request: int,
        base_sha: str,
        head_sha: str,
    ) -> str | None:
        """Withhold when actual reviewed context is missing or differs."""
        from .publication import review_result_digest

        if not isinstance(result, ReviewResult) or configuration_context is None:
            return None
        try:
            digest = ReviewTransaction.compute_configuration_digest(
                configuration_context
            )
            if (
                configuration_context.get("publication_mode") != publication_mode
                or result.provider != "openrouter"
                or result.model != self._target.model
                or result.limits != self._limits
                or digest != self._runtime_digest
                or _json_bytes(configuration_context) != self._runtime_configuration
                or (
                    result.transaction is not None
                    and result.transaction.configuration_digest != digest
                )
            ):
                return None
            return self._binding(
                repository=repository,
                pull_request=pull_request,
                base_sha=base_sha,
                head_sha=head_sha,
                result_digest=review_result_digest(result),
            )
        except ReviewInputError:
            return None

    def authorizes(
        self,
        receipt: str | None,
        *,
        repository: str,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        result_digest: str,
    ) -> bool:
        """An accepting persisted string never substitutes for current evidence."""
        try:
            return receipt is not None and receipt == self._binding(
                repository=repository,
                pull_request=pull_request,
                base_sha=base_sha,
                head_sha=head_sha,
                result_digest=result_digest,
            )
        except ReviewInputError:
            return False
