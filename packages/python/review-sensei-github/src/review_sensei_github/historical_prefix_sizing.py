"""Retained and source-fixed bytes for detached sizing, never authority.

Only the original detached observer or the existing historical execution lease
can establish an immutable prefix. Confirmed-looking mutable journal objects
alone are deliberately insufficient. The detached observer may also prove the
fixed compact receipts of its selected PUT/PATCH sequence; it cannot execute it.
Unsupported scopes keep generic sizing.
"""

import json
import sys

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import (
    require_instance_source,
    require_module_seal,
    require_original_class_identity,
)


def _owner_references(journal):
    """Authenticate exact classes and plain namespaces before following fields."""
    from . import (
        api_run_requests,
        finding_lifecycle,
        review_run,
    )

    def namespace(value, module, name, methods):
        require_original_class_identity(value, module=module, name=name)
        raw = object.__getattribute__(value, "__dict__")
        if type(raw) is not dict or any(type(key) is not str for key in raw):
            raise ReviewInputError("retained prefix owner namespace changed")
        require_instance_source(value, methods=methods)
        return raw

    own = namespace(
        journal,
        finding_lifecycle,
        "FindingLifecycleJournal",
        ("_preflight_effect", "_document"),
    )
    ledger = own.get("ledger")
    ledger_fields = namespace(
        ledger, review_run, "ActionRunLedger", ("evidence_producer",)
    )
    caller = ledger_fields.get("caller")
    namespace(caller, api_run_requests, "ApiRunRequestTransport", ())
    record = own.get("_record")
    return ledger, caller, record


def _owned_inputs(journal):
    from . import historical_record_snapshot

    ledger, caller, record = _owner_references(journal)
    caller_fields = object.__getattribute__(caller, "__dict__")
    if record is not caller_fields.get("_effect_record"):
        raise ReviewInputError("retained prefix selected record changed")
    require_module_seal(historical_record_snapshot)
    raw = historical_record_snapshot._record_snapshot(record)
    root = caller_fields.get("_root")
    if type(root) is not str or raw["record_sha256"] != root:
        raise ReviewInputError("retained prefix selected root changed")
    identity = object.__getattribute__(journal, "__dict__").get("identity")
    if _identity_fields(identity) != (
        raw["repository"],
        raw["pull_request"],
        raw["repository_id"],
    ):
        raise ReviewInputError("retained prefix selected identity changed")
    return ledger, caller, record, canonical_bytes(raw)


def _identity_fields(value):
    from review_sensei import session

    require_original_class_identity(value, module=session, name="SessionIdentity")
    raw = object.__getattribute__(value, "__dict__")
    if (
        type(raw) is not dict
        or any(type(key) is not str for key in raw)
        or set(raw) != {"repository", "pull_request", "repository_id"}
        or type(raw["repository"]) is not str
        or type(raw["pull_request"]) is not int
        or (raw["repository_id"] is not None and type(raw["repository_id"]) is not int)
    ):
        raise ReviewInputError("retained prefix identity fields changed")
    return raw["repository"], raw["pull_request"], raw["repository_id"]


