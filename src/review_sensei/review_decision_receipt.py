"""Bounded, non-authoritative triage receipt; no source or raw responses."""

from __future__ import annotations

from collections.abc import Mapping

from .decision_provider import canonical, sha256
from .errors import ReviewInputError
from .review_decision_contract import bounded_identity, bounded_name


def validate_decision_receipt(value: object) -> None:
    fields = {
        "schema_version",
        "policy_digest",
        "input_sha256",
        "inventory_sha256",
        "status",
        "provider",
        "model",
        "models",
        "candidates",
        "calls",
        "input_bytes",
        "output_bytes",
        "output_reserved",
        "elapsed_ms",
        "verification_called",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value["schema_version"] != "1.0"
        or value["status"] not in {"complete", "incomplete"}
        or any(
            not sha256(value[k])
            for k in ("policy_digest", "input_sha256", "inventory_sha256")
        )
        or any(not bounded_identity(value[k]) for k in ("provider", "model"))
    ):
        raise ReviewInputError("decision receipt identity is invalid")
    if (
        any(
            type(value[k]) is not int or not 0 <= value[k] <= 1_000_000_000
            for k in (
                "calls",
                "input_bytes",
                "output_bytes",
                "output_reserved",
                "elapsed_ms",
            )
        )
        or type(value["verification_called"]) is not bool
    ):
        raise ReviewInputError("decision receipt accounting is invalid")
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 250:
        raise ReviewInputError("decision receipt inventory is invalid")
    identities = set()
    for item in candidates:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "candidate_id",
                "fingerprint",
                "action",
                "status",
                "assessments",
                "suspected_duplicate",
                "verification",
            }
            or not bounded_name(item["candidate_id"])
            or not bounded_identity(item["fingerprint"])
            or item["candidate_id"] in identities
            or item["action"]
            not in {"continue", "optional_suggestion", "human_review", "verify_further"}
            or item["status"] not in {"unprocessed", "assessed"}
            or type(item["suspected_duplicate"]) is not bool
            or item["verification"]
            not in {
                "not_requested",
                "supported",
                "contradicted",
                "insufficient_evidence",
                "unavailable",
            }
        ):
            raise ReviewInputError("decision receipt candidate is invalid")
        identities.add(item["candidate_id"])
        assessments = item["assessments"]
        allowed = {
            "kind": {
                "behavioral_defect",
                "optional_improvement",
                "clarification",
                "uncertain",
            },
            "evidence": {"supported", "contradicted", "insufficient_evidence"},
            "next": {"continue", "verify_further", "human_review"},
            "duplicate": {"same_concern", "different_concern", "uncertain"},
        }
        if not isinstance(assessments, dict) or (
            assessments
            and (
                set(assessments) != set(allowed)
                or any(assessments[k] not in allowed[k] for k in allowed)
            )
        ):
            raise ReviewInputError("decision receipt assessment is invalid")
        if value["status"] == "complete" and (
            item["status"] != "assessed"
            or item["action"] not in {"continue", "optional_suggestion"}
        ):
            raise ReviewInputError("decision receipt completion is invalid")
    models = value["models"]
    if not isinstance(models, list) or len(models) > 8:
        raise ReviewInputError("decision model inventory is invalid")
    for model in models:
        if (
            not isinstance(model, dict)
            or set(model)
            != {
                "provider",
                "requested_model",
                "actual_model",
                "actual_provider",
                "input_tokens",
                "output_tokens",
            }
            or any(
                not bounded_identity(model[k])
                for k in ("provider", "requested_model", "actual_model")
            )
            or (
                model["actual_provider"] is not None
                and not bounded_identity(model["actual_provider"])
            )
            or any(
                model[k] is not None
                and (
                    type(model[k]) is not int
                    or not 0 <= model[k] <= 9_007_199_254_740_991
                )
                for k in ("input_tokens", "output_tokens")
            )
        ):
            raise ReviewInputError("decision model metadata is invalid")
    if len(canonical(dict(value)).encode("utf-8")) > 65_536:
        raise ReviewInputError("decision receipt exceeds its byte bound")
