from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .errors import ReviewInputError, ReviewSenseiError
from .models import ProviderRequest, ProviderResponse, ReviewComment, ReviewRequest
from .providers.base import ReviewProvider
from .schemas import validate_public_document
from .service import ReviewService
from .validation import (
    DEFAULT_REVIEW_LIMITS,
    read_bounded_utf8,
    validate_repository_path,
)

MAX_CORPUS_FILES = 128
MAX_CORPUS_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CORPUS_JSON_BYTES = 256 * 1024
MAX_JSON_FILE_BYTES = 256 * 1024
MAX_TEXT_FILE_BYTES = 512 * 1024

_PRIVACY_PATTERNS = (
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[0-9A-Za-z]{20,}\b"),
    re.compile(r"\bBearer [0-9A-Za-z._~+/=-]{20,}\b"),
    re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
_SECRET_MARKERS = ("PRIVATE_DIFF_MARKER", "PRIVATE_PROMPT_MARKER", "PROD_OUTPUT_MARKER")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _json_digest(value: object) -> str:
    return _sha256_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def _normalize_terms(value: str) -> tuple[str, ...]:
    return tuple(token for token in re.split(r"[^a-z0-9]+", value.lower()) if token)


def _sanitized_error(kind: str, relative: Path) -> ReviewInputError:
    return ReviewInputError(f"corpus {kind} failed for {relative.as_posix()}")


def _load_json(path: Path, *, maximum: int, label: str) -> dict[str, Any]:
    text = read_bounded_utf8(path, maximum=maximum, label=label)
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReviewInputError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReviewInputError(f"{label} must be a JSON object")
    return value


def _canonical_path(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{label} must be a string")
    return validate_repository_path(value, label=label)


def _file_size(path: Path) -> int:
    return path.stat().st_size


def _relative_path(root: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ReviewInputError("corpus file escaped the corpus root") from exc


def _iter_corpus_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ReviewInputError("corpus root is not a directory")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ReviewInputError("corpus contains a symlink")
        if path.is_file():
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise ReviewInputError("corpus file escaped the corpus root") from exc
            files.append(path)
    if len(files) > MAX_CORPUS_FILES:
        raise ReviewInputError("corpus contains too many files")
    total = sum(_file_size(path) for path in files)
    if total > MAX_CORPUS_TOTAL_BYTES:
        raise ReviewInputError("corpus exceeds the total size limit")
    return files


def _scan_text(value: str, *, relative: Path) -> None:
    for pattern in _PRIVACY_PATTERNS:
        if pattern.search(value):
            raise _sanitized_error("privacy", relative)
    for marker in _SECRET_MARKERS:
        if marker in value:
            raise _sanitized_error("privacy", relative)


@dataclass(frozen=True)
class Corpus:
    """Validated evaluation corpus metadata and asset paths."""

    root: Path
    document: dict[str, Any]
    digest: str
    files: tuple[Path, ...]

    @property
    def corpus_id(self) -> str:
        return str(self.document["corpus_id"])

    @property
    def version(self) -> str:
        return str(self.document["version"])

    def asset_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ReviewInputError("corpus asset escaped the corpus root") from exc
        if path.is_symlink() or not path.is_file():
            raise ReviewInputError("corpus asset is not a regular file")
        return path

    def read_asset(self, relative: str, *, maximum: int, label: str) -> str:
        return read_bounded_utf8(
            self.asset_path(relative),
            maximum=maximum,
            label=label,
        )


def load_corpus(corpus_path: Path) -> Corpus:
    root = corpus_path.resolve().parent
    value = _load_json(
        corpus_path,
        maximum=MAX_CORPUS_JSON_BYTES,
        label="corpus",
    )
    validate_public_document(value, "evaluation-corpus")
    sample_repository = value["sample_repository"]
    for root_key in ("base_root", "head_root"):
        root_relative = _canonical_path(
            sample_repository[root_key],
            label=f"corpus sample repository {root_key}",
        )
        sample_root = (root / root_relative).resolve()
        try:
            sample_root.relative_to(root.resolve())
        except ValueError as exc:
            raise ReviewInputError(
                f"corpus sample repository {root_key} escaped the corpus root"
            ) from exc
        if sample_root.is_symlink() or not sample_root.is_dir():
            raise ReviewInputError(
                f"corpus sample repository {root_key} is not a regular directory"
            )
    files = _iter_corpus_files(root)
    declared: set[str] = set()
    for entry in value.get("files", []):
        relative = _canonical_path(entry["path"], label="corpus inventory path")
        declared.add(relative)
        actual = (root / relative).resolve()
        try:
            actual.relative_to(root.resolve())
        except ValueError as exc:
            raise ReviewInputError(
                "corpus inventory path escaped the corpus root"
            ) from exc
        if actual.is_symlink() or not actual.is_file():
            raise ReviewInputError("corpus inventory path is not a regular file")
        expected_sha = entry["sha256"]
        if relative == corpus_path.name:
            manifest = json.loads(corpus_path.read_text(encoding="utf-8"))
            for manifest_entry in manifest.get("files", []):
                if manifest_entry.get("path") == relative:
                    manifest_entry["sha256"] = ""
            actual_sha = _json_digest(manifest)
        else:
            actual_sha = _sha256_bytes(actual.read_bytes())
        if expected_sha != actual_sha:
            raise ReviewInputError("corpus inventory sha256 does not match the file")
    actual_relative = {_relative_path(root, path).as_posix() for path in files}
    if declared != actual_relative:
        raise ReviewInputError("corpus inventory does not match the declared file tree")

    total_bytes = sum(_file_size(path) for path in files)
    if total_bytes > MAX_CORPUS_TOTAL_BYTES:
        raise ReviewInputError("corpus exceeds the total size limit")

    for path in files:
        relative_path = _relative_path(root, path)
        maximum = MAX_JSON_FILE_BYTES if path.suffix == ".json" else MAX_TEXT_FILE_BYTES
        text = read_bounded_utf8(path, maximum=maximum, label="corpus file")
        _scan_text(text, relative=relative_path)

    for case in value.get("cases", []):
        for key in ("diff_path", "response_path", "expected_result_path"):
            relative = case.get(key)
            if relative is not None:
                asset = _canonical_path(relative, label="corpus case path")
                if asset not in declared:
                    raise ReviewInputError("corpus case asset is not declared")

    return Corpus(
        root=root,
        document=value,
        digest=_sha256_bytes(corpus_path.read_bytes()),
        files=tuple(files),
    )


def package_stage_digest() -> str:
    from .service import DEFAULT_STAGES

    return _json_digest(
        [
            {
                "name": stage.name,
                "prompt_template": stage.prompt_template,
                "outputs": list(stage.outputs),
                "category_ids": [category.id for category in stage.categories],
            }
            for stage in DEFAULT_STAGES
        ]
    )


def _packaged_category_digest() -> str:
    from .service import DEFAULT_CATEGORY_CATALOG

    return _json_digest(
        [
            {
                "id": category.id,
                "title": category.title,
                "focus": list(category.focus),
                "applies_to": list(category.applies_to),
                "learning_categories": list(category.learning_categories),
                "include_uncategorized_learnings": category.include_uncategorized_learnings,
            }
            for category in DEFAULT_CATEGORY_CATALOG.categories
        ]
    )


def configuration_digest(
    corpus: Corpus, mode: str, provider: str, model: str | None
) -> str:
    return _json_digest(
        {
            "corpus_id": corpus.corpus_id,
            "corpus_version": corpus.version,
            "mode": mode,
            "provider": provider,
            "model": model,
            "review_configuration_id": corpus.document["review_configuration"]["id"],
            "category_digest": _packaged_category_digest(),
            "stage_digest": package_stage_digest(),
        }
    )


class MeasuredProvider:
    """Provider wrapper retaining only non-secret call metrics."""

    name: str
    model: str | None

    def __init__(self, provider: ReviewProvider | None) -> None:
        self._provider = provider
        self.name = provider.name if provider is not None else "aggregate"
        self.model = provider.model if provider is not None else None
        self.calls = 0
        self.elapsed_ms: list[int] = []
        self.prompt_bytes = 0
        self.response_bytes = 0

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        if self._provider is None:
            raise RuntimeError("aggregate metrics provider cannot complete requests")
        started = time.perf_counter()
        response = self._provider.complete(request)
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        self.calls += 1
        self.elapsed_ms.append(elapsed_ms)
        self.prompt_bytes += len(request.prompt.encode("utf-8"))
        self.response_bytes += len(response.text.encode("utf-8"))
        return response

    def snapshot(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "elapsed_ms": self.elapsed_ms,
            "prompt_bytes": self.prompt_bytes,
            "response_bytes": self.response_bytes,
        }


def endpoint_scope(base_url: str | None) -> str:
    if not base_url:
        return "none"
    parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(base_url)
    host = parsed.hostname or ""
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        return "loopback"
    return "remote"


@dataclass(frozen=True)
class ExpectedFinding:
    path: str
    line: int
    category: str | None
    body_terms: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExpectedFinding":
        return cls(
            path=str(value["path"]),
            line=int(value["line"]),
            category=value.get("category"),
            body_terms=tuple(
                normalized
                for term in value["body_terms"]
                for normalized in _normalize_terms(str(term))
            ),
        )


def _one_to_one_matches(
    comments: Sequence[ReviewComment],
    expected: Sequence[ExpectedFinding],
) -> tuple[int, int, int]:
    unmatched = list(expected)
    actual_matches = 0
    false_positives = 0
    for comment in comments:
        found = None
        for index, candidate in enumerate(unmatched):
            terms = _normalize_terms(comment.body)
            if (
                comment.path == candidate.path
                and comment.line == candidate.line
                and (
                    candidate.category is None or comment.category == candidate.category
                )
                and set(candidate.body_terms).issubset(terms)
            ):
                found = index
                break
        if found is None:
            false_positives += 1
        else:
            unmatched.pop(found)
            actual_matches += 1
    return actual_matches, len(expected) - len(unmatched), false_positives


def _percent(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return numerator / denominator


def _p95(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return float(ordered[index])


def run_case(
    corpus: Corpus,
    case: dict[str, Any],
    service: ReviewService,
    measured: MeasuredProvider,
) -> dict[str, Any]:
    case_id = str(case["id"])
    kind = str(case["kind"])
    category = str(case["category"])
    expected_category = str(case["expected_error_category"])
    started = time.perf_counter()
    status = "failed"
    expected_matches = 0
    actual_matches = 0
    false_positives = 0
    location_valid = True
    category_valid = True
    calls_before = measured.calls
    prompt_before = measured.prompt_bytes
    response_before = measured.response_bytes
    request = ReviewRequest(
        diff=corpus.read_asset(
            str(case["diff_path"]),
            maximum=1048576,
            label="case diff",
        ),
        repository="synthetic/sample",
        title=str(case["title"]),
        propose_learnings=False,
        limits=DEFAULT_REVIEW_LIMITS,
    )
    result = None
    error_category = "none"
    try:
        result = service.review(request)
    except ReviewSenseiError as exc:
        error_category = exc.error_category
    elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
    case_calls = measured.calls - calls_before
    case_prompt_bytes = measured.prompt_bytes - prompt_before
    case_response_bytes = measured.response_bytes - response_before

    if kind == "contract-rejection":
        status = (
            "rejected-expected"
            if error_category == expected_category
            else "rejected-unexpected"
        )
        return {
            "id": case_id,
            "kind": kind,
            "category": category,
            "status": status,
            "expected_matches": 0,
            "actual_matches": 0,
            "false_positives": 0,
            "location_valid": True,
            "category_valid": True,
            "elapsed_ms": elapsed_ms,
            "provider_calls": case_calls,
            "prompt_bytes": case_prompt_bytes,
            "response_bytes": case_response_bytes,
        }

    if result is None:
        status = "rejected-unexpected"
        return {
            "id": case_id,
            "kind": kind,
            "category": category,
            "status": status,
            "expected_matches": 0,
            "actual_matches": 0,
            "false_positives": 0,
            "location_valid": False,
            "category_valid": False,
            "elapsed_ms": elapsed_ms,
            "provider_calls": case_calls,
            "prompt_bytes": case_prompt_bytes,
            "response_bytes": case_response_bytes,
        }

    expected = [
        ExpectedFinding.from_dict(item) for item in case.get("expected_findings", [])
    ]
    actual_matches, expected_matches, false_positives = _one_to_one_matches(
        result.comments,
        expected,
    )
    if case.get("acceptable_no_finding", False) and result.comments:
        status = "failed"
    elif (
        not case.get("expected_result_path")
        or json.loads(
            corpus.read_asset(
                str(case["expected_result_path"]),
                maximum=MAX_JSON_FILE_BYTES,
                label="expected result",
            )
        )
        == result.to_dict()
    ):
        status = "passed"
    else:
        status = "failed"
    location_valid = all(
        comment.line > 0 and comment.path for comment in result.comments
    )
    category_valid = all(
        comment.category
        in {"correctness", "security", "architecture", "maintainability", "tests"}
        for comment in result.comments
        if comment.category is not None
    )
    return {
        "id": case_id,
        "kind": kind,
        "category": category,
        "status": status,
        "expected_matches": expected_matches,
        "actual_matches": actual_matches,
        "false_positives": false_positives,
        "location_valid": location_valid,
        "category_valid": category_valid,
        "elapsed_ms": elapsed_ms,
        "provider_calls": case_calls,
        "prompt_bytes": case_prompt_bytes,
        "response_bytes": case_response_bytes,
    }


def evaluate_fixture(corpus: Corpus) -> dict[str, Any]:
    from .providers.fixture import FixtureProvider

    cases: list[dict[str, Any]] = []
    measured = MeasuredProvider(None)
    for case in corpus.document["cases"]:
        response_path = corpus.asset_path(str(case["response_path"]))
        fixture = FixtureProvider(response_path, model="fixture-v1")
        case_measured = MeasuredProvider(fixture)
        service = ReviewService(case_measured)
        cases.append(run_case(corpus, case, service, case_measured))
        measured.calls += case_measured.calls
        measured.elapsed_ms.extend(case_measured.elapsed_ms)
        measured.prompt_bytes += case_measured.prompt_bytes
        measured.response_bytes += case_measured.response_bytes
    return build_report(
        corpus,
        cases,
        measured,
        mode="fixture",
        provider="fixture",
        provider_version=None,
        model="fixture-v1",
        endpoint_scope="none",
    )


def evaluate_live(
    corpus: Corpus,
    provider: ReviewProvider,
    *,
    provider_version: str,
    model: str | None,
    endpoint_scope: str,
) -> dict[str, Any]:
    measured = MeasuredProvider(provider)
    cases: list[dict[str, Any]] = []
    for case in corpus.document["cases"]:
        if case["kind"] == "contract-rejection":
            cases.append(
                {
                    "id": str(case["id"]),
                    "kind": "contract-rejection",
                    "category": str(case["category"]),
                    "status": "skipped",
                    "expected_matches": 0,
                    "actual_matches": 0,
                    "false_positives": 0,
                    "location_valid": True,
                    "category_valid": True,
                    "elapsed_ms": 0,
                    "provider_calls": 0,
                    "prompt_bytes": 0,
                    "response_bytes": 0,
                }
            )
            continue
        service = ReviewService(measured)
        cases.append(run_case(corpus, case, service, measured))
    return build_report(
        corpus,
        cases,
        measured,
        mode="live",
        provider=provider.name,
        provider_version=provider_version,
        model=model,
        endpoint_scope=endpoint_scope,
    )


def build_report(
    corpus: Corpus,
    cases: Sequence[dict[str, Any]],
    measured: MeasuredProvider,
    *,
    mode: str,
    provider: str,
    provider_version: str | None,
    model: str | None,
    endpoint_scope: str,
) -> dict[str, Any]:
    from .cli import _package_version

    quality_cases = [case for case in cases if case["kind"] == "quality"]
    expected = sum(case["expected_matches"] for case in quality_cases)
    actual = sum(case["actual_matches"] for case in quality_cases)
    false_positives = sum(case["false_positives"] for case in quality_cases)
    precision = _percent(actual, actual + false_positives)
    recall = _percent(actual, expected)
    fpr = false_positives / max(1, len(quality_cases))
    location_valid = _percent(
        sum(case["location_valid"] for case in quality_cases),
        len(quality_cases),
    )
    categories = {
        str(case["category"])
        for case in quality_cases
        if case["category"] and case["status"] == "passed"
    }
    required_categories = {"correctness", "security", "architecture", "tests"}
    category_coverage = _percent(
        len(categories & required_categories), len(required_categories)
    )

    exact = sum(
        1
        for case in cases
        if case["kind"] == "quality"
        and case["status"] in {"passed", "rejected-expected"}
    )
    rejection = sum(
        1
        for case in cases
        if case["kind"] == "contract-rejection"
        and case["status"] == "rejected-expected"
    )
    contract_cases = [case for case in cases if case["kind"] == "contract-rejection"]
    exact_rate = _percent(exact, len(quality_cases))
    rejection_rate = _percent(rejection, len(contract_cases))

    threshold_failures: list[str] = []
    report = {
        "schema_version": "1.0",
        "corpus": {
            "id": corpus.corpus_id,
            "version": corpus.version,
            "sha256": corpus.digest,
        },
        "run": {
            "mode": mode,
            "review_sensei_version": _package_version(),
            "provider": provider,
            "provider_version": provider_version,
            "model": model,
            "endpoint_scope": endpoint_scope,
            "package_stage_digest": package_stage_digest(),
            "configuration_digest": configuration_digest(corpus, mode, provider, model),
        },
        "configuration": {
            "review_configuration_id": corpus.document["review_configuration"]["id"],
        },
        "privacy": {
            "status": "clean",
            "scanned_inventory_count": len(corpus.files),
        },
        "cases": [dict(case) for case in cases],
        "metrics": {
            "provider_calls": measured.calls,
            "prompt_bytes": measured.prompt_bytes,
            "response_bytes": measured.response_bytes,
            "token_proxy_4_bytes": math.ceil(
                (measured.prompt_bytes + measured.response_bytes) / 4
            ),
            "elapsed_total_ms": sum(measured.elapsed_ms),
            "elapsed_mean_ms": (sum(measured.elapsed_ms) / len(measured.elapsed_ms))
            if measured.elapsed_ms
            else 0.0,
            "elapsed_p95_ms": _p95(measured.elapsed_ms),
        },
        "deterministic": {
            "exact_fixture_result_rate": exact_rate,
            "expected_contract_rejection_rate": rejection_rate,
        },
        "quality": {
            "actionable_precision": precision,
            "false_positive_rate": fpr,
            "expected_finding_recall": recall,
            "location_validity": location_valid,
            "category_coverage": category_coverage,
        },
        "threshold_failures": threshold_failures,
        "passed": True,
    }
    thresholds = corpus.document["deterministic_thresholds"]
    if exact_rate < float(thresholds["exact_fixture_result_rate"]):
        threshold_failures.append("exact_fixture_result_rate")
    if rejection_rate < float(thresholds["expected_contract_rejection_rate"]):
        threshold_failures.append("expected_contract_rejection_rate")
    quality = corpus.document["quality_thresholds"]
    if precision < float(quality["actionable_precision_minimum"]):
        threshold_failures.append("actionable_precision_minimum")
    if fpr > float(quality["false_positive_rate_maximum"]):
        threshold_failures.append("false_positive_rate_maximum")
    if recall < float(quality["expected_finding_recall_minimum"]):
        threshold_failures.append("expected_finding_recall_minimum")
    if location_valid < float(quality["location_validity_minimum"]):
        threshold_failures.append("location_validity_minimum")
    if category_coverage < float(quality["category_coverage_minimum"]):
        threshold_failures.append("category_coverage_minimum")
    report["passed"] = not threshold_failures
    validate_public_document(report, "evaluation-report")
    return report
