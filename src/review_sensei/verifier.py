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
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

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


def _enclosing_scope_preceding_lines(
    lines: Sequence[str], line_number: int
) -> list[str]:
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
        if indent < target_indent and stripped.startswith(
            ("def ", "async def ", "class ")
        ):
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


def _unparse_expr(node: ast.AST) -> str:
    try:
        return ast.unparse(node).strip()
    except Exception:
        return ""


def _dereferenced_expressions_on_line(tree: ast.AST, line_no: int) -> set[str]:
    """Return canonical AST expressions dereferenced via attribute or subscript on ``line_no``."""
    exprs: set[str] = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line_no:
            continue
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            text = _unparse_expr(node.value)
            if text:
                exprs.add(text)
    return exprs


def _divisor_expressions_on_line(tree: ast.AST, line_no: int) -> set[str]:
    """Return canonical AST divisor expressions in ``/``, ``//``, or ``%`` on ``line_no``."""
    divisors: set[str] = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line_no:
            continue
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Div, ast.FloorDiv, ast.Mod)
        ):
            if (
                isinstance(node.right, ast.Constant)
                and isinstance(node.right.value, (int, float))
                and node.right.value != 0
            ):
                continue
            text = _unparse_expr(node.right)
            if text:
                divisors.add(text)
    return divisors


def _extract_non_none_guards(test: ast.expr) -> set[str]:
    """Return expressions guaranteed non-None when ``test`` evaluates to True."""
    guards: set[str] = set()
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.And):
            for value in test.values:
                guards.update(_extract_non_none_guards(value))
        return guards
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _extract_is_none_guards(test.operand)
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and len(test.comparators) == 1
    ):
        op = test.ops[0]
        left = test.left
        right = test.comparators[0]
        if isinstance(op, ast.IsNot):
            if isinstance(right, ast.Constant) and right.value is None:
                expr = _unparse_expr(left)
                if expr:
                    guards.add(expr)
            elif isinstance(left, ast.Constant) and left.value is None:
                expr = _unparse_expr(right)
                if expr:
                    guards.add(expr)
        return guards
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id in {"isinstance", "bool"}
        and test.args
    ):
        expr = _unparse_expr(test.args[0])
        if expr:
            guards.add(expr)
        return guards
    if isinstance(test, (ast.Name, ast.Attribute, ast.Subscript)):
        expr = _unparse_expr(test)
        if expr:
            guards.add(expr)
    return guards


def _extract_is_none_guards(test: ast.expr) -> set[str]:
    """Return expressions guaranteed non-None when ``test`` evaluates to False (e.g. after ``if test: return``)."""
    guards: set[str] = set()
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.Or):
            for value in test.values:
                guards.update(_extract_is_none_guards(value))
        return guards
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _extract_non_none_guards(test.operand)
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and len(test.comparators) == 1
    ):
        op = test.ops[0]
        left = test.left
        right = test.comparators[0]
        if isinstance(op, ast.Is):
            if isinstance(right, ast.Constant) and right.value is None:
                expr = _unparse_expr(left)
                if expr:
                    guards.add(expr)
            elif isinstance(left, ast.Constant) and left.value is None:
                expr = _unparse_expr(right)
                if expr:
                    guards.add(expr)
    return guards


def _extract_non_zero_guards(test: ast.expr) -> set[str]:
    """Return expressions guaranteed non-zero when ``test`` evaluates to True."""
    guards: set[str] = set()
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.And):
            for value in test.values:
                guards.update(_extract_non_zero_guards(value))
        return guards
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _extract_is_zero_guards(test.operand)
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and len(test.comparators) == 1
    ):
        op = test.ops[0]
        left = test.left
        right = test.comparators[0]
        if (
            isinstance(op, (ast.NotEq, ast.Gt, ast.Lt))
            and isinstance(right, ast.Constant)
            and right.value == 0
        ):
            expr = _unparse_expr(left)
            if expr:
                guards.add(expr)
            if (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name)
                and left.func.id == "len"
                and len(left.args) == 1
            ):
                arg_expr = _unparse_expr(left.args[0])
                if arg_expr:
                    guards.add(arg_expr)
        return guards
    if isinstance(test, (ast.Name, ast.Attribute, ast.Subscript)):
        expr = _unparse_expr(test)
        if expr:
            guards.add(expr)
            guards.add(f"len({expr})")
    return guards


