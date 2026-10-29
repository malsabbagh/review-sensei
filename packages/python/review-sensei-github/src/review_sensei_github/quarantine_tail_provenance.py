"""Closed installed historical provenance; never caller-supplied attestation."""

from __future__ import annotations

import re

from review_sensei.errors import ReviewInputError

from .reservation_recovery import _stamp

SURFACES = frozenset(
    {
        "evidence_parts",
        "issue_comments",
        "inline_replies",
        "reviews",
        "checks",
        "threads",
        "reactions",
    }
)


def _digest(value, width=64):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-f0-9]{" + str(width) + "}", value) is not None
    )


def validate_provenance(value, contract):
    """Review authenticates installed readbacks; hashes only bind their identity."""
    from .quarantine_published_tail import execution_manifest_digest

    fields = {
        "schema_version",
        "contract",
        "executor_sha",
        "execution_manifest_sha256",
        "broker",
        "exposure",
        "prewrite_refusals",
    }
    projected = (
        isinstance(value, dict)
        and value.get("schema_version") == "quarantine-published-tail-provenance-v2"
    )
    if projected:
        fields.add("reader_projection")
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema_version"]
        not in {
            "quarantine-published-tail-provenance-v1",
            "quarantine-published-tail-provenance-v2",
        }
        or value["contract"] != contract["schema_version"]
        or value["executor_sha"] != contract["executor_sha"]
        or value["execution_manifest_sha256"] != execution_manifest_digest(contract)
    ):
        raise ReviewInputError("published-tail installed provenance scope conflicts")
    broker = value["broker"]
    if (
        not isinstance(broker, dict)
        or set(broker)
        != {
            "account",
            "service",
            "environment",
            "endpoints",
            "native_contract",
            "bindings_sha256",
            "routing_intervals",
            "readback_sha256s",
        }
        or broker["native_contract"] != "no-independent-native-write-continuation-v1"
        or any(
            not isinstance(broker[k], str) or not 1 <= len(broker[k]) <= 256
            for k in ("account", "service", "environment")
        )
        or broker["endpoints"] != ["/github/token", "/github/session-grant"]
        or not _digest(broker["bindings_sha256"])
        or not isinstance(broker["readback_sha256s"], list)
        or not 1 <= len(broker["readback_sha256s"]) <= 32
        or not all(_digest(d) for d in broker["readback_sha256s"])
        or not isinstance(broker["routing_intervals"], list)
        or not 1 <= len(broker["routing_intervals"]) <= 16
    ):
        raise ReviewInputError(
            "published-tail historical broker provenance is unsupported"
        )
    prior_end = None
    for interval in broker["routing_intervals"]:
        if not isinstance(interval, dict) or set(interval) != {
            "starts_at",
            "ends_at",
            "versions",
        }:
            raise ReviewInputError("published-tail routing interval is unsupported")
        start, end = _stamp(interval["starts_at"]), _stamp(interval["ends_at"])
        if start >= end or prior_end is not None and start != prior_end:
            raise ReviewInputError(
                "published-tail historical routing has a gap or overlap"
            )
        prior_end = end
        versions = interval["versions"]
        if not isinstance(versions, list) or not 1 <= len(versions) <= 16:
            raise ReviewInputError("published-tail routed version census is incomplete")
        for version in versions:
            if (
                not isinstance(version, dict)
                or set(version)
                != {
                    "deployment_id",
                    "version_id",
                    "bundle_sha256",
                    "source_commit",
                    "build_sha256",
                }
                or any(
                    not isinstance(version[k], str) or not 1 <= len(version[k]) <= 256
                    for k in ("deployment_id", "version_id")
                )
                or not _digest(version["source_commit"], 40)
                or not all(
                    _digest(version[k]) for k in ("bundle_sha256", "build_sha256")
                )
            ):
                raise ReviewInputError(
                    "published-tail deployed source/build binding is unsupported"
                )
    exposure = value["exposure"]
    if (
        not isinstance(exposure, dict)
        or set(exposure) != {"source_contract_sha256", "surfaces"}
        or not _digest(exposure["source_contract_sha256"])
        or not isinstance(exposure["surfaces"], dict)
        or set(exposure["surfaces"]) != SURFACES
    ):
        raise ReviewInputError("published-tail surface exposure proof is incomplete")
    for surface in exposure["surfaces"].values():
        if (
            not isinstance(surface, dict)
            or set(surface)
            != {
                "new_objects",
                "new_wire_bytes",
                "positive_update_bytes",
                "max_object_wire_bytes",
            }
            or any(
                type(n) is not int or not 0 <= n <= 9_007_199_254_740_991
                for n in surface.values()
            )
        ):
            raise ReviewInputError("published-tail surface exposure bound is invalid")
    if projected:
        from .native_reader_projection import validate_reader_projection

        validate_reader_projection(
            value["reader_projection"],
            contract,
            source_contract_sha256=exposure["source_contract_sha256"],
            surface_exposure=exposure["surfaces"],
        )
    refusals = value["prewrite_refusals"]
    if not isinstance(refusals, list) or len(refusals) > 8:
        raise ReviewInputError("published-tail pre-write refusal census is unsupported")
    for refused in refusals:
        if (
            not isinstance(refused, dict)
            or set(refused)
            != {
                "run_id",
                "head_sha",
                "writer_inventory_sha256",
                "failure_evidence_sha256",
            }
            or type(refused["run_id"]) is not int
            or refused["run_id"] < 1
            or not _digest(refused["head_sha"], 40)
            or not all(
                _digest(refused[k])
                for k in ("writer_inventory_sha256", "failure_evidence_sha256")
            )
        ):
            raise ReviewInputError(
                "published-tail positive pre-write refusal proof is unsupported"
            )
    ids = [r["run_id"] for r in refusals]
    if len(ids) != len(set(ids)) or set(ids) & {
        e["run_id"] for e in contract["executions"]
    }:
        raise ReviewInputError("published-tail execution roles overlap")
    return value


def exposure_reserve(qualification):
    surfaces = qualification["exposure"]["surfaces"]
    parts = surfaces["evidence_parts"]
    native = [surface for name, surface in surfaces.items() if name != "evidence_parts"]
    return {
        "evidence_parts": parts["new_objects"],
        "evidence_bytes": parts["new_wire_bytes"] + parts["positive_update_bytes"],
        "native_objects": sum(s["new_objects"] for s in native),
        "native_bytes": sum(
            s["new_wire_bytes"] + s["positive_update_bytes"] for s in native
        ),
    }
