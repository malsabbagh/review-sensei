"""Deterministic candidate-finding evidence checks.

This pass never asks a model to adjudicate itself.  It verifies that bounded
evidence references exist in the exact snapshot reviewed and that candidates
are actionable before a publisher is allowed to consider them.

Snapshot bounds intentionally reuse ``ReviewLimits`` diff ceilings
(``max_diff_files`` and ``max_diff_bytes``) so verification stays aligned with
the same bounded reviewed-input profile used to admit review inputs.  The
snapshot is the reviewed source mapping passed into verification, not an
unbounded repository checkout.  Embedders may tighten limits by passing a lower
``ReviewLimits`` profile to the verifier helpers.

Public documents in this module use ``schema_version: "1.0"``, matching the
existing v1 schema family identified by ``$id`` URLs under ``/schemas/v1/``.
Evidence excerpts are single-line substrings matched against one reviewed line.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Mapping, Sequence

from .convergence import (
    REQUIRED_CONTRACT_KINDS,
    BlockerCandidate,
    ReviewConvergencePolicy,
    admit_review_result,
    comment_targets_pr_change,
    derive_blocker_candidate,
)
from .errors import ReviewInputError
from .models import ReviewComment, ReviewResult
from .presentation import escape_markdown_label
from .schemas import validate_public_document
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    ReviewLimits,
    utf8_size,
    validate_repository_path,
)

if TYPE_CHECKING:
    from .baseline import ReviewBaseline
    from .context import ReviewContextCacheKey

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MENTION_PREFIX = re.compile(r"(^|\s)@")
DISPOSITIONS = frozenset({"confirmed", "rejected", "insufficient-evidence"})
EVIDENCE_POLICIES = frozenset({"legacy", "confirmed"})
CANDIDATE_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_EVIDENCE_FIELDS = frozenset({"path", "line", "snapshot_sha256", "excerpt"})
_CANDIDATE_FIELDS = frozenset(
    {
        "schema_version",
        "claim",
        "triggering_conditions",
        "impacted_path",
        "evidence",
        "severity_rationale",
        "assumptions",
        "severity",
        "remedy",
        "required_contract",
        "named_mandatory_rule",
    }
)
PUBLIC_SCHEMA_VERSION = "1.0"
_COVERAGE_NOTE = (
    "Unpublished candidates are not findings. Incomplete verification is not "
    "a clean review."
)


@dataclass(frozen=True)
class EvidenceReference:
    """One bounded line reference into the reviewed snapshot.

    ``excerpt``, when present, must be a single-line string at construction time.
    Whether it is a substring of the referenced snapshot line is validated only
    during ``verify_candidate`` / ``verify_candidates``, not here.
    """

    path: str
    line: int
    snapshot_sha256: str
    excerpt: str | None = None

    def __post_init__(self) -> None:
        validate_repository_path(
            self.path, label="evidence path", allow_glob_chars=True
        )
        if (
            isinstance(self.line, bool)
            or not isinstance(self.line, int)
            or self.line < 1
        ):
            raise ReviewInputError("evidence line must be positive")
        if not isinstance(self.snapshot_sha256, str) or not _SHA256.fullmatch(
            self.snapshot_sha256
        ):
            raise ReviewInputError("evidence snapshot_sha256 must be a SHA-256 digest")
        if self.excerpt is not None and (
            not isinstance(self.excerpt, str)
            or not self.excerpt
            or len(self.excerpt) > 512
            or "\n" in self.excerpt
            or "\r" in self.excerpt
        ):
            raise ReviewInputError("evidence excerpt must be a single line")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "snapshot_sha256": self.snapshot_sha256,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class CandidateFinding:
    claim: str
    triggering_conditions: str
    impacted_path: str
    evidence: tuple[EvidenceReference, ...]
    severity_rationale: str
    assumptions: tuple[str, ...] = ()
    severity: str | None = None
    remedy: str | None = None
    required_contract: str | None = None
    named_mandatory_rule: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "claim",
            "triggering_conditions",
            "impacted_path",
            "severity_rationale",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ReviewInputError(
                    f"candidate {name} must be a bounded non-empty string"
                )
        validate_repository_path(
            self.impacted_path, label="candidate impacted_path", allow_glob_chars=True
        )
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(reference, EvidenceReference) for reference in self.evidence
        ):
            raise ReviewInputError(
                "candidate evidence must be EvidenceReference values"
            )
        if not self.evidence:
            raise ReviewInputError("candidate must include evidence")
        if len(self.evidence) > 8:
            raise ReviewInputError("candidate has too many evidence references")
        if not isinstance(self.assumptions, tuple) or any(
            not isinstance(assumption, str)
            or not assumption.strip()
            or len(assumption) > 1024
            for assumption in self.assumptions
        ):
            raise ReviewInputError(
                "candidate assumptions must be bounded non-empty strings"
            )
        if len(self.assumptions) > 16:
            raise ReviewInputError("candidate has too many assumptions")
        if self.severity is not None and (
            not isinstance(self.severity, str)
            or self.severity not in CANDIDATE_SEVERITIES
        ):
            raise ReviewInputError("candidate severity is unsupported")
        if self.remedy is not None and (
            not isinstance(self.remedy, str)
            or not self.remedy.strip()
            or len(self.remedy) > 1024
        ):
            raise ReviewInputError(
                "candidate remedy must be a bounded non-empty string"
            )
        if self.required_contract is not None and (
            not isinstance(self.required_contract, str)
            or self.required_contract not in REQUIRED_CONTRACT_KINDS
        ):
            raise ReviewInputError("candidate required_contract is unsupported")
        if self.named_mandatory_rule is not None and (
            not isinstance(self.named_mandatory_rule, str)
            or not self.named_mandatory_rule.strip()
            or len(self.named_mandatory_rule) > 256
        ):
            raise ReviewInputError(
                "candidate named_mandatory_rule must be a bounded non-empty string"
            )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "claim": self.claim,
            "triggering_conditions": self.triggering_conditions,
            "impacted_path": self.impacted_path,
            "evidence": [reference.to_dict() for reference in self.evidence],
            "severity_rationale": self.severity_rationale,
        }
        if self.assumptions:
            value["assumptions"] = list(self.assumptions)
        if self.severity is not None:
            value["severity"] = self.severity
        if self.remedy is not None:
            value["remedy"] = self.remedy
        if self.required_contract is not None:
            value["required_contract"] = self.required_contract
        if self.named_mandatory_rule is not None:
            value["named_mandatory_rule"] = self.named_mandatory_rule
        validate_public_document(value, "candidate-finding")
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CandidateFinding":
        if not isinstance(value, Mapping):
            raise ReviewInputError("candidate must be an object")
        try:
            unknown_top = set(value) - _CANDIDATE_FIELDS
            if unknown_top:
                raise TypeError("candidate contains unknown fields")
            required_fields = (
                "claim",
                "triggering_conditions",
                "impacted_path",
                "severity_rationale",
            )
            for field_name in required_fields:
                if field_name not in value:
                    raise KeyError(field_name)
                if not isinstance(value[field_name], str):
                    raise TypeError(f"candidate {field_name} must be a string")
            if "evidence" not in value:
                raise KeyError("evidence")
            raw_evidence = value["evidence"]
            if not isinstance(raw_evidence, (list, tuple)):
                raise TypeError("evidence must be iterable")
            evidence_values: list[EvidenceReference] = []
            for item in raw_evidence:
                if not isinstance(item, Mapping):
                    raise TypeError("evidence reference must be an object")
                unknown = set(item) - _EVIDENCE_FIELDS
                if unknown:
                    raise TypeError("evidence reference contains unknown fields")
                try:
                    evidence_values.append(EvidenceReference(**item))  # type: ignore[arg-type]
                except ReviewInputError as exc:
                    raise ReviewInputError("candidate finding is malformed") from exc
            evidence = tuple(evidence_values)
            raw_assumptions = value.get("assumptions", ())
            if not isinstance(raw_assumptions, (list, tuple)):
                raise TypeError("assumptions must be iterable")
            if any(not isinstance(item, str) for item in raw_assumptions):
                raise TypeError("assumptions must contain only strings")
            assumptions = tuple(raw_assumptions)
            return cls(
                value["claim"],  # type: ignore[arg-type]
                value["triggering_conditions"],  # type: ignore[arg-type]
                value["impacted_path"],  # type: ignore[arg-type]
                evidence,
                value["severity_rationale"],  # type: ignore[arg-type]
                assumptions,
                severity=value.get("severity"),  # type: ignore[arg-type]
                remedy=value.get("remedy"),  # type: ignore[arg-type]
                required_contract=value.get("required_contract"),  # type: ignore[arg-type]
                named_mandatory_rule=value.get("named_mandatory_rule"),  # type: ignore[arg-type]
            )
        except KeyError as exc:
            field_name = exc.args[0]
            raise ReviewInputError(
                f"candidate finding is missing field '{field_name}'"
            ) from exc
        except ReviewInputError as exc:
            if str(exc) == "candidate finding is malformed":
                raise
            raise ReviewInputError("candidate finding is malformed") from exc
        except (TypeError, ValueError) as exc:
            raise ReviewInputError("candidate finding is malformed") from exc


@dataclass(frozen=True)
class VerificationResult:
    disposition: str
    reasons: tuple[str, ...]
    evidence_valid: bool
    actionable: bool
    supported_premises: tuple[str, ...] = field(default=(), compare=False)
    contrary_evidence: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if self.disposition not in DISPOSITIONS:
            raise ReviewInputError("verification disposition is unsupported")

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "disposition": self.disposition,
            "reasons": list(self.reasons),
            "evidence_valid": self.evidence_valid,
            "actionable": self.actionable,
        }
        validate_public_document(value, "verification-result")
        return value


def _check_snapshot_bounds(
    snapshot: Mapping[str, str],
    *,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> None:
    """Bound snapshot size using the public diff profile.

    ``max_diff_bytes`` applies to the aggregate UTF-8 size of all paths and
    contents.  Each individual file is also capped at the same byte ceiling as
    a fail-fast guard so no single path can exceed the public diff limit alone.
    """
    if not isinstance(snapshot, Mapping):
        raise ReviewInputError("snapshot must be a mapping")
    if len(snapshot) > limits.max_diff_files:
        raise ReviewInputError("snapshot contains too many files")
    total_bytes = 0
    for path, content in snapshot.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise ReviewInputError("snapshot paths and contents must be strings")
        validate_repository_path(path, label="snapshot path", allow_glob_chars=True)
        path_bytes = utf8_size(path, label="snapshot path")
        content_bytes = utf8_size(content, label="snapshot content")
        if path_bytes + content_bytes > limits.max_diff_bytes:
            raise ReviewInputError(
                "snapshot file exceeds the configured per-file byte limit"
            )
        total_bytes += path_bytes + content_bytes
        if total_bytes > limits.max_diff_bytes:
            raise ReviewInputError(
                "snapshot exceeds the configured aggregate byte limit"
            )


def _canonical_snapshot_bytes(snapshot: Mapping[str, str]) -> bytes:
    """Return the canonical UTF-8 bytes used for snapshot digests.

    Callers must hash this exact representation: sorted object keys,
    ``sort_keys=True``, compact separators ``(",", ":")``, and
    ``ensure_ascii=True`` (non-ASCII code points are escaped).
    """

    return json.dumps(
        dict(sorted(snapshot.items())),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _snapshot_digest(
    snapshot: Mapping[str, str],
    *,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> str:
    _check_snapshot_bounds(snapshot, limits=limits)
    return hashlib.sha256(_canonical_snapshot_bytes(snapshot)).hexdigest()


def _snapshot_lines(content: str) -> list[str]:
    """Split reviewed snapshot text on normalized Unix line boundaries."""

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


def _candidate_dedup_key(candidate: CandidateFinding) -> tuple[str, str, str]:
    evidence_payload = json.dumps(
        [reference.to_dict() for reference in candidate.evidence],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    evidence_digest = hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()
    return (candidate.impacted_path, candidate.claim, evidence_digest)


_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b")
_NULL_DEREF_KEYWORDS = frozenset(
    {
        "none",
        "null",
        "attributeerror",
        "typeerror",
        "nonetype",
        "dereference",
        "dereferences",
        "unset",
        "uninitialized",
    }
)
_DIV_ZERO_KEYWORDS = frozenset(
    {
        "zero",
        "zerodivisionerror",
        "division",
        "divide",
        "dividing",
        "modulo",
        "empty",
    }
)
_LOOKUP_KEYWORDS = frozenset(
    {
        "keyerror",
        "indexerror",
        "out of range",
        "missing key",
    }
)
_UNSAFE_OP_KEYWORDS = frozenset(
    {
        "injection",
        "shell",
        "eval",
        "exec",
        "pickle",
        "deserialization",
        "sql",
        "traversal",
        "unsanitized",
    }
)


def _enclosing_scope_preceding_lines(lines: Sequence[str], line_number: int) -> list[str]:
    """Return lines preceding ``line_number`` (1-based) within the enclosing block."""
    if line_number <= 1 or line_number > len(lines):
        return []
    target = lines[line_number - 1]
    target_indent = len(target) - len(target.lstrip(" \t"))
    collected: list[str] = []
    for idx in range(line_number - 2, -1, -1):
        raw = lines[idx]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        collected.append(stripped)
        if indent < target_indent and stripped.startswith(("def ", "async def ", "class ")):
            break
    collected.reverse()
    return collected


def _line_is_harmless_constant_or_declaration(line_text: str) -> bool:
    """Return True when a single line is a constant assignment, import, pass, or literal."""
    stripped = line_text.strip()
    if not stripped or stripped.startswith("#") or stripped in {"pass", "..."}:
        return True
    try:
        tree = ast.parse(stripped)
    except SyntaxError:
        return False
    if len(tree.body) != 1:
        return False
    stmt = tree.body[0]
    if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Pass)):
        return True
    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
        return True
    if (
        isinstance(stmt, ast.AnnAssign)
        and stmt.value is not None
        and isinstance(stmt.value, ast.Constant)
    ):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return True
    return False


def _extract_dereferenced_roots(line_text: str) -> tuple[set[str], set[str]]:
    """Return (base_names, dotted_prefixes) dereferenced via attribute/subscript on ``line_text``."""
    bases: set[str] = set()
    dotted: set[str] = set()
    stripped = line_text.strip()
    try:
        tree = ast.parse(stripped)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                parts: list[str] = [node.attr]
                curr: ast.AST = node.value
                while isinstance(curr, ast.Attribute):
                    parts.append(curr.attr)
                    curr = curr.value
                if isinstance(curr, ast.Name):
                    parts.append(curr.id)
                    chain = list(reversed(parts))
                    bases.add(chain[0])
                    for i in range(1, len(chain)):
                        dotted.add(".".join(chain[:i]))
                        bases.add(chain[i - 1])
            elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                bases.add(node.value.id)
    except SyntaxError:
        for match in re.finditer(
            r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)", stripped
        ):
            chain = match.group(1).split(".")
            bases.add(chain[0])
            for i in range(1, len(chain)):
                dotted.add(".".join(chain[:i]))
                bases.add(chain[i - 1])
    return bases, dotted


def _has_null_guard(
    preceding_lines: Sequence[str],
    target_line: str,
    *,
    bases: set[str],
    dotted: set[str],
) -> bool:
    """Return True if preceding scope or inline expression guards any dereferenced target against None."""
    candidates = bases | dotted
    if not candidates:
        return False
    inline = target_line.strip()
    for sym in candidates:
        esc = re.escape(sym)
        if re.search(
            rf"\bif\s+{esc}\s+is\s+(?:not\s+)?None\b|\b{esc}\s+and\s+{esc}\.",
            inline,
        ):
            return True
    joined = "\n".join(preceding_lines)
    for sym in candidates:
        esc = re.escape(sym)
        if re.search(
            rf"(?:\bif\b|\belif\b|\bassert\b)[^\n]*\b{esc}\s+is\s+(?:not\s+)?None\b"
            rf"|(?:\bif\b|\belif\b)\s+not\s+{esc}\b"
            rf"|\b{esc}\s*=\s*(?:[A-Za-z0-9_\"'\[\{{]|\()",
            joined,
        ):
            # If assigned to None explicitly, that is not a guard; otherwise check guard patterns
            if re.search(
                rf"(?:\bif\b|\belif\b|\bassert\b)[^\n]*\b{esc}\s+is\s+(?:not\s+)?None\b"
                rf"|(?:\bif\b|\belif\b)\s+not\s+{esc}\b",
                joined,
            ):
                return True
    return False


def _line_has_nonconstant_division(line_text: str) -> set[str]:
    """Return divisor identifiers when ``line_text`` divides or mods by a non-constant expression."""
    divisors: set[str] = set()
    stripped = line_text.strip()
    try:
        tree = ast.parse(stripped)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Div, ast.FloorDiv, ast.Mod)
            ):
                if isinstance(node.right, ast.Constant) and isinstance(
                    node.right.value, (int, float)
                ) and node.right.value != 0:
                    continue
                for sub in ast.walk(node.right):
                    if isinstance(sub, ast.Name):
                        divisors.add(sub.id)
                if not divisors:
                    divisors.add("<expr>")
    except SyntaxError:
        if any(op in stripped for op in ("/", "%")):
            for match in _IDENTIFIER_RE.finditer(stripped):
                divisors.add(match.group(0))
    return divisors


def _has_zero_or_empty_guard(
    preceding_lines: Sequence[str],
    target_line: str,
    *,
    divisors: set[str],
) -> bool:
    if not divisors:
        return False
    joined = "\n".join([*preceding_lines, target_line.strip()])
    for sym in divisors:
        if sym in {"len", "<expr>"}:
            continue
        esc = re.escape(sym)
        if re.search(
            rf"(?:\bif\b|\belif\b|\bassert\b)[^\n]*(?:\bnot\s+{esc}\b|\blen\(\s*{esc}\s*\)\s*(?:==|<=|>|!=)\s*0\b|\b{esc}\s*(?:==|<=|!=|>)\s*0\b)",
            joined,
        ):
            return True
    return False


def _assess_candidate_premises(
    candidate: CandidateFinding,
    snapshot: Mapping[str, str],
) -> tuple[tuple[str, ...], bool]:
    """Assess which defect premises are grounded in the reviewed snapshot and contracts.

    Returns ``(supported_premises, contrary_evidence)``.
    """
    premises: list[str] = []
    if _candidate_has_excerpt_evidence(candidate):
        premises.append("source-reference")

    combined_text = f"{candidate.claim} {candidate.triggering_conditions}".lower()
    words = set(re.findall(r"[a-z_][a-z0-9_]*", combined_text))

    alleges_null = bool(words & _NULL_DEREF_KEYWORDS)
    alleges_div_zero = bool(words & _DIV_ZERO_KEYWORDS)
    alleges_lookup = bool(words & _LOOKUP_KEYWORDS) or any(
        phrase in combined_text for phrase in _LOOKUP_KEYWORDS
    )
    alleges_unsafe = bool(words & _UNSAFE_OP_KEYWORDS)

    mechanism_supported = False
    contrary_evidence = False

    for reference in candidate.evidence:
        content = snapshot.get(reference.path)
        if content is None:
            continue
        lines = _snapshot_lines(content)
        if reference.line < 1 or reference.line > len(lines):
            continue
        target_line = lines[reference.line - 1]
        if _line_is_harmless_constant_or_declaration(target_line):
            continue
        preceding = _enclosing_scope_preceding_lines(lines, reference.line)

        if alleges_null:
            bases, dotted = _extract_dereferenced_roots(target_line)
            if bases or dotted:
                if _has_null_guard(
                    preceding, target_line, bases=bases, dotted=dotted
                ):
                    contrary_evidence = True
                else:
                    mechanism_supported = True

        if alleges_div_zero:
            divisors = _line_has_nonconstant_division(target_line)
            if divisors:
                if _has_zero_or_empty_guard(
                    preceding, target_line, divisors=divisors
                ):
                    contrary_evidence = True
                else:
                    mechanism_supported = True

        if alleges_lookup:
            if "[" in target_line and "]" in target_line and ".get(" not in target_line:
                mechanism_supported = True

        if alleges_unsafe:
            if any(
                token in target_line
                for token in (
                    "shell=True",
                    "eval(",
                    "exec(",
                    "pickle.loads",
                    "os.system",
                    "subprocess.",
                    "SELECT ",
                    "INSERT ",
                    "UPDATE ",
                    "DELETE ",
                )
            ):
                mechanism_supported = True

        if (
            candidate.required_contract is not None
            or candidate.named_mandatory_rule is not None
        ) and not contrary_evidence:
            mechanism_supported = True

    if mechanism_supported and not contrary_evidence:
        premises.append("failure-mechanism")
        premises.append("trigger-plausible")
        premises.append("absence-of-guard")

    if (
        candidate.severity in {"high", "critical"}
        or candidate.required_contract is not None
        or candidate.named_mandatory_rule is not None
    ):
        premises.append("material-impact")

    if candidate.remedy is not None and bool(candidate.remedy.strip()):
        premises.append("actionable-remedy")

    return tuple(dict.fromkeys(premises)), contrary_evidence


def _verify_candidate_evidence(
    candidate: CandidateFinding,
    snapshot: Mapping[str, str],
    *,
    reviewed_snapshot_sha256: str,
) -> VerificationResult:
    reasons: list[str] = []
    evidence_valid = True
    for reference in candidate.evidence:
        if reference.snapshot_sha256 != reviewed_snapshot_sha256:
            evidence_valid = False
            reasons.append("evidence snapshot does not match reviewed snapshot")
            continue
        snapshot_content = snapshot.get(reference.path)
        if snapshot_content is None:
            evidence_valid = False
            reasons.append("evidence path is absent from reviewed snapshot")
            continue
        lines = _snapshot_lines(snapshot_content)
        if reference.line < 1 or reference.line > len(lines):
            evidence_valid = False
            reasons.append("evidence line is outside reviewed snapshot")
            continue
        if (
            reference.excerpt is not None
            and reference.excerpt not in lines[reference.line - 1]
        ):
            evidence_valid = False
            reasons.append("evidence excerpt does not match reviewed snapshot")
    actionable = bool(
        candidate.claim.strip()
        and candidate.triggering_conditions.strip()
        and candidate.impacted_path.strip()
    )
    if not actionable:
        reasons.append("candidate lacks actionable triggering conditions")
    if not evidence_valid:
        return VerificationResult(
            "rejected", tuple(dict.fromkeys(reasons)), False, False
        )
    if not actionable:
        return VerificationResult(
            "insufficient-evidence", tuple(dict.fromkeys(reasons)), True, False
        )
    supported_premises, contrary_evidence = _assess_candidate_premises(
        candidate, snapshot
    )
    return VerificationResult(
        "confirmed",
        (),
        True,
        True,
        supported_premises=supported_premises,
        contrary_evidence=contrary_evidence,
    )


def verify_candidate(
    candidate: CandidateFinding,
    snapshot: Mapping[str, str],
    *,
    snapshot_sha256: str,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> VerificationResult:
    """Verify evidence paths/lines and excerpts against a reviewed snapshot.

    ``snapshot`` maps canonical repository paths to source text.  It is treated
    as data only; no instructions in source or candidate fields are executed.
    """
    if not isinstance(candidate, CandidateFinding):
        raise ReviewInputError("candidate must be a CandidateFinding")
    if not isinstance(snapshot_sha256, str) or not _SHA256.fullmatch(snapshot_sha256):
        raise ReviewInputError("snapshot_sha256 must be a SHA-256 digest")
    reviewed_digest = _snapshot_digest(snapshot, limits=limits)
    if reviewed_digest != snapshot_sha256:
        raise ReviewInputError("snapshot_sha256 does not match reviewed snapshot")
    return _verify_candidate_evidence(
        candidate, snapshot, reviewed_snapshot_sha256=reviewed_digest
    )


def verify_candidates(
    candidates: Sequence[CandidateFinding],
    snapshot: Mapping[str, str],
    *,
    snapshot_sha256: str,
    limits: ReviewLimits = DEFAULT_REVIEW_LIMITS,
) -> tuple[VerificationResult, ...]:
    """Verify candidates independently, de-duplicating identical claims.

    De-duplication keys on ``(impacted_path, claim, evidence_digest)`` so the
    same logical finding with distinct evidence remains distinct.  Actionable
    checks require bounded non-empty claim, triggering conditions, and path;
    semantic quality gates belong upstream of this verifier.

    Snapshot bounds and digest work happen once per batch; each candidate then
    reuses the validated snapshot mapping for evidence checks.
    """
    if not isinstance(snapshot_sha256, str) or not _SHA256.fullmatch(snapshot_sha256):
        raise ReviewInputError("snapshot_sha256 must be a SHA-256 digest")
    digest = _snapshot_digest(snapshot, limits=limits)
    if digest != snapshot_sha256:
        raise ReviewInputError("snapshot_sha256 does not match reviewed snapshot")
    seen: set[tuple[str, str, str]] = set()
    results: list[VerificationResult] = []
    for candidate in candidates:
        if not isinstance(candidate, CandidateFinding):
            raise ReviewInputError("candidates must contain CandidateFinding values")
        key = _candidate_dedup_key(candidate)
        if key in seen:
            results.append(
                VerificationResult("rejected", ("duplicate candidate",), False, False)
            )
            continue
        seen.add(key)
        results.append(
            _verify_candidate_evidence(
                candidate, snapshot, reviewed_snapshot_sha256=digest
            )
        )
    return tuple(results)


@dataclass(frozen=True)
class PublishableReview:
    """A review whose comments are safe for a publisher to emit as findings.

    The GitHub publisher intentionally compares this whole dataclass when a
    prepared artifact is rebound at the write boundary. Every admission field
    must therefore be reproducible from the publisher arguments; additive
    fields are fail-closed until that recomputation contract is updated.
    """

    result: ReviewResult
    verifications: tuple[VerificationResult, ...]
    evidence_policy: str
    unpublished: int
    # These bindings make a prepared artifact safe to reuse only for the
    # publication context that admitted it.  ``None`` is retained for direct
    # verifier callers that do not have a host head/policy context; the GitHub
    # publisher rejects such artifacts in its prepared-review shortcut.
    head_sha: str | None = None
    convergence_policy_digest: str | None = None
    diff_sha256: str | None = None


def _escape_published_text(value: str) -> str:
    """Escape untrusted candidate text before it becomes a published comment body."""

    escaped = escape_markdown_label(value.strip())
    return _MENTION_PREFIX.sub(r"\1\\@", escaped)


def _escape_inline_excerpt(value: str) -> str:
    """Keep excerpt text inside one inline-code span."""

    return value.replace("\\", "\\\\").replace("`", "\\`")


def format_candidate_finding(candidate: CandidateFinding) -> str:
    """Render a candidate as concise published reasoning, never a raw transcript."""

    if not isinstance(candidate, CandidateFinding):
        raise ReviewInputError("candidate must be a CandidateFinding")
    evidence_parts: list[str] = []
    for reference in candidate.evidence:
        location = f"{_escape_published_text(reference.path)}:{reference.line}"
        if reference.excerpt:
            excerpt = _escape_inline_excerpt(reference.excerpt)
            evidence_parts.append(f"{location} (`{excerpt}`)")
        else:
            evidence_parts.append(location)
    lines = [
        _escape_published_text(candidate.claim),
        "",
        f"Trigger: {_escape_published_text(candidate.triggering_conditions)}",
        f"Why it matters: {_escape_published_text(candidate.severity_rationale)}",
        f"Evidence: {'; '.join(evidence_parts)}",
    ]
    if candidate.remedy is not None:
        lines.append(f"Fix: {_escape_published_text(candidate.remedy)}")
    if candidate.assumptions:
        escaped = "; ".join(
            _escape_published_text(assumption) for assumption in candidate.assumptions
        )
        lines.append(f"Assumptions: {escaped}")
    return "\n".join(lines)


def _candidate_has_excerpt_evidence(candidate: CandidateFinding) -> bool:
    return any(
        reference.excerpt is not None and bool(reference.excerpt.strip())
        for reference in candidate.evidence
    )


def _has_verified_causal_defect(
    candidate: CandidateFinding | None,
    verification: VerificationResult,
) -> bool:
    if candidate is None:
        return False
    if (
        verification.disposition != "confirmed"
        or not verification.evidence_valid
        or not verification.actionable
        or verification.contrary_evidence
    ):
        return False
    if candidate.assumptions:
        return False
    required_premises = {
        "source-reference",
        "failure-mechanism",
        "absence-of-guard",
        "material-impact",
        "actionable-remedy",
    }
    return required_premises.issubset(set(verification.supported_premises))


def _has_plausible_unverified_high_impact(
    candidate: CandidateFinding | None,
    verification: VerificationResult,
) -> bool:
    if candidate is None:
        return False
    if (
        verification.disposition != "confirmed"
        or not verification.evidence_valid
        or not verification.actionable
        or verification.contrary_evidence
    ):
        return False
    if not candidate.assumptions:
        return False
    required_premises = {
        "source-reference",
        "failure-mechanism",
        "absence-of-guard",
        "material-impact",
    }
    return required_premises.issubset(set(verification.supported_premises))


def _candidate_to_comment(candidate: CandidateFinding) -> ReviewComment:
    """Render a confirmed candidate as a comment using structured fields only."""

    # Anchor the finding at the first evidence reference on impacted_path when
    # present; otherwise fall back to the primary reference. Publishers may
    # still reject the location against the diff if it is outside hunks.
    located = next(
        (
            reference
            for reference in candidate.evidence
            if reference.path == candidate.impacted_path
        ),
        candidate.evidence[0],
    )
    return ReviewComment(
        path=located.path,
        line=located.line,
        body=format_candidate_finding(candidate),
        severity=candidate.severity,
        blocking=None,
    )


def _verification_coverage(
    verifications: Sequence[VerificationResult],
    *,
    dropped_legacy: int = 0,
) -> str:
    counts = {"confirmed": 0, "rejected": 0, "insufficient-evidence": 0}
    reason_counts: Counter[str] = Counter()
    for item in verifications:
        counts[item.disposition] += 1
        if item.disposition != "confirmed":
            for reason in item.reasons:
                reason_counts[reason] += 1
    parts: list[str] = []
    if verifications:
        parts.append(
            "Verification coverage: "
            f"confirmed={counts['confirmed']}, "
            f"rejected={counts['rejected']}, "
            f"insufficient-evidence={counts['insufficient-evidence']}."
        )
        if reason_counts:
            breakdown = ", ".join(
                f"{reason}={count}" for reason, count in sorted(reason_counts.items())
            )
            parts.append(f"Rejection reasons: {breakdown}.")
    if dropped_legacy:
        parts.append(
            f"Dropped legacy comments: {dropped_legacy} "
            "(superseded by confirmed candidate verification). "
            "Legacy single-pass comments are not findings under confirmed policy."
        )
    parts.append(_COVERAGE_NOTE)
    return " ".join(parts)


def _downgrade_incomplete_status(status: str) -> str:
    if status in {"complete", "summary-only"}:
        return "partial"
    return status


def _comment_in_changed_lines(
    comment: ReviewComment,
    changed_lines: Mapping[str, frozenset[int]] | None,
    deleted_lines: Mapping[str, frozenset[int]] | None = None,
) -> bool:
    return comment_targets_pr_change(
        comment,
        changed_lines=changed_lines,
        deleted_lines=deleted_lines,
    )


def _blocker_candidate_from_verification(
    comment: ReviewComment,
    verification: VerificationResult,
    *,
    candidate: CandidateFinding | None = None,
    changed_lines: Mapping[str, frozenset[int]] | None = None,
    deleted_lines: Mapping[str, frozenset[int]] | None = None,
) -> BlockerCandidate:
    """Map trusted verification facts for blocker admission.

    Snapshot confirmation proves evidence locations. Only when the cited code
    also supports the alleged failure mechanism without contrary guards, with
    structured material impact, an actionable remedy, and no unverified
    assumptions, do we populate causal blocker facts. Ambiguous high-impact
    candidates whose code mechanism is plausible but depend on unverified
    assumptions become human-decision obligations, while disproved or
    unsupported allegations fail closed without manufacturing blockers or
    human-review bottlenecks.
    """

    causal_defect = comment.blocking is not False and _has_verified_causal_defect(
        candidate, verification
    )
    mechanism_refuted_or_absent = (
        comment.blocking is None
        and candidate is not None
        and (
            verification.contrary_evidence
            or "failure-mechanism" not in verification.supported_premises
        )
    )
    return derive_blocker_candidate(
        comment,
        on_changed_path=_comment_in_changed_lines(
            comment, changed_lines, deleted_lines
        ),
        evidence_locations_validated=verification.evidence_valid,
        has_failure_condition=causal_defect,
        has_actionable_remedy=(
            True
            if causal_defect
            else (
                True
                if candidate is not None
                and "actionable-remedy" in verification.supported_premises
                and not mechanism_refuted_or_absent
                else None
            )
        ),
        has_specific_violation=(
            True
            if causal_defect
            else (
                True
                if candidate is not None
                and (
                    candidate.required_contract is not None
                    or candidate.named_mandatory_rule is not None
                )
                and not mechanism_refuted_or_absent
                else None
            )
        ),
        has_required_contract=(
            True
            if candidate is not None
            and candidate.required_contract is not None
            and not mechanism_refuted_or_absent
            else None
        ),
        named_mandatory_rule=(
            candidate.named_mandatory_rule
            if candidate is not None and not mechanism_refuted_or_absent
            else None
        ),
    )


def _align_confirmed_blocker_candidates(
    *,
    blocker_candidates: Sequence[BlockerCandidate] | None,
    input_blocker_candidates: Sequence[BlockerCandidate] | None,
    input_count: int,
    published_indices: Sequence[int],
    derived: Sequence[BlockerCandidate],
) -> Sequence[BlockerCandidate]:
    """Align caller facts with an explicit pre- or post-verification basis.

    ``blocker_candidates`` must match published comments.
    ``input_blocker_candidates`` must match input candidates and is then
    sliced to published indices. Passing both fails closed.
    """

    if blocker_candidates is not None and input_blocker_candidates is not None:
        raise ReviewInputError("specify only one blocker candidate basis")
    published_count = len(published_indices)
    if input_blocker_candidates is not None:
        if len(input_blocker_candidates) != input_count:
            raise ReviewInputError(
                "input blocker candidates must align with input candidates"
            )
        return tuple(input_blocker_candidates[index] for index in published_indices)
    if blocker_candidates is not None:
        if len(blocker_candidates) != published_count:
            raise ReviewInputError("blocker candidates must align with review comments")
        return blocker_candidates
    return derived


def _with_admission(
    result: ReviewResult,
    *,
    convergence_policy: ReviewConvergencePolicy | None,
    blocker_candidates: Sequence[BlockerCandidate] | None,
    changed_lines: Mapping[str, frozenset[int]] | None,
    deleted_lines: Mapping[str, frozenset[int]] | None = None,
    derived: Sequence[BlockerCandidate] | None = None,
    baseline: ReviewBaseline | None = None,
    changed_paths: Sequence[str] | None = None,
    related_paths: Sequence[str] = (),
    current_key: ReviewContextCacheKey | None = None,
    evidence_confirmed_concerns: Sequence[str] = (),
    authorized_dispositions: Sequence[object] = (),
    current_head_sha: str | None = None,
) -> ReviewResult:
    policy = convergence_policy or ReviewConvergencePolicy()
    return admit_review_result(
        result,
        policy,
        candidates=blocker_candidates if blocker_candidates is not None else derived,
        changed_lines=changed_lines,
        deleted_lines=deleted_lines,
        baseline=baseline,
        changed_paths=changed_paths,
        related_paths=related_paths,
        current_key=current_key,
        evidence_confirmed_concerns=evidence_confirmed_concerns,
        authorized_dispositions=authorized_dispositions,
        current_head_sha=current_head_sha,
    )


def prepare_publishable_review(
    result: ReviewResult,
    *,
    candidates: Sequence[CandidateFinding] | None = None,
    snapshot: Mapping[str, str] | None = None,
    snapshot_sha256: str | None = None,
    evidence_policy: str = "legacy",
    limits: ReviewLimits | None = None,
    changed_lines: Mapping[str, frozenset[int]] | None = None,
    deleted_lines: Mapping[str, frozenset[int]] | None = None,
    convergence_policy: ReviewConvergencePolicy | None = None,
    blocker_candidates: Sequence[BlockerCandidate] | None = None,
    input_blocker_candidates: Sequence[BlockerCandidate] | None = None,
    baseline: ReviewBaseline | None = None,
    changed_paths: Sequence[str] | None = None,
    related_paths: Sequence[str] = (),
    current_key: ReviewContextCacheKey | None = None,
    evidence_confirmed_concerns: Sequence[str] = (),
    authorized_dispositions: Sequence[object] = (),
    current_head_sha: str | None = None,
) -> PublishableReview:
    """Gate findings before publication using the configured evidence policy.

    ``legacy`` is the compatible single-pass mode: existing comments publish
    unchanged and are identified by ``evidence_policy="legacy"``. Operator
    modes may still admit those comments when ``blocker_candidates`` are
    supplied; ``input_blocker_candidates`` and leftover facts under
    display-only enforcement fail closed. ``confirmed``
    publishes only candidates whose evidence exists in the exact reviewed
    snapshot and, when ``changed_lines`` is supplied, targets a changed diff
    hunk. Legacy single-pass comments are dropped once candidate verification
    runs; callers must supply candidates whenever legacy comments are present.
    Rejected, duplicate, malformed, insufficient-evidence, and out-of-diff
    candidates never become findings, and incomplete coverage cannot be a clean
    review. A confirmed review with no legacy comments, no candidates, and no
    rejections remains ``complete``. Operator review-convergence modes then
    apply trusted blocker admission before a publisher emits events.
    ``blocker_candidates`` must match published comments;
    ``input_blocker_candidates`` must match input candidates.
    """

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("review result is invalid")
    if evidence_policy not in EVIDENCE_POLICIES:
        raise ReviewInputError("evidence policy is unsupported")
    policy_for_binding = convergence_policy or ReviewConvergencePolicy()
    convergence_policy_digest = (
        policy_for_binding.digest()
        if isinstance(policy_for_binding, ReviewConvergencePolicy)
        else None
    )
    if evidence_policy == "legacy":
        if input_blocker_candidates is not None:
            raise ReviewInputError(
                "input blocker candidates require confirmed evidence policy"
            )
        policy = convergence_policy or ReviewConvergencePolicy()
        if blocker_candidates is not None and policy.enforcement != "publication":
            raise ReviewInputError("blocker candidates require publication enforcement")
        # Rebuild only when upstream tagged a non-legacy policy on the result.
        if result.evidence_policy != "legacy":
            result = replace(result, evidence_policy="legacy")
        result = _with_admission(
            result,
            convergence_policy=convergence_policy,
            blocker_candidates=blocker_candidates,
            changed_lines=changed_lines,
            deleted_lines=deleted_lines,
            baseline=baseline,
            changed_paths=changed_paths,
            related_paths=related_paths,
            current_key=current_key,
            evidence_confirmed_concerns=evidence_confirmed_concerns,
            authorized_dispositions=authorized_dispositions,
            current_head_sha=current_head_sha,
        )
        return PublishableReview(
            result,
            (),
            "legacy",
            0,
            head_sha=current_head_sha,
            convergence_policy_digest=convergence_policy_digest,
        )

    if candidates is None:
        candidates = ()
    if snapshot is None or snapshot_sha256 is None:
        raise ReviewInputError("confirmed evidence policy requires a reviewed snapshot")
    if not candidates and result.comments:
        raise ReviewInputError(
            "confirmed evidence policy requires candidates when legacy comments are present"
        )
    review_limits = limits if limits is not None else result.limits
    verifications = verify_candidates(
        candidates,
        snapshot,
        snapshot_sha256=snapshot_sha256,
        limits=review_limits,
    )
    published: list[ReviewComment] = []
    published_indices: list[int] = []
    derived_facts: list[BlockerCandidate] = []
    final_verifications: list[VerificationResult] = []
    unpublished_candidates = 0
    for index, (candidate, verification) in enumerate(
        zip(candidates, verifications, strict=True)
    ):
        if verification.disposition != "confirmed":
            final_verifications.append(verification)
            unpublished_candidates += 1
            continue
        comment = _candidate_to_comment(candidate)
        if comment.blocking is None:
            if _has_verified_causal_defect(candidate, verification):
                comment = replace(
                    comment,
                    blocking=True,
                    severity=comment.severity or candidate.severity or "high",
                )
            elif (
                comment.severity in {"high", "critical"}
                and not _has_plausible_unverified_high_impact(candidate, verification)
            ):
                comment = replace(comment, severity=None)
        if changed_lines is not None and not _comment_in_changed_lines(
            comment, changed_lines, deleted_lines
        ):
            final_verifications.append(
                VerificationResult(
                    "rejected",
                    ("location outside reviewed diff",),
                    True,
                    True,
                )
            )
            unpublished_candidates += 1
            continue
        published.append(comment)
        published_indices.append(index)
        derived_facts.append(
            _blocker_candidate_from_verification(
                comment,
                verification,
                candidate=candidate,
                changed_lines=changed_lines,
                deleted_lines=deleted_lines,
            )
        )
        final_verifications.append(verification)
    dropped_legacy = len(result.comments) if candidates else 0
    unpublished = unpublished_candidates + dropped_legacy
    incomplete = unpublished_candidates > 0 or dropped_legacy > 0
    summary = result.summary
    status = result.review_status
    if incomplete:
        status = _downgrade_incomplete_status(status)
        coverage = _verification_coverage(
            tuple(final_verifications),
            dropped_legacy=dropped_legacy,
        )
        summary = f"{summary}\n\n{coverage}" if summary else coverage
    prepared = ReviewResult(
        summary=summary,
        comments=tuple(published),
        provider=result.provider,
        model=result.model,
        learning_proposals=result.learning_proposals,
        review_status=status,
        limits=review_limits,
        source_context_coverage=result.source_context_coverage,
        evidence_policy="confirmed",
    )
    prepared = _with_admission(
        prepared,
        convergence_policy=convergence_policy,
        blocker_candidates=_align_confirmed_blocker_candidates(
            blocker_candidates=blocker_candidates,
            input_blocker_candidates=input_blocker_candidates,
            input_count=len(candidates),
            published_indices=published_indices,
            derived=tuple(derived_facts),
        ),
        changed_lines=changed_lines,
        deleted_lines=deleted_lines,
        baseline=baseline,
        changed_paths=changed_paths,
        related_paths=related_paths,
        current_key=current_key,
        evidence_confirmed_concerns=evidence_confirmed_concerns,
        authorized_dispositions=authorized_dispositions,
        current_head_sha=current_head_sha,
    )
    return PublishableReview(
        prepared,
        tuple(final_verifications),
        "confirmed",
        unpublished,
        head_sha=current_head_sha,
        convergence_policy_digest=convergence_policy_digest,
    )
