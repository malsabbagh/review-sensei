"""Review-only closed choices; independent of inactive comment-intent proposals.

Model assessments never establish evidence, disposition or publication authority.
The application binds native responses to the request it actually dispatched.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Protocol

from .decision_execution import DecisionExecution
from .decision_provider import digest
from .errors import ReviewInputError

MAX_PACKET_BYTES = 16_384
MAX_QUESTIONS = 16
PROBABILITY_TOLERANCE = 0.000001


def bounded_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is not None
    )


def bounded_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-8")) <= 256
        and not any(ord(c) < 32 for c in value)
    )


@dataclass(frozen=True)
class ReviewDecisionQuestion:
    name: str
    subject: str
    description: str
    choices: tuple[str, ...]

    def __post_init__(self) -> None:
        if not bounded_name(self.name) or not bounded_name(self.subject):
            raise ReviewInputError("review decision question identity is invalid")
        if (
            not bounded_identity(self.description)
            or type(self.choices) is not tuple
            or not 2 <= len(self.choices) <= 8
            or len(set(self.choices)) != len(self.choices)
            or any(not bounded_name(c) for c in self.choices)
        ):
            raise ReviewInputError("review decision choices are invalid")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "subject": self.subject,
            "description": self.description,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class ReviewDecisionRequest:
    state: str
    questions: tuple[ReviewDecisionQuestion, ...]
    provider: str
    model: str

    def __post_init__(self) -> None:
        if not bounded_identity(self.provider) or not bounded_identity(self.model):
            raise ReviewInputError("review decision provider identity is invalid")
        if (
            not isinstance(self.state, str)
            or not 1 <= len(self.state.encode("utf-8")) <= MAX_PACKET_BYTES
        ):
            raise ReviewInputError("review decision packet exceeds its bound")
        if not isinstance(strict_json(self.state), dict):
            raise ReviewInputError("review decision state must be a structured object")
        if (
            type(self.questions) is not tuple
            or not 1 <= len(self.questions) <= MAX_QUESTIONS
            or any(type(q) is not ReviewDecisionQuestion for q in self.questions)
            or len({q.name for q in self.questions}) != len(self.questions)
        ):
            raise ReviewInputError("review decision question inventory is invalid")

    def to_dict(self) -> dict:
        return {
            "schema_version": "review-decision/1",
            "state": self.state,
            "questions": [q.to_dict() for q in self.questions],
            "provider": self.provider,
            "model": self.model,
        }

    def digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True)
class ReviewDecisionAnswer:
    name: str
    choice: str
    probabilities: tuple[tuple[str, float], ...] | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class ReviewDecisionResponse:
    request_sha256: str
    provider: str
    requested_model: str
    actual_model: str
    answers: tuple[ReviewDecisionAnswer, ...]
    actual_provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def validate(self, request: ReviewDecisionRequest) -> None:
        if (
            self.request_sha256 != request.digest()
            or self.provider != request.provider
            or self.requested_model != request.model
            or not bounded_identity(self.actual_model)
            or (
                self.actual_provider is not None
                and not bounded_identity(self.actual_provider)
            )
        ):
            raise ReviewInputError("review decision response identity conflicts")
        if (
            type(self.answers) is not tuple
            or len(self.answers) != len(request.questions)
            or any(type(a) is not ReviewDecisionAnswer for a in self.answers)
        ):
            raise ReviewInputError("review decision response is incomplete")
        answers = {a.name: a for a in self.answers}
        if len(answers) != len(self.answers) or set(answers) != {
            q.name for q in request.questions
        }:
            raise ReviewInputError("review decision answer inventory conflicts")
        for question in request.questions:
            answer = answers[question.name]
            if answer.choice not in question.choices:
                raise ReviewInputError(
                    "review decision answer is not an allowed choice"
                )
            if answer.confidence is not None:
                _probability(answer.confidence)
            distribution = answer.probabilities
            if distribution is not None:
                if type(distribution) is not tuple or any(
                    type(p) is not tuple or len(p) != 2 for p in distribution
                ):
                    raise ReviewInputError("decision distribution is invalid")
                labels = [p[0] for p in distribution]
                if len(labels) != len(set(labels)) or set(labels) != set(
                    question.choices
                ):
                    raise ReviewInputError("decision distribution labels conflict")
                for _, probability in distribution:
                    _probability(probability)
                if abs(sum(p for _, p in distribution) - 1.0) > PROBABILITY_TOLERANCE:
                    raise ReviewInputError("decision distribution is not normalized")
        if any(
            v is not None
            and (type(v) is not int or not 0 <= v <= 9_007_199_254_740_991)
            for v in (self.input_tokens, self.output_tokens)
        ):
            raise ReviewInputError("decision usage is invalid")

    def metadata(self) -> dict:
        return {
            "provider": self.provider,
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "actual_provider": self.actual_provider,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def _probability(value: object) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 1
        or not math.isfinite(value)
    ):
        raise ReviewInputError("decision probability is invalid")


class ReviewDecisionProvider(Protocol):
    name: str
    model: str

    def evaluate(
        self, request: ReviewDecisionRequest, execution: DecisionExecution
    ) -> ReviewDecisionResponse:
        """Charge each attempted dispatch; no retries, tools or fallback."""
        ...


def strict_json(raw: str) -> object:
    import json

    def unique(pairs: list[tuple[str, object]]) -> Mapping[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ReviewInputError("duplicate decision JSON key")
            value[key] = item
        return value

    try:
        return json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, RecursionError) as exc:
        raise ReviewInputError("decision response is invalid JSON") from exc
