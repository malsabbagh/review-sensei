"""Pure proposal-only comment planning; identity and live facts belong to hosts.

Nothing in this module is an action grant, finding assessment, or completion
receipt. A model's choices cannot widen a thread or change the execution head.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import TypedDict

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

_SHA = re.compile(r"[a-f0-9]{40}")
_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_-])RS-[A-Fa-f0-9]{6}(?![A-Za-z0-9_-])|(?<![A-Za-z0-9_-])([a-fA-F0-9]{7,40})(?![A-Za-z0-9_-])"
)
INTENTS = (
    "none",
    "status",
    "recheck_approval",
    "resume_review",
    "fresh_review",
    "remaining_review",
    "assess_claim",
    "risk_disposition",
    "approve",
    "stop",
    "unknown",
)
STANCES = ("fixed", "not_fixed", "disputed", "risk_accepted", "context", "unknown")
POLARITIES = ("affirmative", "negated", "conditional", "uncertain")


class _Selection(TypedDict):
    targets: tuple[str, ...]
    commits: tuple[str, ...]
    decision_sha256: str
    claim: AttributedClaim | None


def _id(value: object) -> bool:
    return type(value) is int and 1 <= value <= 9_007_199_254_740_991


@dataclass(frozen=True)
class AnalysisScope:
    repository_id: int
    pull_request: int
    base_sha: str
    head_sha: str
    diff_sha256: str
    inventory_sha256: str
    context_policy_sha256: str

    def __post_init__(self) -> None:
        if (
            not _id(self.repository_id)
            or not _id(self.pull_request)
            or any(
                not isinstance(v, str) or _SHA.fullmatch(v) is None
                for v in (self.base_sha, self.head_sha)
            )
            or not all(
                sha256(v)
                for v in (
                    self.diff_sha256,
                    self.inventory_sha256,
                    self.context_policy_sha256,
                )
            )
        ):
            raise ReviewInputError("comment analysis scope is invalid")


@dataclass(frozen=True)
class CommentObservation:
    scope: AnalysisScope
    surface: str
    source_id: int
    revision: str
    actor_id: int
    actor_login: str
    actor_type: str
    body: str
    root_id: int | None = None
    root_instance: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, AnalysisScope)
            or self.surface not in {"issue_comment", "review_comment"}
            or not _id(self.source_id)
            or not _id(self.actor_id)
            or any(
                not isinstance(v, str) or not 1 <= len(v) <= 128
                for v in (self.revision, self.actor_login)
            )
            or self.actor_type not in {"User", "Bot"}
            or not isinstance(self.body, str)
        ):
            raise ReviewInputError("comment observation is invalid")
        if self.surface == "issue_comment":
            valid = self.root_id is None and self.root_instance is None
        else:
            valid = _id(self.root_id) and sha256(self.root_instance)
        if not valid:
            raise ReviewInputError("comment thread identity is invalid")
        if utf8_size(self.body, label="original comment") > 4096:
            raise ReviewInputError("original comment exceeds byte limit")

    def fingerprint(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class FindingTarget:
    reference: str
    instance: str
    criterion_sha256: str
    head_sha: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reference, str)
            or re.fullmatch(r"RS-[A-F0-9]{6}", self.reference) is None
            or not sha256(self.instance)
            or not self.instance.upper().startswith(self.reference[3:])
            or not sha256(self.criterion_sha256)
            or not isinstance(self.head_sha, str)
            or _SHA.fullmatch(self.head_sha) is None
        ):
            raise ReviewInputError("comment finding identity is invalid")


@dataclass(frozen=True)
class CommitReference:
    literal: str
    sha: str
    in_pr: bool
    ancestor_of_head: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.literal, str)
            or re.fullmatch(r"[a-f0-9]{7,40}", self.literal) is None
            or not isinstance(self.sha, str)
            or _SHA.fullmatch(self.sha) is None
            or not self.sha.startswith(self.literal)
            or type(self.in_pr) is not bool
            or type(self.ancestor_of_head) is not bool
        ):
            raise ReviewInputError("comment commit reference is invalid")


@dataclass(frozen=True)
class ResolvedActionFacts:
    """Trusted adapter snapshot, never deserialized from provider output."""

    source_fingerprint: str
    scope: AnalysisScope
    actor_permission: str
    extraction_admitted: bool
    invocation_authorized: bool
    pr_open: bool
    inventory_complete: bool
    session_terminal: bool
    provider: str
    model: str
    findings: tuple[FindingTarget, ...] = ()
    commits: tuple[CommitReference, ...] = ()

    def __post_init__(self) -> None:
        if (
            not sha256(self.source_fingerprint)
            or not isinstance(self.scope, AnalysisScope)
            or self.actor_permission
            not in {"admin", "maintain", "write", "read", "none"}
            or any(
                type(v) is not bool
                for v in (
                    self.extraction_admitted,
                    self.invocation_authorized,
                    self.pr_open,
                    self.inventory_complete,
                    self.session_terminal,
                )
            )
            or any(
                not isinstance(v, str) or not 1 <= len(v) <= 128
                for v in (self.provider, self.model)
            )
            or type(self.findings) is not tuple
            or len(self.findings) > 200
            or any(not isinstance(v, FindingTarget) for v in self.findings)
            or type(self.commits) is not tuple
            or len(self.commits) > 20
            or any(not isinstance(v, CommitReference) for v in self.commits)
        ):
            raise ReviewInputError("resolved comment facts are invalid")

    def fingerprint(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class AttributedClaim:
    """One provisional clause stance, bound to the original source bytes."""

    kind: str
    polarity: str
    source_span_utf8: tuple[int, int]

    def __post_init__(self) -> None:
        if (
            self.kind not in STANCES
            or self.polarity not in POLARITIES
            or type(self.source_span_utf8) is not tuple
            or len(self.source_span_utf8) != 2
            or any(type(v) is not int for v in self.source_span_utf8)
            or not 0 <= self.source_span_utf8[0] < self.source_span_utf8[1] <= 4096
        ):
            raise ReviewInputError("attributed comment claim is invalid")


@dataclass(frozen=True)
class EvidenceRequirement:
    """A pending input/criterion binding, never an examined-evidence receipt."""

    kind: str
    identity: str
    binding_sha256: str

    def __post_init__(self) -> None:
        if (
            self.kind
            not in {
                "pr-diff",
                "pr-inventory",
                "context-policy",
                "criterion",
                "claimed-change",
            }
            or not isinstance(self.identity, str)
            or not 1 <= len(self.identity) <= 128
            or not sha256(self.binding_sha256)
        ):
            raise ReviewInputError("comment evidence requirement is invalid")


@dataclass(frozen=True)
class ActionProposal:
    status: str
    action: str | None
    reason: str
    scope: AnalysisScope
    source_sha256: str
    state_sha256: str
    targets: tuple[str, ...] = ()
    commits: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    decision_sha256: str | None = None
    evidence: tuple[EvidenceRequirement, ...] = ()
    claim: AttributedClaim | None = None

    @property
    def executable(self) -> bool:
        return False

    def digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class PreparedComment:
    request: DecisionRequest | None = None
    proposal: ActionProposal | None = None


def proposal(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    status: str,
    reason: str,
    action: str | None = None,
    **kwargs,
) -> ActionProposal:
    return ActionProposal(
        status,
        action,
        reason,
        observation.scope,
        observation.fingerprint(),
        facts.fingerprint(),
        **kwargs,
    )


def current_source_refusal(
    observation: CommentObservation, facts: ResolvedActionFacts
) -> ActionProposal | None:
    if (
        observation.scope != facts.scope
        or observation.fingerprint() != facts.source_fingerprint
    ):
        return proposal(
            observation, facts, "refused", "source-or-current-snapshot-changed"
        )
    if not facts.pr_open:
        return proposal(observation, facts, "refused", "pull-request-closed")
    return None


def _references(body: str) -> tuple[tuple[str, str, int, int], ...]:
    refs = []
    for match in _REFERENCE.finditer(body):
        start, end = match.span(1) if match.group(1) else match.span()
        literal = body[start:end]
        refs.append(
            (
                "commit" if match.group(1) else "finding",
                literal,
                len(body[:start].encode("utf-8")),
                len(body[:end].encode("utf-8")),
            )
        )
    return tuple(refs)


def prepare_intent_request(
    observation: CommentObservation, facts: ResolvedActionFacts
) -> PreparedComment:
    refusal = current_source_refusal(observation, facts)
    if refusal:
        return PreparedComment(proposal=refusal)
    body = observation.body
    if not body or len(body.encode("utf-8")) > 4096:
        return PreparedComment(
            proposal=proposal(observation, facts, "refused", "comment-byte-limit")
        )
    if not facts.extraction_admitted:
        return PreparedComment(
            proposal=proposal(observation, facts, "refused", "extraction-not-admitted")
        )
    # First slice intentionally abstains on quotation/multiple clauses. It does
    # not strip text into a different source or invent arbitrary clause spans.
    if (
        any(
            c in body
            for c in (
                "`",
                '"',
                "\n",
                "\r",
                ";",
                ",",
                ":",
                "–",
                "—",
                "“",
                "”",
                "«",
                "»",
                "‘",
                "’",
                "<",
            )
        )
        or body.lstrip().startswith(">")
        or body.startswith(("    ", "\t"))
        or re.search(
            r"(?<!\w)'[^']+'(?!\w)|[.!?]\s+\S|\b(?:and|or|but|however|except|yet|while|although|whereas)\b"
            r"|[ \t]-[ \t]",
            body,
            re.I,
        )
    ):
        return PreparedComment(
            proposal=proposal(
                observation, facts, "clarify", "quoted-or-multiclause-input"
            )
        )
    if any(
        ord(c) > 127 and unicodedata.category(c)[0] in {"L", "N", "C"} for c in body
    ):
        return PreparedComment(
            proposal=proposal(
                observation, facts, "clarify", "unsupported-language-or-reference"
            )
        )
    if any(
        re.fullmatch(r"RS-[a-fA-F0-9]{6}", m.group()) is None
        for m in re.finditer(
            r"\bRS(?:[-\u2010-\u2015][^\s,.:!?]+|[A-Fa-f0-9]{6,64}\b|[A-Za-z0-9_]*[0-9][A-Za-z0-9_]*\b)",
            body,
            re.I,
        )
    ):
        return PreparedComment(
            proposal=proposal(
                observation, facts, "clarify", "unsupported-finding-reference"
            )
        )
    # An explicitly named claimed change must not disappear just because its
    # token cannot be represented by the supported native candidate grammar.
    # This is a bounded lexical marker, not arbitrary prose reference inference.
    if any(
        re.fullmatch(r"[A-Fa-f0-9]{7,40}[.!?)]*", match.group(1)) is None
        for match in re.finditer(r"\bcommit\b[ \t]*(\S*)", body, re.I)
    ):
        return PreparedComment(
            proposal=proposal(
                observation, facts, "clarify", "unsupported-commit-reference"
            )
        )
    refs = _references(body)
    if len(refs) > 10:
        return PreparedComment(
            proposal=proposal(observation, facts, "refused", "reference-count-limit")
        )
    questions = (
        DecisionQuestion("intent", INTENTS),
        DecisionQuestion("stance", STANCES),
        DecisionQuestion("polarity", POLARITIES),
        *(
            DecisionQuestion("reference_" + str(i), ("use", "ignore", "uncertain"))
            for i in range(len(refs))
        ),
    )
    # Host-extracted candidates, including exact original UTF-8 spans, are data.
    # No permission, credentials or code is supplied to the classifier.
    text = canonical({"comment": body, "references": refs})
    if len(text.encode("utf-8")) > 4096:
        return PreparedComment(
            proposal=proposal(
                observation, facts, "refused", "decision-input-byte-limit"
            )
        )
    return PreparedComment(
        request=DecisionRequest(
            observation.fingerprint(),
            facts.fingerprint(),
            facts.provider,
            facts.model,
            text,
            questions,
        )
    )


def plan_comment_action(
    observation: CommentObservation,
    facts: ResolvedActionFacts,
    request: DecisionRequest,
    response: DecisionResponse,
) -> ActionProposal:
    """Bind choices to current facts. Always return a non-executable proposal."""
    prepared = prepare_intent_request(observation, facts)
    if prepared.proposal:
        return prepared.proposal
    if prepared.request != request:
        return proposal(observation, facts, "refused", "decision-request-changed")
    # Revalidate even a directly constructed response. Duplicate tuple answers
    # must not be lost by its dict projection.
    if len(dict(response.answers)) != len(response.answers):
        raise ReviewInputError("duplicate decision answer")
    validated = parse_decision_response(canonical(response.to_dict()), request)
    answers = dict(validated.answers)
    intent, stance, polarity = (answers[k] for k in ("intent", "stance", "polarity"))
    refs = _references(observation.body)
    targets: list[str] = []
    commits: list[str] = []
    for i, (kind, literal, _, _) in enumerate(refs):
        selected = answers["reference_" + str(i)]
        # Ignoring an explicit reference cannot narrow away contrary evidence.
        if selected != "use":
            return proposal(
                observation, facts, "clarify", "reference-attribution-uncertain"
            )
        if kind == "finding":
            matches = [
                f
                for f in facts.findings
                if f.reference == literal.upper() and f.head_sha == facts.scope.head_sha
            ]
            if not facts.inventory_complete or len(matches) != 1:
                return proposal(
                    observation, facts, "clarify", "finding-identity-unresolved"
                )
            if sum(f.instance == matches[0].instance for f in facts.findings) != 1:
                return proposal(
                    observation, facts, "clarify", "finding-identity-unresolved"
                )
            targets.append(matches[0].instance)
        else:
            matches_c = [c for c in facts.commits if c.literal == literal.lower()]
            if (
                len(matches_c) != 1
                or not matches_c[0].in_pr
                or not matches_c[0].ancestor_of_head
            ):
                return proposal(
                    observation, facts, "clarify", "commit-ancestry-unresolved"
                )
            commits.append(matches_c[0].sha)
    if observation.surface == "review_comment":
        root = [
            f
            for f in facts.findings
            if f.instance == observation.root_instance
            and f.head_sha == facts.scope.head_sha
        ]
        if (
            not facts.inventory_complete
            or len(root) != 1
            or any(t != observation.root_instance for t in targets)
        ):
            return proposal(
                observation, facts, "clarify", "inline-root-identity-conflicts"
            )
        targets = [root[0].instance]
    fields: _Selection = dict(
        targets=tuple(dict.fromkeys(targets)),
        commits=tuple(dict.fromkeys(commits)),
        decision_sha256=digest(validated.to_dict()),
        claim=AttributedClaim(stance, polarity, (0, utf8_size(observation.body)))
        if stance in {"fixed", "not_fixed", "disputed", "risk_accepted"}
        else None,
    )
    if (
        intent == "unknown"
        or stance == "unknown"
        or polarity in {"conditional", "uncertain"}
    ):
        return proposal(observation, facts, "clarify", "semantic-abstention", **fields)
    # A single-clause claim cannot lose its assessment obligations because a
    # provider independently selected a different action. Richer mixed requests
    # require a later extraction contract with separate clause attribution.
    if stance in {"fixed", "not_fixed", "disputed"} and intent not in {
        "none",
        "assess_claim",
    }:
        return proposal(
            observation, facts, "clarify", "claim-intent-conflicts", **fields
        )
    request_guard = re.search(
        r"\b(?:if|unless|maybe|perhaps|possibly)\b"
        r"|\b(?:do[ \t]+not|don't|never|cannot|can't|not[ \t]+to|no[ \t]+need[ \t]+to)"
        r"[ \t]+(?:assess|review|verify|rescan|recheck|resume|continue|approve|override)\b",
        observation.body,
        re.I,
    )
    # Negative fix evidence ('did not fix it') is distinct from a request not
    # to assess it and remains eligible for an assessment proposal.
    if (
        polarity == "negated"
        or request_guard
        or (
            intent not in {"none", "assess_claim"}
            and re.search(
                r"\b(?:not|never|don't|cannot|can't|unless|if|maybe)\b",
                observation.body,
                re.I,
            )
        )
    ):
        return proposal(
            observation,
            facts,
            "evidence_only",
            "negated-or-conditional-request",
            **fields,
        )
    if stance == "fixed" and re.search(
        r"\b(?:not|isn't|wasn't|didn't|never)\b", observation.body, re.I
    ):
        return proposal(
            observation, facts, "clarify", "claim-polarity-conflicts", **fields
        )
    if intent in {"risk_disposition", "stop"} or stance == "risk_accepted":
        return proposal(
            observation, facts, "clarify", "explicit-command-required", **fields
        )
    if intent == "none":
        return proposal(
            observation,
            facts,
            "evidence_only" if targets or commits else "no_action",
            "no-request",
            **fields,
        )
    if (
        observation.actor_type != "User"
        or facts.actor_permission not in {"maintain", "admin"}
        or not facts.invocation_authorized
    ):
        return proposal(
            observation, facts, "evidence_only", "no-source-action-authority", **fields
        )
    if not facts.session_terminal:
        return proposal(
            observation, facts, "refused", "historical-accounting-unconfirmed", **fields
        )
    if not facts.inventory_complete:
        return proposal(
            observation, facts, "refused", "current-inventory-incomplete", **fields
        )
    if intent == "assess_claim":
        if not targets:
            return proposal(
                observation, facts, "clarify", "claim-target-unresolved", **fields
            )
        evidence = (
            EvidenceRequirement(
                "pr-diff", facts.scope.head_sha, facts.scope.diff_sha256
            ),
            EvidenceRequirement(
                "pr-inventory", facts.scope.head_sha, facts.scope.inventory_sha256
            ),
            EvidenceRequirement(
                "context-policy",
                facts.scope.head_sha,
                facts.scope.context_policy_sha256,
            ),
            *(
                EvidenceRequirement("criterion", f.instance, f.criterion_sha256)
                for f in facts.findings
                if f.instance in targets
            ),
            *(
                EvidenceRequirement(
                    "claimed-change",
                    sha,
                    digest({"commit": sha, "head": facts.scope.head_sha}),
                )
                for sha in dict.fromkeys(commits)
            ),
        )
        return proposal(
            observation,
            facts,
            "proposed",
            "assessment-contract-required",
            "assess_claim",
            requirements=(
                "whole-pr-current-evidence",
                "independent-criterion-assessment",
                "cross-file-context",
                "claimed-delta-and-later-changes",
                "separate-coverage-and-approval-gates",
            ),
            evidence=evidence,
            **fields,
        )
    action = {
        "status": "status",
        "approve": "status",
        "recheck_approval": "verify",
        "resume_review": "continue",
        "fresh_review": "rescan",
        "remaining_review": "remaining",
    }[intent]
    return proposal(
        observation,
        facts,
        "proposed",
        "semantic-action-protocol-inactive",
        action,
        requirements=(
            "original-source-action-admission",
            "current-head-evidence",
            "existing-command-preconditions",
            "accounting-and-finalizer-gates",
        ),
        **fields,
    )