def _fixed_command_updates(retained, document, payload, producer, preconditions):
    """Match detached fixed-scalar updates; this DATA predicate grants nothing.

    A PUT of the selected review and PATCH of the selected check have fixed
    compact confirmations under the original response validators. A new check,
    approval, cleanup or variable native identity never belongs in this prefix.
    The caller must independently authenticate the original branch builder and
    observation before using the result for sizing.
    """
    from . import finding_lifecycle, finding_lifecycle_prefix

    require_module_seal(finding_lifecycle)
    require_module_seal(finding_lifecycle_prefix)
    retained, document, payload, preconditions = finding_lifecycle_prefix._plain(
        [retained, document, payload, preconditions]
    )
    if (
        type(producer) is not str
        or len(producer) > len("github-bot:") + 19
        or not producer.startswith("github-bot:")
        or not producer.split(":", 1)[1].isdecimal()
        or not 0 < int(producer.split(":", 1)[1]) <= 2**63 - 1
        or type(retained) is not dict
        or type(document) is not dict
        or type(payload) is not dict
        or type(preconditions) is not dict
        or payload.get("cleanup_requests") != []
    ):
        return False
    old, effects = retained.get("effects"), document.get("effects")
    if (
        type(old) is not list
        or type(effects) is not list
        or not 1 <= len(effects) - len(old) - 1 <= 2
        or effects[: len(old)] != old
    ):
        return False
    review, check, approval = (
        payload.get("review_update"),
        payload.get("check_request"),
        payload.get("conditional_approval"),
    )
    if any(type(r) is not list or len(r) != 3 for r in (review, check, approval)):
        return False
    assert review is not None and check is not None and approval is not None
    if review[0] != "PUT" or check[0] != "PATCH" or approval[0] != "POST":
        return False
    before = payload.get("review_before")
    commitment = preconditions.get("check")
    if (
        type(before) is not dict
        or type(before.get("id")) is not int
        or type(commitment) is not dict
        or type(commitment.get("check_run_id")) is not int
        or type(commitment.get("app_id")) is not int
        or any(
            not 0 < n <= 2**63 - 1
            for n in (before["id"], commitment["check_run_id"], commitment["app_id"])
        )
    ):
        return False
    repository, number = payload.get("repository"), payload.get("pull_request")
    if (
        type(repository) is not str
        or type(number) is not int
        or review[1] != f"/repos/{repository}/pulls/{number}/reviews/{before['id']}"
        or check[1] != f"/repos/{repository}/check-runs/{commitment['check_run_id']}"
        or approval[1] != f"/repos/{repository}/pulls/{number}/reviews"
        or commitment.get("head_sha") != payload.get("head_sha")
    ):
        return False
    confirmations = (
        dict(
            id=before["id"],
            producer_id=int(producer.split(":", 1)[1]),
            app_id=None,
            mutation=None,
        ),
        dict(
            id=commitment["check_run_id"],
            producer_id=None,
            app_id=commitment["app_id"],
            mutation=None,
        ),
    )
    for index, effect in enumerate(effects[len(old) : -1]):
        if type(effect) is not dict:
            return False
        request = (review, check)[index]
        identity = finding_lifecycle._request_identity(request[2])
        expected = dict(
            method=request[0],
            path=request[1],
            request=identity,
            request_sha256=finding_lifecycle._digest(
                dict(method=request[0], path=request[1], request=identity)
            ),
            status="confirmed",
            response=[200, confirmations[index]],
        )
        if index == 0 and "review_update_precondition" in effect:
            expected["review_update_precondition"] = preconditions.get("review")
        if index == 1:
            expected["check_update_precondition"] = commitment
        if effect != expected:
            return False
    # The next effect must be the next source-planned request, never another
    # operation appended after an approval with an unknown new native identity.
    next_request = (review, check, approval)[len(effects) - len(old) - 1]
    last = effects[-1]
    return (
        type(last) is dict
        and last.get("method") == next_request[0]
        and last.get("path") == next_request[1]
        and last.get("request") == finding_lifecycle._request_identity(next_request[2])
    )


