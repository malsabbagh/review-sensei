"""Closed-choice provider contract. No adapter, registry or dispatch is enabled.

An embedding host must admit and charge extraction within its original budget.
This protocol does not grant work, retry unknown requests, or evaluate code.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from .errors import ReviewInputError
from .outcomes import ResourceBudgetTracker
from .validation import utf8_size

MAX_DECISION_BYTES = 16_384
MAX_DECISION_QUESTIONS = 16


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class DecisionQuestion:
    name: str
    choices: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name) is None
            or type(self.choices) is not tuple
            or not 2 <= len(self.choices) <= 32
            or any(
                not isinstance(c, str)
                or not 1 <= utf8_size(c, label="decision choice") <= 64
                for c in self.choices
            )
            or len(set(self.choices)) != len(self.choices)
        ):
            raise ReviewInputError("decision question is invalid")


@dataclass(frozen=True)
class DecisionRequest:
    source_sha256: str
    state_sha256: str
    provider: str
    model: str
    text: str
    questions: tuple[DecisionQuestion, ...]
    rubric: str = "comment-intent-choice-v1"

    def __post_init__(self) -> None:
        if (
            not sha256(self.source_sha256)
            or not sha256(self.state_sha256)
            or any(
                not isinstance(v, str) or not 1 <= len(v) <= 128
                for v in (self.provider, self.model, self.rubric)
            )
            or not isinstance(self.text, str)
            or not 1 <= utf8_size(self.text, label="decision input") <= 4096
            or type(self.questions) is not tuple
            or not 1 <= len(self.questions) <= MAX_DECISION_QUESTIONS
            or any(not isinstance(q, DecisionQuestion) for q in self.questions)
            or len({q.name for q in self.questions}) != len(self.questions)
        ):
            raise ReviewInputError("decision request is invalid")
        if len(canonical(self.to_dict()).encode("utf-8")) > MAX_DECISION_BYTES:
            raise ReviewInputError("decision request exceeds bounded envelope")

    def to_dict(self) -> dict:
        return {
            "schema_version": "1.0",
            "source_sha256": self.source_sha256,
            "state_sha256": self.state_sha256,
            "provider": self.provider,
            "model": self.model,
            "text": self.text,
            "rubric": self.rubric,
            "questions": [
                {"name": q.name, "choices": list(q.choices)} for q in self.questions
            ],
        }

    def digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True)
class DecisionResponse:
    request_sha256: str
    provider: str
    model: str
    answers: tuple[tuple[str, str], ...]
    input_tokens: int | None
    output_tokens: int | None

    def __post_init__(self) -> None:
        if (
            not sha256(self.request_sha256)
            or any(
                not isinstance(v, str) or not 1 <= len(v) <= 128
                for v in (self.provider, self.model)
            )
            or type(self.answers) is not tuple
            or not 1 <= len(self.answers) <= MAX_DECISION_QUESTIONS
            or any(
                type(pair) is not tuple
                or len(pair) != 2
                or any(not isinstance(v, str) or not 1 <= len(v) <= 64 for v in pair)
                for pair in self.answers
            )
            or len(dict(self.answers)) != len(self.answers)
            or any(
                v is not None
                and (type(v) is not int or not 0 <= v <= 9_007_199_254_740_991)
                for v in (self.input_tokens, self.output_tokens)
            )
        ):
            raise ReviewInputError("typed decision response is invalid")

    @property
    def usage_known(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None

    def to_dict(self) -> dict:
        return {
            "schema_version": "1.0",
            "request_sha256": self.request_sha256,
            "provider": self.provider,
            "model": self.model,
            "completion": "complete",
            "answers": dict(self.answers),
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
        }


class DecisionProvider(Protocol):
    """Typed choice seam; host owns capability, privacy and attempt admission.

    Implementations must charge attempted/unknown consumption to ``tracker`` and
    must not fall back, call tools, retry, or dispatch other review work. There
    is deliberately no installed implementation or automatic invocation here.
    """

    name: str
    model: str

    def evaluate(
        self, request: DecisionRequest, tracker: ResourceBudgetTracker
    ) -> DecisionResponse: ...


def _unique(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ReviewInputError("duplicate decision response key")
        value[key] = item
    return value


def parse_decision_response(raw: str, request: DecisionRequest) -> DecisionResponse:
    """Reject incomplete, unbound or non-choice answers before planning.

    Unknown token usage remains unknown; this parser never refunds or completes
    an attempt. Errors contain no provider text or credential-bearing metadata.
    """
    if (
        not isinstance(raw, str)
        or utf8_size(raw, label="decision response") > MAX_DECISION_BYTES
    ):
        raise ReviewInputError("decision response exceeds bounded envelope")
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as exc:
        raise ReviewInputError("decision response is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "request_sha256",
            "provider",
            "model",
            "completion",
            "answers",
            "usage",
        }
        or value["schema_version"] != "1.0"
        or value["request_sha256"] != request.digest()
        or value["provider"] != request.provider
        or value["model"] != request.model
        or value["completion"] != "complete"
    ):
        raise ReviewInputError("decision completion or identity conflicts")
    answers, usage = value["answers"], value["usage"]
    if (
        not isinstance(answers, dict)
        or set(answers) != {q.name for q in request.questions}
        or any(
            not isinstance(answers[q.name], str) or answers[q.name] not in q.choices
            for q in request.questions
        )
        or not isinstance(usage, dict)
        or set(usage) != {"input_tokens", "output_tokens"}
        or any(
            v is not None
            and (type(v) is not int or not 0 <= v <= 9_007_199_254_740_991)
            for v in usage.values()
        )
    ):
        raise ReviewInputError("decision answers or usage are invalid")
    return DecisionResponse(
        request.digest(),
        request.provider,
        request.model,
        tuple((q.name, answers[q.name]) for q in request.questions),
        usage["input_tokens"],
        usage["output_tokens"],
    )
