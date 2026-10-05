"""Closed broker diagnostics shared with the Worker; never render remote text."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Mapping

MAX_DIAGNOSTIC_BYTES = 4096
_RAY = re.compile(r"[a-f0-9]{16,64}-[a-z]{3}", re.IGNORECASE)
_CATALOG = json.loads(
    files("review_sensei")
    .joinpath("schemas/broker-diagnostics.json")
    .read_text("utf-8")
)


def _correlation(value: object) -> str | None:
    return value if isinstance(value, str) and _RAY.fullmatch(value) else None


@dataclass(frozen=True)
class BrokerDiagnostic:
    code: str
    correlation_id: str | None = None
    upstream_status: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in _CATALOG:
            raise ValueError("unknown broker diagnostic")
        if (
            self.correlation_id is not None
            and _correlation(self.correlation_id) is None
        ):
            raise ValueError("invalid broker correlation")
        if self.upstream_status is not None and (
            type(self.upstream_status) is not int
            or not 100 <= self.upstream_status <= 599
            or self.code
            not in {
                "github_workflow_tag_unavailable",
                "github_installation_token_failed",
                "github_capability_issue_failed",
            }
        ):
            raise ValueError("invalid broker upstream status")

    @property
    def stage(self) -> str:
        return str(_CATALOG[self.code]["stage"])

    @property
    def action(self) -> str:
        return str(_CATALOG[self.code]["action"])

    @property
    def hint(self) -> str:
        return str(_CATALOG[self.code]["hint"])

    def metadata(self) -> dict[str, str]:
        values = {"code": self.code, "stage": self.stage, "action": self.action}
        if self.correlation_id is not None:
            values["correlation_id"] = self.correlation_id
        if self.upstream_status is not None:
            values["upstream_status"] = str(self.upstream_status)
        return {f"broker.{key}": value for key, value in values.items()}

    def message(self) -> str:
        facts = " ".join(f"{key}={value}" for key, value in self.metadata().items())
        return f"{facts}. {self.hint}"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate diagnostic field")
        result[key] = value
    return result


def parse_broker_diagnostic(error: Any) -> BrokerDiagnostic:
    """Best-effort bounded inspection of an HTTP error, including old Workers."""
    correlation = None
    try:
        correlation = _correlation(error.headers.get("cf-ray"))
    except Exception:  # Supplementary metadata cannot replace the HTTP failure.
        pass
    fallback = BrokerDiagnostic("broker_unknown", correlation)
    try:
        raw = error.read(MAX_DIAGNOSTIC_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_DIAGNOSTIC_BYTES:
            return fallback
        body = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object
        )
        if not isinstance(body, dict):
            return fallback
        value = body.get("diagnostic")
        if (
            not isinstance(value, dict)
            or not {"version", "code", "stage", "action"} <= value.keys()
        ):
            return fallback
        if value.keys() - {
            "version",
            "code",
            "stage",
            "action",
            "correlation_id",
            "upstream_status",
        }:
            return fallback
        if type(value["version"]) is not int or value["version"] != 1:
            return fallback
        code = value["code"]
        if not isinstance(code, str) or code not in _CATALOG:
            return fallback
        if (
            value["stage"] != _CATALOG[code]["stage"]
            or value["action"] != _CATALOG[code]["action"]
        ):
            return fallback
        if "correlation_id" in value and _correlation(value["correlation_id"]) is None:
            return fallback
        if "upstream_status" in value and value["upstream_status"] is None:
            return fallback
        return BrokerDiagnostic(
            code, value.get("correlation_id", correlation), value.get("upstream_status")
        )
    except Exception:  # Includes interrupted/truncated reads; never render the error.
        return fallback
    finally:
        try:
            error.close()
        except Exception:
            pass


def exception_broker_diagnostic(error: BaseException) -> BrokerDiagnostic | None:
    """Follow only a bounded explicit cause chain, including session wrappers."""
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        value = getattr(current, "broker_diagnostic", None)
        if isinstance(value, BrokerDiagnostic):
            return value
        current = current.__cause__
    return None


def outcome_broker_diagnostic(
    diagnostic: str | None, metadata: Mapping[str, str]
) -> BrokerDiagnostic | None:
    if diagnostic not in {"broker_rejected", "broker_temporarily_unavailable"}:
        return None
    values = {
        key.removeprefix("broker."): value
        for key, value in metadata.items()
        if key.startswith("broker.")
    }
    if values.keys() - {"code", "stage", "action", "correlation_id", "upstream_status"}:
        return None
    try:
        status = values.get("upstream_status")
        if status is not None and re.fullmatch(r"[1-5][0-9]{2}", status) is None:
            return None
        result = BrokerDiagnostic(
            values["code"],
            values.get("correlation_id"),
            int(status) if status else None,
        )
        if values != {
            key.removeprefix("broker."): value
            for key, value in result.metadata().items()
        }:
            return None
        return result
    except (KeyError, ValueError, TypeError):
        return None
