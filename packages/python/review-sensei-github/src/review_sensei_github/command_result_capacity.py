"""Original command formatter/codec size bounds; data, never execution authority."""

import json
import sys
from dataclasses import asdict

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal


def formatter_growth_bound(*, call_limit, source_review_id, approval_review_id):
    """Canonical result-envelope growth from original finalizer formatting.

    Detached capacity data only. A caller must additionally bind the original
    base result, native source, full root/accounting projection and writer.
    The required IDs are DATA, NOT SOURCE PROOFS. The issuer must authenticate
    the selected source review and an already-known approval (or prove no new
    approval is reachable). Original publication source seals alone do not
    enforce these ID widths. An unknown future approval is unsupported.
    """
    from . import (
        application,
        approval,
        approval_progress,
        checks,
        operation_diagnostics,
        publication,
    )

    for module in (
        sys.modules[__name__],
        approval,
        approval_progress,
        checks,
        operation_diagnostics,
        application,
        publication,
    ):
        require_module_seal(module)
    if type(call_limit) is not int or not 1 <= call_limit <= 2**63 - 1:
        raise ReviewInputError("current result call limit is unsupported")

    if type(source_review_id) is not int or not 1 <= source_review_id <= 2**63 - 1:
        raise ReviewInputError("result source review identity domain is unsupported")
    if approval_review_id is not None and (
        type(approval_review_id) is not int or not 1 <= approval_review_id <= 2**63 - 1
    ):
        raise ReviewInputError("result approval review identity domain is unsupported")

    def width(value):
        return len(json.dumps(value, ensure_ascii=True).encode()) - 2

    # The original method appends at most one thread observation and one
    # terminal branch. The longest branch dominates capacity, approval and
    # retry prose, including the closed diagnostic vocabulary.
    strings = tuple(
        value
        for value in application.GitHubApplication._refinalize_command.__code__.co_consts
        if isinstance(value, str)
    )
    thread = next(
        value for value in strings if "thread resolution unconfirmed" in value
    )
    suffix = next(
        value
        for value in strings
        if value.startswith("; approval withheld (review_incomplete)")
    )
    diagnostics = list(approval._WITHHELD_DIAGNOSTICS.values())
    for function in (
        publication.ReviewApprovalFinalizer.finalize,
        publication.ReviewApprovalFinalizer._refinalize_with_dispositions_impl,
        publication.ReviewApprovalFinalizer._preflight,
    ):
        diagnostics.extend(
            value for value in function.__code__.co_consts[1:] if isinstance(value, str)
        )
    diagnostic_width = max(map(width, diagnostics))
    capacity_prefix = next(
        value for value in strings if value.startswith("; gate re-evaluation blocked (")
    )
    capacity_suffix = next(
        value for value in strings if value.startswith("); decisions retained;")
    )
    ordinary_suffix = max(width(value) for value in strings if value.startswith(";"))
    prose = width(thread) + max(
        width(suffix),
        ordinary_suffix,
        width(capacity_prefix) + diagnostic_width + width(capacity_suffix),
        width("; approval withheld (") + diagnostic_width + width(")"),
    )
    maximum = 2**63 - 1
    progress = approval_progress.ApprovalProgress(
        "1.0",
        "f" * 40,
        "f" * 64,
        maximum,
        maximum,
        maximum,
        False,
        max(approval.REVIEW_STATUSES, key=width),
        max(approval._COVERAGE_BLOCKER_STATES, key=width),
        max(approval.QUALIFICATION_STATES, key=width),
        "unconfirmed",
        "observed-existing",
        approval_review_id,
        "not-attempted",
        maximum,
        max(checks.COMPLETED_CONCLUSIONS, key=width),
        "verify_required_qualification",
    )
    nullable_calls = call_limit if len(str(call_limit)) >= len("null") else None
    failure = operation_diagnostics.OperationFailure(
        max(operation_diagnostics._STAGES, key=width),
        "changed-authority",
        None,
        tuple(
            sorted(operation_diagnostics._GRAPHQL_CATEGORIES, key=width, reverse=True)[
                :3
            ]
        ),
        nullable_calls,
    )
    phase = operation_diagnostics.PhaseFailures(failure, 1000)
    diagnostic = operation_diagnostics.OperationDiagnostics(
        phase,
        phase,
        phase,
        phase,
        nullable_calls,
        False,
        1000,
        "inspect_operation_failure",
        nullable_calls,
        nullable_calls,
    )
    observations = (
        "\napproval-operation-failure: "
        + json.dumps(
            diagnostic.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\napproval-progress: "
        + json.dumps(
            progress.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
    )
    # All non-summary command fields survive refinalization unchanged except
    # evaluation_failed. Reserve a byte rather than rely on true being shorter.
    return prose + width(observations) + 1


def command_result_capacity_bound(
    result,
    *,
    binding,
    intent_sha256,
    outcome,
    call_limit,
    reference_history,
    source_review_id,
    approval_review_id,
):
    """Data-only bound; separate issuers must prove the exact base result source.

    Full retained semantic history must be priced in the lifecycle/root proof;
    the V2 result only carries its fixed-width commitment, outside compression.
    V1 result/history routes are not qualified by this helper. The consumer
    must authenticate source_review_id and prove approval_review_id is the
    exact already-known output (None on a strictly withheld route). No future
    approval POST is supported. Values and module seals are not that proof.
    """
    from review_sensei.action_result_codec import V1, validate_action_result_envelope
    from review_sensei.disposition import MaintainerCommandResult

    require_module_seal(sys.modules[__name__])
    if type(result) is not MaintainerCommandResult or reference_history is not True:
        raise ReviewInputError("current result capacity data is unsupported")
    envelope = {
        "schema_version": V1,
        "intent_sha256": intent_sha256,
        "outcome": outcome,
        "result": asdict(result),
    }
    validate_action_result_envelope(envelope)
    size = len(canonical_bytes(envelope)) + formatter_growth_bound(
        call_limit=call_limit,
        source_review_id=source_review_id,
        approval_review_id=approval_review_id,
    )
    return raw_result_manifest_bound(
        envelope_bytes=size, binding=binding, reference_history=reference_history
    )


def raw_result_manifest_upper(*, envelope_bytes, binding, reference_history):
    """Inert maximum-width manifest shape, not decodable evidence or authority.

    The payload uses raw codec width, not favorable placeholder compression.
    This object is only for original root/comment renderer size checks. Never
    read it as a retained result, stage it, or pass it to an execution grant.
    """
    from review_sensei import action_result_codec, bounded_evidence

    require_module_seal(sys.modules[__name__])
    require_module_seal(action_result_codec)
    require_module_seal(bounded_evidence)
    if (
        type(envelope_bytes) is not int
        or not 0 < envelope_bytes <= bounded_evidence.MAX_PARTITION_DECODED_BYTES
    ):
        raise ReviewInputError("current result envelope size is unsupported")
    packed = (
        envelope_bytes
        + (envelope_bytes >> 12)
        + (envelope_bytes >> 14)
        + (envelope_bytes >> 25)
        + 13
    )
    carrier = {
        "schema_version": action_result_codec.V2
        if reference_history
        else action_result_codec.V1,
        "payload": {
            "encoding": "zlib-json-v1",
            "decoded_bytes": envelope_bytes,
            "sha256": "f" * 64,
            "data": "A" * (4 * ((packed + 2) // 3)),
        },
    }
    if reference_history:
        carrier[action_result_codec.COMMITMENT_KEY] = "f" * 64
    manifest = bounded_evidence.inline_manifest(carrier, binding=binding, item_count=1)
    if manifest is None:
        raise ReviewInputError(
            "current result raw carrier exceeds the original inline bound"
        )
    return manifest


def raw_result_manifest_bound(*, envelope_bytes, binding, reference_history):
    """Worst zlib/base64 wire, not favorable placeholder compression or authority."""
    return len(
        canonical_bytes(
            raw_result_manifest_upper(
                envelope_bytes=envelope_bytes,
                binding=binding,
                reference_history=reference_history,
            )
        )
    )


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