def retained_for_candidate(journal, document):
    """Check existing custody before any known-prefix size optimization."""
    from . import finding_lifecycle

    require_module_seal(sys.modules[__name__])
    require_module_seal(finding_lifecycle)
    maximum = sys._getframe(1)
    preflight = maximum.f_back
    if (
        maximum.f_code is not finding_lifecycle._maximum_native_allocation.__code__
        or maximum.f_globals is not vars(finding_lifecycle)
        or maximum.f_locals.get("_pair_source") is not journal
        or maximum.f_locals.get("document") is not document
        or preflight is None
        or preflight.f_code
        is not finding_lifecycle.FindingLifecycleJournal._preflight_effect.__code__
        or preflight.f_globals is not vars(finding_lifecycle)
        or preflight.f_locals.get("self") is not journal
    ):
        raise ReviewInputError("retained prefix sizing requires original preflight")
    require_original_class_identity(
        journal, module=finding_lifecycle, name="FindingLifecycleJournal"
    )
    if type(document) is not dict or document.get("schema_version") != (
        "finding-lifecycle-journal-v3"
    ):
        return False
    effects = document.get("effects")
    if type(effects) is not list or not 4 <= len(effects) <= 32:
        return False
    outer = preflight.f_back
    retained = None
    fixed_update_context = None
    if (
        preflight.f_locals.get("capacity_only") is True
        and outer is not None
        and outer.f_code
        is finding_lifecycle.FindingLifecycleJournal._command_branch_documents.__code__
        and outer.f_globals is vars(finding_lifecycle)
        and outer.f_locals.get("shadow") is journal
    ):
        from . import quarantine_command_finalization

        require_module_seal(quarantine_command_finalization)
        measurement = outer.f_back
        if (
            measurement is None
            or measurement.f_code
            is not quarantine_command_finalization._measure_inline_branches.__code__
            or measurement.f_globals is not vars(quarantine_command_finalization)
            or measurement.f_locals.get("journal") is not outer.f_locals.get("self")
            or measurement.f_locals.get("plan") is not outer.f_locals.get("plan")
        ):
            return False
        ledger, caller, record, _raw = _owned_inputs(journal)
        measured = measurement.f_locals.get("saved")
        if type(measured) is not dict or measured.get("record") is not record:
            raise ReviewInputError("retained prefix original measurement root changed")
        saved = quarantine_command_finalization.require_observation(
            measurement.f_locals.get("observation")
        )
        if (
            saved is not measured
            or record is not saved["record"]
            or ledger is not saved["run"].ledger
            or caller is not saved["run"].caller
        ):
            raise ReviewInputError("retained prefix detached source changed")
        retained = json.loads(saved["document"])
        fixed_update_context = (
            measurement.f_locals["observation"],
            outer.f_locals["plan"],
            outer.f_locals["self"],
            ledger,
        )
    elif (
        preflight.f_locals.get("capacity_only") is False
        and outer is not None
        and outer.f_code is finding_lifecycle.FindingLifecycleJournal.request.__code__
        and outer.f_globals is vars(finding_lifecycle)
        and outer.f_locals.get("self") is journal
    ):
        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        retained = quarantine_historical_execution.retained_prefix_source(journal)
    if retained is None:
        return False
    # Synthetic confirmations are never called retained observations. Only the
    # detached original compiler may additionally use its fixed PUT/PATCH
    # response domains. Runtime custody remains exactly one new effect.
    if (
        type(retained) is not dict
        or retained.get("schema_version") != "finding-lifecycle-journal-v3"
        or type(retained.get("effects")) is not list
        or any(effect.get("status") != "confirmed" for effect in retained["effects"])
        or canonical_bytes(document.get("guarded_publication_pair"))
        != canonical_bytes(retained.get("guarded_publication_pair"))
    ):
        return False
    finding_lifecycle._validate(retained)
    if canonical_bytes(effects[:-1]) == canonical_bytes(retained["effects"]):
        return True
    if fixed_update_context is None:
        return False
    observation, source_plan, original, ledger = fixed_update_context
    from . import quarantine_command_finalization

    if observation is None or source_plan is None:
        raise ReviewInputError("fixed update sizing source plan is unavailable")
    if (
        quarantine_command_finalization._NATIVE_PLANS.get(observation)
        is not source_plan
        or not quarantine_command_finalization._observation_native_payload_match(
            observation, source_plan
        )
    ):
        raise ReviewInputError("fixed update sizing source plan changed")
    payload = json.loads(observation.payload)
    require_instance_source(
        original, methods=("_native_review_commitment", "_check_precondition")
    )
    check = payload.get("check_request")
    preconditions = {
        "review": original._native_review_commitment(payload.get("review_before")),
        "check": original._check_precondition(check[1])
        if type(check) is list and len(check) == 3
        else None,
    }
    return _fixed_command_updates(
        retained, document, payload, ledger.evidence_producer(), preconditions
    )


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
