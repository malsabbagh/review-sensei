"""Bounded original-source item manifests and inactive batch proposals.

This module prepares requests, validates observations and plans data. It never
calls a provider, creates an allowance, dispatches commands or publishes.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Mapping

from .comment_intent import (
    INTENTS,
    POLARITIES,
    STANCES,
    ActionProposal,
    CommentObservation,
    ResolvedActionFacts,
    _clause_refusal,
    _plan_clause_action,
    _references,
    _requires_explicit_command,
    current_source_refusal,
    proposal,
)
from .decision_provider import (
    DecisionQuestion,
    DecisionRequest,
    DecisionResponse,
    canonical,
    digest,
    parse_decision_response,
    sha256,
)
from .errors import ReviewInputError
from .validation import utf8_size

SEGMENTATION = "delimited-items-v1"
MAX_ITEMS = 10
MAX_QUESTIONS = 40


def _valid_span(value: object) -> bool:
    return (
        type(value) is tuple
        and len(value) == 2
        and all(type(v) is int for v in value)
        and 0 <= value[0] < value[1] <= 4096
    )


def _distinct(values: tuple) -> bool:
    return len(set(values)) == len(values)


@dataclass(frozen=True)
class CommentItem:
    item_id: str
    span: tuple[int, int]
    references: tuple[tuple[str, str, int, int], ...]
    static_reason: str | None = None
    qualifier_actions: tuple[str, ...] = ()
    qualifier_targets: tuple[str, ...] = ()
    qualifier_broad: bool = False

    def __post_init__(self) -> None:
        if (
            not sha256(self.item_id)
            or not _valid_span(self.span)
            or type(self.references) is not tuple
            or len(self.references) > 10
        ):
            raise ReviewInputError("comment item is invalid")
        for ref in self.references:
            if (
                type(ref) is not tuple
                or len(ref) != 4
                or ref[0] not in {"finding", "commit"}
                or not isinstance(ref[1], str)
                or not 1 <= utf8_size(ref[1]) <= 64
                or not _valid_span((ref[2], ref[3]))
                or not self.span[0] <= ref[2] < ref[3] <= self.span[1]
                or utf8_size(ref[1]) != ref[3] - ref[2]
            ):
                raise ReviewInputError("comment reference provenance is invalid")
        if (
            self.static_reason is not None
            and (
                not isinstance(self.static_reason, str)
                or not 1 <= len(self.static_reason) <= 128
            )
            or type(self.qualifier_actions) is not tuple
            or len(self.qualifier_actions) > len(INTENTS)
            or any(v not in (*INTENTS, "*") for v in self.qualifier_actions)
            or not _distinct(self.qualifier_actions)
            or type(self.qualifier_targets) is not tuple
            or len(self.qualifier_targets) > 10
            or any(
                not isinstance(v, str) or re.fullmatch(r"RS-[A-F0-9]{6}", v) is None
                for v in self.qualifier_targets
            )
            or not _distinct(self.qualifier_targets)
            or type(self.qualifier_broad) is not bool
        ):
            raise ReviewInputError("comment item eligibility is invalid")


@dataclass(frozen=True)
class CommentItemManifest:
    source_sha256: str
    state_sha256: str
    items: tuple[CommentItem, ...]
    segmentation: str = SEGMENTATION

    def __post_init__(self) -> None:
        if (
            not sha256(self.source_sha256)
            or not sha256(self.state_sha256)
            or self.segmentation != SEGMENTATION
            or type(self.items) is not tuple
            or not 1 <= len(self.items) <= MAX_ITEMS
            or any(not isinstance(i, CommentItem) for i in self.items)
            or not _distinct(tuple(i.item_id for i in self.items))
            or sum(len(i.references) for i in self.items) > 10
            or any(a.span[1] > b.span[0] for a, b in zip(self.items, self.items[1:]))
        ):
            raise ReviewInputError("comment item manifest is invalid")

    def digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class PreparedCommentBatch:
    manifest: CommentItemManifest | None = None
    requests: tuple[DecisionRequest, ...] = ()
    terminal: ActionProposal | None = None

    def __post_init__(self) -> None:
        if (
            self.manifest is not None
            and not isinstance(self.manifest, CommentItemManifest)
            or self.terminal is not None
            and not isinstance(self.terminal, ActionProposal)
            or self.manifest is None
            and self.terminal is None
            or type(self.requests) is not tuple
            or len(self.requests) > 3
            or any(not isinstance(r, DecisionRequest) for r in self.requests)
            or self.terminal is not None
            and bool(self.requests)
            or sum(len(r.questions) for r in self.requests) > MAX_QUESTIONS
        ):
            raise ReviewInputError("comment batch preparation is invalid")
        if self.manifest and any(
            r.source_sha256 != self.manifest.source_sha256
            or r.state_sha256 != self.manifest.state_sha256
            for r in self.requests
        ):
            raise ReviewInputError("comment request source identity conflicts")

    def digest(self) -> str:
        return digest(
            {
                "manifest": asdict(self.manifest) if self.manifest else None,
                "requests": [r.to_dict() for r in self.requests],
                "terminal": asdict(self.terminal) if self.terminal else None,
            }
        )


@dataclass(frozen=True)
class GroupObservation:
    request_sha256: str
    status: str
    response: DecisionResponse | None

    def __post_init__(self) -> None:
        if (
            not sha256(self.request_sha256)
            or self.status not in {"accepted", "invalid", "pending"}
            or self.status == "accepted"
            and (
                not isinstance(self.response, DecisionResponse)
                or self.response.request_sha256 != self.request_sha256
            )
            or self.status != "accepted"
            and self.response is not None
        ):
            raise ReviewInputError("comment group observation is invalid")

    @property
    def usage_known(self) -> bool:
        return self.response is not None and self.response.usage_known


@dataclass(frozen=True)
class ItemExtraction:
    item_id: str
    status: str
    answers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (
            not sha256(self.item_id)
            or self.status not in {"static", "accepted", "invalid", "pending"}
            or type(self.answers) is not tuple
            or len(self.answers) > 13
            or any(
                type(p) is not tuple
                or len(p) != 2
                or any(not isinstance(v, str) for v in p)
                for p in self.answers
            )
            or not _distinct(tuple(p[0] for p in self.answers))
        ):
            raise ReviewInputError("comment item extraction is invalid")
        values = dict(self.answers)
        if self.status != "accepted":
            if values:
                raise ReviewInputError("unaccepted item has semantic answers")
            return
        if (
            not {"intent", "stance", "polarity"} <= set(values)
            or values["intent"] not in INTENTS
            or values["stance"] not in STANCES
            or values["polarity"] not in POLARITIES
            or any(
                (
                    not re.fullmatch(r"reference_[0-9]", k)
                    or v not in {"use", "ignore", "uncertain"}
                )
                for k, v in values.items()
                if k not in {"intent", "stance", "polarity"}
            )
        ):
            raise ReviewInputError("comment item choices are invalid")


@dataclass(frozen=True)
class ValidatedBatchExtraction:
    preparation_sha256: str
    groups: tuple[GroupObservation, ...]
    items: tuple[ItemExtraction, ...]

    def __post_init__(self) -> None:
        if (
            not sha256(self.preparation_sha256)
            or type(self.groups) is not tuple
            or len(self.groups) > 3
            or any(not isinstance(g, GroupObservation) for g in self.groups)
            or not _distinct(tuple(g.request_sha256 for g in self.groups))
            or type(self.items) is not tuple
            or not 1 <= len(self.items) <= MAX_ITEMS
            or any(not isinstance(i, ItemExtraction) for i in self.items)
            or not _distinct(tuple(i.item_id for i in self.items))
        ):
            raise ReviewInputError("normalized comment extraction is invalid")

    @property
    def usage_known(self) -> bool:
        return all(g.usage_known for g in self.groups)


@dataclass(frozen=True)
class CommentItemProposal:
    item_id: str
    span: tuple[int, int]
    extraction_status: str
    proposal: ActionProposal

    def __post_init__(self) -> None:
        if (
            not sha256(self.item_id)
            or not _valid_span(self.span)
            or self.extraction_status
            not in {"static", "accepted", "pending", "invalid"}
            or not isinstance(self.proposal, ActionProposal)
            or not sha256(self.proposal.source_sha256)
            or not sha256(self.proposal.state_sha256)
            or self.proposal.claim is not None
            and self.proposal.claim.source_span_utf8 != self.span
        ):
            raise ReviewInputError("comment item proposal is invalid")

    @property
    def executable(self) -> bool:
        return False

    @property
    def idempotency_key(self) -> str:
        # Delivery identity is independent of model output and array ordering.
        return digest(
            {
                "domain": "comment-item-delivery-v1",
                "item": self.item_id,
                "source": self.proposal.source_sha256,
            }
        )

    @property
    def operation_key(self) -> str | None:
        # Equivalent pending work can share an effect identity, but all source
        # claims remain visible. This is not an admission or persisted receipt.
        p = self.proposal
        if p.status != "proposed" or p.action is None:
            return None
        return digest(
            {
                "domain": "comment-item-operation-v1",
                "source": p.source_sha256,
                "state": p.state_sha256,
                "action": p.action,
                "targets": sorted(p.targets),
                "scope": asdict(p.scope),
            }
        )


@dataclass(frozen=True)
class CommentBatchProposal:
    source_sha256: str
    state_sha256: str
    items: tuple[CommentItemProposal, ...]
    extraction_complete: bool
    status: str = "planned"
    reason: str | None = None

    def __post_init__(self) -> None:
        if (
            not sha256(self.source_sha256)
            or not sha256(self.state_sha256)
            or type(self.items) is not tuple
            or len(self.items) > MAX_ITEMS
            or any(not isinstance(i, CommentItemProposal) for i in self.items)
            or not _distinct(tuple(i.item_id for i in self.items))
            or type(self.extraction_complete) is not bool
            or self.extraction_complete
            and any(i.extraction_status in {"pending", "invalid"} for i in self.items)
            or any(
                i.proposal.source_sha256 != self.source_sha256
                or i.proposal.state_sha256 != self.state_sha256
                for i in self.items
            )
        ):
            raise ReviewInputError("comment batch proposal is invalid")

    @property
    def executable(self) -> bool:
        return False


def terminal_batch(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    terminal: ActionProposal,
) -> CommentBatchProposal:
    span = (0, utf8_size(observation.body))
    items = (
        ()
        if not span[1]
        else (
            CommentItemProposal(
                digest(
                    {
                        "source": observation.fingerprint(),
                        "span": span,
                        "segmentation": SEGMENTATION,
                    }
                ),
                span,
                "static",
                terminal,
            ),
        )
    )
    return CommentBatchProposal(
        observation.fingerprint(),
        facts.fingerprint(),
        items,
        True,
        terminal.status,
        terminal.reason,
    )


def _protected_characters(body: str) -> list[bool]:
    """Conservative quote/fence context across the complete original source."""
    mask = [False] * len(body)
    lines = []
    start = 0
    for line in body.split("\n"):
        end = start + len(line)
        lines.append((start, end, line))
        start = end + 1
    fence = None
    quote_block = False
    for start, end, line in lines:
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            closed = bool(
                fence_match
                and fence_match.group(1)[0] == fence[0]
                and len(fence_match.group(1)) >= len(fence)
                and not line[fence_match.end() :].strip()
            )
            stop = end if closed else min(end + 1, len(body))
            mask[start:stop] = [True] * (stop - start)
            if closed:
                fence = None
            continue
        if fence_match:
            fence = fence_match.group(1)
            mask[start : min(end + 1, len(body))] = [True] * (
                min(end + 1, len(body)) - start
            )
            continue
        if not line.strip():
            quote_block = False
        if line.lstrip().startswith(">"):
            quote_block = True
        if quote_block or line.startswith(("    ", "\t")):
            mask[start:end] = [True] * (end - start)
    pairs = {'"': '"', "“": "”", "«": "»", "‘": "’"}
    i = 0
    while i < len(body):
        if mask[i]:
            i += 1
            continue
        c = body[i]
        if c == "<":
            # Unsupported HTML can carry scope across lines. Never isolate an
            # apparent action inside an unknown HTML region as independent.
            mask[i:] = [True] * (len(body) - i)
            break
        marker = c
        if c == "`":
            matched = re.match(r"`+", body[i:])
            assert matched is not None
            marker = matched.group()
            closing = marker
        elif c == "'" and (i == 0 or not body[i - 1].isalnum()):
            closing = "'"
        elif c in pairs:
            closing = pairs[c]
        else:
            i += 1
            continue
        end = body.find(closing, i + len(marker))
        while end >= 0:
            after = end + len(closing)
            escaped = len(body[:end]) - len(body[:end].rstrip("\\"))
            inside_word = closing == "'" and after < len(body) and body[after].isalnum()
            wrong_run = closing.startswith("`") and (
                (end and body[end - 1] == "`")
                or (after < len(body) and body[after] == "`")
            )
            if escaped % 2 == 0 and not inside_word and not wrong_run:
                break
            end = body.find(closing, end + 1)
        end = len(body) if end < 0 else end + len(closing)
        mask[i:end] = [True] * (end - i)
        i = end
    return mask


def _broad_qualifier(text: str) -> bool:
    return (
        re.search(
            r"\b(?:either|both|all|these|those|only[ \t]+(?:the[ \t]+)?first|former|latter|above|below|them)\b",
            text,
            re.I,
        )
        is not None
    )


def _qualifier_actions(text: str) -> tuple[str, ...]:
    pointer = _broad_qualifier(text)
    restriction = re.search(
        r"\b(?:not|don't|never|only|unless|except|instead|rather|maybe|if|no[ \t]+need)\b",
        text,
        re.I,
    )
    request_guard = re.search(
        r"\b(?:if|unless|maybe|perhaps|possibly)\b"
        r"|\b(?:do[ \t]+not|don't|never|cannot|can't|not[ \t]+to|no[ \t]+need[ \t]+to)"
        r"[ \t]+(?:assess|review|verify|rescan|recheck|resume|continue|approve|override)\b",
        text,
        re.I,
    )
    if not request_guard and not (pointer and restriction):
        return ()
    kinds = []
    for pattern, intent in (
        (r"\b(?:assess|fix|fixed)\b", "assess_claim"),
        (r"\b(?:verify|recheck|approval|approve)\b", "recheck_approval"),
        (r"\b(?:resume|continue)\b", "resume_review"),
        (r"\b(?:rescan|afresh|fresh)\b", "fresh_review"),
        (r"\bremaining\b", "remaining_review"),
        (r"\b(?:override|risk)\b", "risk_disposition"),
    ):
        if re.search(pattern, text, re.I):
            kinds.append(intent)
    return tuple(kinds) or ("*",)


def build_item_manifest(
    observation: CommentObservation, facts: ResolvedActionFacts
) -> CommentItemManifest:
    body = observation.body
    mask = _protected_characters(body)
    byte_offsets = [0]
    for c in body:
        byte_offsets.append(byte_offsets[-1] + len(c.encode("utf-8")))
    units = []
    start = 0
    for index in range(len(body) + 1):
        if index < len(body) and (body[index] not in "\n;" or mask[index]):
            continue
        left, right = start, index
        while left < right and body[left] in " \t\r\n":
            left += 1
        while right > left and body[right - 1] in " \t\r\n":
            right -= 1
        if left < right:
            units.append((left, right))
        start = index + 1
    if not units or len(units) > MAX_ITEMS:
        raise ReviewInputError("comment item count is invalid")
    result = []
    total_refs = 0
    for left, right in units:
        text = body[left:right]
        span = (byte_offsets[left], byte_offsets[right])
        refs = tuple(
            (kind, literal, start + span[0], end + span[0])
            for kind, literal, start, end in _references(text)
        )
        total_refs += len(refs)
        protected = any(mask[left:right])
        rejected = _clause_refusal(text, labelled=True)
        reason = "quoted-item" if protected else rejected[1] if rejected else None
        if not protected and _requires_explicit_command(text):
            reason = "explicit-command-required"
        if (
            not protected
            and reason != "explicit-command-required"
            and len({r[1].upper() for r in refs if r[0] == "finding"}) > 1
        ):
            reason = "multiple-item-targets"
        # Quoted fragments cannot hide surrounding restrictions or contribute
        # targets. This metadata is derived before any model interpretation.
        unquoted = "".join(" " if mask[i] else body[i] for i in range(left, right))
        qualifiers = _qualifier_actions(unquoted)
        qualifier_targets = (
            tuple(
                dict.fromkeys(
                    literal.upper()
                    for kind, literal, _, _ in _references(unquoted)
                    if kind == "finding"
                )
            )
            if qualifiers
            else ()
        )
        broad = bool(qualifiers) and _broad_qualifier(unquoted)
        if broad:
            reason = "cross-item-qualification"
        result.append(
            CommentItem(
                digest(
                    {
                        "source": observation.fingerprint(),
                        "segmentation": SEGMENTATION,
                        "span": span,
                    }
                ),
                span,
                refs,
                reason,
                qualifiers,
                qualifier_targets,
                broad,
            )
        )
    if total_refs > 10:
        raise ReviewInputError("comment reference count exceeds limit")
    return CommentItemManifest(
        observation.fingerprint(), facts.fingerprint(), tuple(result)
    )


def prepare_intent_batch(
    observation: CommentObservation, facts: ResolvedActionFacts
) -> PreparedCommentBatch:
    refused = current_source_refusal(observation, facts)
    if refused:
        return PreparedCommentBatch(terminal=refused)
    if not observation.body.strip():
        return PreparedCommentBatch(
            terminal=proposal(observation, facts, "no_action", "no-comment-items")
        )
    if not facts.extraction_admitted:
        return PreparedCommentBatch(
            terminal=proposal(observation, facts, "refused", "extraction-not-admitted")
        )
    try:
        manifest = build_item_manifest(observation, facts)
    except ReviewInputError as exc:
        reason = (
            "reference-count-limit" if "reference" in str(exc) else "item-count-limit"
        )
        return PreparedCommentBatch(
            terminal=proposal(observation, facts, "refused", reason)
        )
    text = canonical(
        {
            "comment": observation.body,
            "manifest_sha256": manifest.digest(),
            "segmentation": manifest.segmentation,
            "items": [
                {
                    "index": i,
                    "span": item.span,
                    "references": item.references,
                    "static": item.static_reason,
                }
                for i, item in enumerate(manifest.items)
            ],
        }
    )
    if utf8_size(text) > 4096:
        return PreparedCommentBatch(
            manifest=manifest,
            terminal=proposal(
                observation, facts, "refused", "decision-input-byte-limit"
            ),
        )
    requirements: list[list[DecisionQuestion]] = []
    for index, item in enumerate(manifest.items):
        if item.static_reason:
            continue
        own = [
            DecisionQuestion(f"i{index}_intent", INTENTS),
            DecisionQuestion(f"i{index}_stance", STANCES),
            DecisionQuestion(f"i{index}_polarity", POLARITIES),
        ]
        own.extend(
            DecisionQuestion(f"i{index}_reference_{i}", ("use", "ignore", "uncertain"))
            for i in range(len(item.references))
        )
        requirements.append(own)
    groups: list[list[DecisionQuestion]] = [[], [], []]

    def pack(index: int) -> bool:
        if index == len(requirements):
            return True
        own = requirements[index]
        seen = set()
        for group in groups:
            if len(group) in seen or len(group) + len(own) > 16:
                continue
            seen.add(len(group))
            group.extend(own)
            if pack(index + 1):
                return True
            del group[-len(own) :]
        return False

    if sum(map(len, requirements)) > MAX_QUESTIONS or not pack(0):
        return PreparedCommentBatch(
            manifest=manifest,
            terminal=proposal(
                observation, facts, "refused", "decision-question-capacity"
            ),
        )
    requests = tuple(
        DecisionRequest(
            observation.fingerprint(),
            facts.fingerprint(),
            facts.provider,
            facts.model,
            text,
            tuple(group),
            rubric="comment-intent-items-v1",
        )
        for group in groups
        if group
    )
    return PreparedCommentBatch(manifest, requests)


def _items_from_groups(
    prepared: PreparedCommentBatch, groups: tuple[GroupObservation, ...]
) -> tuple[ItemExtraction, ...]:
    assert prepared.manifest is not None
    result = []
    for index, item in enumerate(prepared.manifest.items):
        if item.static_reason:
            result.append(ItemExtraction(item.item_id, "static"))
            continue
        name = f"i{index}_intent"
        matches = [
            (r, g)
            for r, g in zip(prepared.requests, groups)
            if name in {q.name for q in r.questions}
        ]
        if len(matches) != 1:
            raise ReviewInputError("comment question ownership is invalid")
        request, group = matches[0]
        answers = (
            ()
            if group.response is None
            else tuple(
                (key.removeprefix(f"i{index}_"), value)
                for key, value in group.response.answers
                if key.startswith(f"i{index}_")
            )
        )
        result.append(ItemExtraction(item.item_id, group.status, answers))
    return tuple(result)


def normalize_decision_groups(
    prepared: PreparedCommentBatch, results: Mapping[str, DecisionResponse | str | None]
) -> ValidatedBatchExtraction:
    """Validate each complete native group; a missing group is not no_action.

    No retry/refund or claim about dispatch is made. Unknown usage stays unknown.
    """
    if prepared.terminal or prepared.manifest is None:
        raise ReviewInputError("comment batch has no extraction requests")
    expected = {r.digest() for r in prepared.requests}
    if not isinstance(results, Mapping) or any(k not in expected for k in results):
        raise ReviewInputError("unknown comment response group")
    groups = []
    for request in prepared.requests:
        response = results.get(request.digest())
        if response is None:
            groups.append(GroupObservation(request.digest(), "pending", None))
            continue
        try:
            if isinstance(response, DecisionResponse):
                response = canonical(response.to_dict())
            validated = parse_decision_response(response, request)
        except (ReviewInputError, TypeError):
            groups.append(GroupObservation(request.digest(), "invalid", None))
            continue
        groups.append(GroupObservation(request.digest(), "accepted", validated))
    frozen = tuple(groups)
    return ValidatedBatchExtraction(
        prepared.digest(), frozen, _items_from_groups(prepared, frozen)
    )


def _validate_extraction(
    prepared: PreparedCommentBatch, extraction: ValidatedBatchExtraction
) -> ValidatedBatchExtraction:
    if (
        not isinstance(extraction, ValidatedBatchExtraction)
        or extraction.preparation_sha256 != prepared.digest()
    ):
        raise ReviewInputError("comment extraction identity conflicts")
    group_map = {}
    for group in extraction.groups:
        if (
            not isinstance(group, GroupObservation)
            or group.request_sha256 in group_map
            or group.status not in {"accepted", "invalid", "pending"}
        ):
            raise ReviewInputError("comment response group is invalid")
        group_map[group.request_sha256] = group
    if set(group_map) != {r.digest() for r in prepared.requests}:
        raise ReviewInputError("comment response group inventory conflicts")
    groups = []
    for request in prepared.requests:
        group = group_map[request.digest()]
        if group.status == "accepted":
            if not isinstance(group.response, DecisionResponse):
                raise ReviewInputError("comment group completion is missing")
            validated = parse_decision_response(
                canonical(group.response.to_dict()), request
            )
            group = GroupObservation(request.digest(), "accepted", validated)
        elif group.response is not None:
            raise ReviewInputError("unaccepted comment response has answers")
        groups.append(group)
    expected = _items_from_groups(prepared, tuple(groups))
    by_id = {}
    for item in extraction.items:
        if not isinstance(item, ItemExtraction) or item.item_id in by_id:
            raise ReviewInputError("comment extraction item identity is invalid")
        by_id[item.item_id] = item
    if by_id != {item.item_id: item for item in expected}:
        raise ReviewInputError("comment extraction item inventory conflicts")
    return ValidatedBatchExtraction(prepared.digest(), tuple(groups), expected)


def plan_comment_batch(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    prepared: PreparedCommentBatch,
    extraction: ValidatedBatchExtraction | None = None,
) -> CommentBatchProposal:
    """Plan independently attributable items without creating any execution grant."""
    current = prepare_intent_batch(observation, facts)
    if current != prepared:
        return terminal_batch(
            observation,
            facts,
            proposal(
                observation, facts, "refused", "comment-manifest-or-request-changed"
            ),
        )
    if current.terminal:
        return terminal_batch(observation, facts, current.terminal)
    if extraction is None:
        raise ReviewInputError("comment extraction is missing")
    validated = _validate_extraction(prepared, extraction)
    assert prepared.manifest is not None
    output = []
    for item, selected in zip(prepared.manifest.items, validated.items):
        if item.static_reason:
            state = (
                "evidence_only" if item.static_reason == "quoted-item" else "clarify"
            )
            planned = proposal(observation, facts, state, item.static_reason)
        elif selected.status != "accepted":
            planned = proposal(
                observation, facts, "refused", "extraction-" + selected.status
            )
        else:
            body = observation.body.encode()[item.span[0] : item.span[1]].decode()
            planned = _plan_clause_action(
                observation,
                facts,
                body=body,
                source_span=item.span,
                answers=dict(selected.answers),
                decision_sha256=digest(
                    {
                        "item": item.item_id,
                        "answers": dict(selected.answers),
                        "preparation": prepared.digest(),
                    }
                ),
            )
            if planned.action == "assess_claim":
                planned = replace(
                    planned,
                    requirements=(*planned.requirements, "all-related-comment-items"),
                )
        output.append(
            CommentItemProposal(item.item_id, item.span, selected.status, planned)
        )
    # Cross-item qualifications are source context, not an independent array
    # element that can be dropped while executing the apparently positive item.
    for index, result in enumerate(output):
        if not result.proposal.action:
            continue
        interpretation = dict(validated.items[index].answers)
        for item in prepared.manifest.items:
            if not (
                "*" in item.qualifier_actions
                or interpretation.get("intent") in item.qualifier_actions
            ):
                continue
            restricted = {
                finding.instance
                for finding in facts.findings
                if finding.reference.upper() in item.qualifier_targets
            }
            if (
                not item.qualifier_broad
                and item.qualifier_targets
                and result.proposal.targets
                and not (restricted & set(result.proposal.targets))
            ):
                continue
            output[index] = replace(
                result,
                proposal=replace(
                    result.proposal,
                    status="clarify",
                    action=None,
                    reason="cross-item-qualification",
                ),
            )
            break
    # A request not to assess a particular concern conflicts with a sibling
    # affirmative request for that same effect. Negative fix evidence does not.
    for index, result in enumerate(tuple(output)):
        if result.proposal.reason != "negated-or-conditional-request":
            continue
        intent = dict(validated.items[index].answers).get("intent")
        if not result.proposal.targets or intent in {None, "none"}:
            continue
        for other, positive in enumerate(tuple(output)):
            if (
                other != index
                and positive.proposal.action
                and dict(validated.items[other].answers).get("intent") == intent
                and set(positive.proposal.targets) & set(result.proposal.targets)
            ):
                output[other] = replace(
                    positive,
                    proposal=replace(
                        positive.proposal,
                        status="clarify",
                        action=None,
                        reason="cross-item-qualification",
                    ),
                )
    complete = all(g.status == "accepted" for g in validated.groups)
    return CommentBatchProposal(
        observation.fingerprint(),
        facts.fingerprint(),
        tuple(output),
        complete,
        "planned" if complete else "extraction_pending",
    )