def _extract_is_zero_guards(test: ast.expr) -> set[str]:
    """Return expressions guaranteed non-zero when ``test`` evaluates to False."""
    guards: set[str] = set()
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.Or):
            for value in test.values:
                guards.update(_extract_is_zero_guards(value))
        return guards
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _extract_non_zero_guards(test.operand)
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and len(test.comparators) == 1
    ):
        op = test.ops[0]
        left = test.left
        right = test.comparators[0]
        if (
            isinstance(op, (ast.Eq, ast.LtE))
            and isinstance(right, ast.Constant)
            and right.value == 0
        ):
            expr = _unparse_expr(left)
            if expr:
                guards.add(expr)
            if (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name)
                and left.func.id == "len"
                and len(left.args) == 1
            ):
                arg_expr = _unparse_expr(left.args[0])
                if arg_expr:
                    guards.add(arg_expr)
    return guards


def _block_terminates(stmts: Sequence[ast.stmt]) -> bool:
    """Return True if execution of ``stmts`` unconditionally exits the current block."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return True
    if isinstance(last, ast.If) and last.orelse:
        return _block_terminates(last.body) and _block_terminates(last.orelse)
    return False


def _assigned_targets_in_stmt(stmt: ast.stmt) -> set[str]:
    """Return variable and attribute targets assigned or rebound within ``stmt``."""
    names: set[str] = set()
    for node in ast.walk(stmt):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node is not stmt
        ):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            expr = _unparse_expr(node)
            if expr:
                names.add(expr)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _invalidate_reassigned_guards(guards: set[str], assigned: set[str]) -> set[str]:
    if not assigned or not guards:
        return guards
    remaining: set[str] = set()
    for guard in guards:
        invalidated = False
        for target in assigned:
            if (
                guard == target
                or guard.startswith(f"{target}.")
                or guard.startswith(f"{target}[")
                or f"({target})" in guard
            ):
                invalidated = True
                break
        if not invalidated:
            remaining.add(guard)
    return remaining


def _inline_guards_on_line(
    stmt: ast.stmt,
    line_no: int,
    *,
    pos_extractor: Callable[[ast.expr], set[str]],
    neg_extractor: Callable[[ast.expr], set[str]],
) -> set[str]:
    """Extract inline expression guards (ternary IfExp or short-circuit BoolOp) on ``line_no``."""
    guards: set[str] = set()
    for node in ast.walk(stmt):
        if getattr(node, "lineno", None) != line_no:
            continue
        if isinstance(node, ast.IfExp):
            guards.update(pos_extractor(node.test))
            guards.update(neg_extractor(node.test))
        elif isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And) and len(node.values) >= 2:
                for value in node.values[:-1]:
                    guards.update(pos_extractor(value))
            elif isinstance(node.op, ast.Or) and len(node.values) >= 2:
                for value in node.values[:-1]:
                    guards.update(neg_extractor(value))
    return guards


def _active_guards_at_line(
    stmts: Sequence[ast.stmt],
    target_line: int,
    active_guards: set[str],
    *,
    pos_extractor: Callable[[ast.expr], set[str]],
    neg_extractor: Callable[[ast.expr], set[str]],
) -> set[str] | None:
    """Walk AST statements in control-flow order and return guards active at ``target_line``."""
    current_guards = set(active_guards)
    for stmt in stmts:
        start = getattr(stmt, "lineno", None)
        end = getattr(stmt, "end_lineno", start)
        if not isinstance(start, int):
            continue
        if not isinstance(end, int) or end < start:
            end = start

        if start <= target_line <= end:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found = _active_guards_at_line(
                    stmt.body,
                    target_line,
                    set(),
                    pos_extractor=pos_extractor,
                    neg_extractor=neg_extractor,
                )
                return found if found is not None else set()
            if isinstance(stmt, ast.If):
                test_end = getattr(stmt.test, "end_lineno", start) or start
                if target_line <= test_end:
                    return current_guards | _inline_guards_on_line(
                        stmt,
                        target_line,
                        pos_extractor=pos_extractor,
                        neg_extractor=neg_extractor,
                    )
                body_end = max(
                    (getattr(s, "end_lineno", getattr(s, "lineno", start)) or start)
                    for s in stmt.body
                )
                if target_line <= body_end:
                    branch_guards = current_guards | pos_extractor(stmt.test)
                    found = _active_guards_at_line(
                        stmt.body,
                        target_line,
                        branch_guards,
                        pos_extractor=pos_extractor,
                        neg_extractor=neg_extractor,
                    )
                    return found if found is not None else branch_guards
                if stmt.orelse:
                    else_guards = current_guards | neg_extractor(stmt.test)
                    found = _active_guards_at_line(
                        stmt.orelse,
                        target_line,
                        else_guards,
                        pos_extractor=pos_extractor,
                        neg_extractor=neg_extractor,
                    )
                    return found if found is not None else else_guards
            for block_attr in ("body", "orelse", "finalbody"):
                block = getattr(stmt, block_attr, None)
                if isinstance(block, list) and block:
                    found = _active_guards_at_line(
                        block,
                        target_line,
                        current_guards,
                        pos_extractor=pos_extractor,
                        neg_extractor=neg_extractor,
                    )
                    if found is not None:
                        return found
            handlers = getattr(stmt, "handlers", None)
            if isinstance(handlers, list):
                for handler in handlers:
                    h_body = getattr(handler, "body", None)
                    if isinstance(h_body, list):
                        found = _active_guards_at_line(
                            h_body,
                            target_line,
                            current_guards,
                            pos_extractor=pos_extractor,
                            neg_extractor=neg_extractor,
                        )
                        if found is not None:
                            return found
            return current_guards | _inline_guards_on_line(
                stmt,
                target_line,
                pos_extractor=pos_extractor,
                neg_extractor=neg_extractor,
            )

        if end < target_line:
            if isinstance(stmt, ast.If):
                body_exits = _block_terminates(stmt.body)
                else_exits = bool(stmt.orelse) and _block_terminates(stmt.orelse)
                if body_exits and not else_exits:
                    assigned_in_else: set[str] = set()
                    for else_stmt in stmt.orelse:
                        assigned_in_else.update(_assigned_targets_in_stmt(else_stmt))
                    current_guards = _invalidate_reassigned_guards(
                        current_guards, assigned_in_else
                    )
                    current_guards |= _invalidate_reassigned_guards(
                        neg_extractor(stmt.test),
                        assigned_in_else,
                    )
                elif else_exits and not body_exits:
                    assigned_in_body: set[str] = set()
                    for body_stmt in stmt.body:
                        assigned_in_body.update(_assigned_targets_in_stmt(body_stmt))
                    current_guards = _invalidate_reassigned_guards(
                        current_guards, assigned_in_body
                    )
                    current_guards |= _invalidate_reassigned_guards(
                        pos_extractor(stmt.test),
                        assigned_in_body,
                    )
                else:
                    assigned_both = _assigned_targets_in_stmt(stmt)
                    current_guards = _invalidate_reassigned_guards(
                        current_guards, assigned_both
                    )
            elif isinstance(stmt, ast.Assert):
                current_guards |= pos_extractor(stmt.test)
            else:
                current_guards = _invalidate_reassigned_guards(
                    current_guards, _assigned_targets_in_stmt(stmt)
                )
    return None


def _remedy_addresses_mechanism(
    remedy: str | None,
    *,
    target_identifiers: set[str],
    alleges_null: bool,
    alleges_div_zero: bool,
    alleges_lookup: bool,
    alleges_unsafe: bool,
) -> bool:
    """Return True only when ``remedy`` proposes a concrete action tied to the verified mechanism and symbols."""
    if remedy is None or not remedy.strip():
        return False
    remedy_lower = remedy.lower()
    remedy_words = set(re.findall(r"[a-z_][a-z0-9_]*", remedy_lower))
    meaningful_targets = {
        ident.lower()
        for ident in target_identifiers
        if len(ident) >= 2
        and ident.lower() not in {"def", "return", "if", "else", "for", "in", "pass"}
    }
    mentions_target = not meaningful_targets or bool(remedy_words & meaningful_targets)
    if not mentions_target:
        return False
    if alleges_null:
        return bool(
            remedy_words
            & {
                "none",
                "null",
                "guard",
                "check",
                "optional",
                "default",
                "fallback",
                "return",
                "raise",
                "if",
            }
        )
    if alleges_div_zero:
        return bool(
            remedy_words
            & {"zero", "empty", "guard", "check", "default", "return", "raise", "if"}
            or "0" in remedy_lower
        )
    if alleges_lookup:
        return bool(
            remedy_words
            & {
                "get",
                "in",
                "keyerror",
                "indexerror",
                "bounds",
                "check",
                "guard",
                "default",
                "if",
            }
        )
    if alleges_unsafe:
        return bool(
            remedy_words
            & {
                "shell",
                "false",
                "parameter",
                "parameterized",
                "placeholder",
                "safe",
                "safeloader",
                "escape",
                "allowlist",
                "shlex",
                "literal_eval",
                "list",
            }
        )
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
    target_identifiers: set[str] = set()
    saw_harmless_only = False
    saw_non_harmless = False

    for reference in candidate.evidence:
        content = snapshot.get(reference.path)
        if content is None:
            continue
        lines = _snapshot_lines(content)
        if reference.line < 1 or reference.line > len(lines):
            continue
        target_line = lines[reference.line - 1]
        for ident_token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", target_line):
            target_identifiers.add(ident_token)
        if _line_is_harmless_constant_or_declaration(target_line):
            saw_harmless_only = True
            continue
        saw_non_harmless = True

        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError):
            tree = None

        if alleges_null and tree is not None:
            derefs = _dereferenced_expressions_on_line(tree, reference.line)
            if derefs:
                guards = _active_guards_at_line(
                    tree.body,
                    reference.line,
                    set(),
                    pos_extractor=_extract_non_none_guards,
                    neg_extractor=_extract_is_none_guards,
                )
                active_guards = guards if guards is not None else set()
                if derefs.issubset(active_guards):
                    contrary_evidence = True
                else:
                    mechanism_supported = True

        if alleges_div_zero and tree is not None:
            divisors = _divisor_expressions_on_line(tree, reference.line)
            if divisors:
                guards = _active_guards_at_line(
                    tree.body,
                    reference.line,
                    set(),
                    pos_extractor=_extract_non_zero_guards,
                    neg_extractor=_extract_is_zero_guards,
                )
                active_guards = guards if guards is not None else set()
                if divisors.issubset(active_guards):
                    contrary_evidence = True
                else:
                    mechanism_supported = True

        if alleges_lookup and tree is not None:
            has_subscript = any(
                isinstance(node, ast.Subscript)
                and getattr(node, "lineno", None) == reference.line
                for node in ast.walk(tree)
            )
            if has_subscript and ".get(" not in target_line:
                mechanism_supported = True

        if alleges_unsafe:
            if "subprocess." in target_line:
                if "shell=True" in target_line or "shell = True" in target_line:
                    mechanism_supported = True
                elif (
                    "shell=False" in target_line
                    or "shell = False" in target_line
                    or re.search(r"subprocess\.[a-z_]+\(\s*\[", target_line)
                ):
                    contrary_evidence = True
            elif any(
                token in target_line
                for token in (
                    "shell=True",
                    "eval(",
                    "exec(",
                    "pickle.loads",
                    "os.system",
                )
            ):
                mechanism_supported = True
            elif any(
                sql_kw in target_line
                for sql_kw in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
            ) and any(
                interp in target_line for interp in ('f"', "f'", ".format(", "%", "+")
            ):
                mechanism_supported = True

    if (
        saw_harmless_only
        and not saw_non_harmless
        and (alleges_null or alleges_div_zero or alleges_lookup or alleges_unsafe)
    ):
        contrary_evidence = True

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

    if (
        mechanism_supported
        and not contrary_evidence
        and _remedy_addresses_mechanism(
            candidate.remedy,
            target_identifiers=target_identifiers,
            alleges_null=alleges_null,
            alleges_div_zero=alleges_div_zero,
            alleges_lookup=alleges_lookup,
            alleges_unsafe=alleges_unsafe,
        )
    ):
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
    return derive_blocker_candidate(
        comment,
        on_changed_path=_comment_in_changed_lines(
            comment, changed_lines, deleted_lines
        ),
        evidence_locations_validated=verification.evidence_valid,
        has_failure_condition=causal_defect,
        has_actionable_remedy=True if causal_defect else None,
        has_specific_violation=True if causal_defect else None,
        has_required_contract=(
            True
            if causal_defect
            and candidate is not None
            and candidate.required_contract is not None
            else None
        ),
        named_mandatory_rule=(
            candidate.named_mandatory_rule
            if causal_defect and candidate is not None
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
                and verification.contrary_evidence
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
