"""Closed historical intermediate-command execution custody.

This issuer owns a single original invocation. It never changes a native guard's
complete-tail flag, lifetime allocation plan, funding or protected reservations.
"""

import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Any, cast
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import (
    require_module_seal,
    require_original_class_identity,
)


@dataclass(frozen=True, eq=False)
class HistoricalLease:
    pass


@dataclass(frozen=True, eq=False)
class LifecycleContext:
    pass


def _callerless_classifier():
    # This only declines an optional Action-run hook. It cannot issue a lease,
    # read receipt, source waiver or funding authority to these generic ledgers.
    seal = require_module_seal
    classify = require_original_class_identity
    module = sys.modules[__name__]

    def original_callerless_ledger(value):
        seal(module)
        for module_name, name in (
            ("review_sensei.session", "LocalSessionLedger"),
            ("review_sensei_github.session_ledger", "GitHubIssueCommentSessionLedger"),
        ):
            defining = sys.modules.get(module_name)
            if defining is None:
                continue
            try:
                classify(value, module=defining, name=name)
            except ReviewInputError:
                continue
            return True
        return False

    return original_callerless_ledger


_original_callerless_ledger = _callerless_classifier()
del _callerless_classifier


def _require_default_profile(caller):
    """Use the original installed profile reader, never a dynamic substitute."""
    from . import api_run_requests, trust_effects

    require_module_seal(api_run_requests)
    original = api_run_requests._original_trust_verification_profile
    if trust_effects.verification_profile is not original:
        raise ReviewInputError("historical trust profile source changed")
    if original(caller) is not None:
        raise ReviewInputError("historical closed trust profile is unsupported")


def _registry():
    active: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    leases: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
    contexts: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    def require(caller):
        require_module_seal(sys.modules[__name__])
        lease = active.get(caller)
        if lease is None:
            return None
        entry = leases.get(lease)
        from . import quarantine_command_receipt as receipt_source

        require_module_seal(receipt_source)
        from .quarantine_command_receipt import _projection_binding

        if (
            entry is not None
            and _projection_binding(entry["page"]) != entry["page_binding"]
        ):
            raise ReviewInputError("historical receipt projection changed")
        _require_default_profile(caller)
        from .quarantine_capacity import installed_capacity
        from .quarantine_command_execution import _configuration, _execution_owner
        from .quarantine_command_successor import require_current
        from .quarantine_lifecycle_transfer import _target_binding

        if (
            entry is None
            or entry["caller"] is not caller
            or _configuration(caller.accounting.configuration) != entry["configuration"]
            or _execution_owner(caller.accounting.owner) != entry["owner"]
            or caller._block
            != (
                entry["ordinary_block"]
                if caller._terminal_reason is None
                else entry["terminal_block"]
            )
            or caller._terminal_block != entry["terminal_block"]
            or caller.accounting.remaining != entry["remaining"]
            or caller.accounting.prior_nonstartup_liability != entry["prior"]
            or canonical_bytes(caller.accounting.action["intent"]) != entry["intent"]
            or installed_capacity(entry["ledger"], entry["identity"])
            is not entry["guard"]
            or canonical_bytes(entry["authority"].document) != entry["authority_bytes"]
            or _target_binding(entry["target"]) != entry["target_binding"]
            or tuple(entry["guard"].comment_plan.unknown) != entry["initial_unknown"]
            or entry["guard"].complete_tail
            or entry["guard"].comment_plan.worst_read_calls()
            > json.loads(entry["recipe"])["comment_reads"]
        ):
            raise ReviewInputError("historical closed invocation owner changed")
        if caller._terminal_reason is not None:
            from . import api_run_requests
            from .quarantine_command_phase import _optional_frame

            terminal_frame = _optional_frame(
                api_run_requests, api_run_requests.ApiRunRequestTransport.close_action
            )
            if (
                caller._terminal_reason != "action-completion"
                or terminal_frame is None
                or terminal_frame.f_locals.get("self") is not caller
                or terminal_frame.f_locals.get("proof")
                is not caller._action_result_proof
            ):
                raise ReviewInputError(
                    "historical terminal entry lacks original settled proof"
                )
        review_certificate = entry["guard"].certificates.get("reviews", {})
        review_calls = review_certificate.get("physical_reads_upper_bound")
        if (
            review_certificate.get("reader") != "bounded-cold-reviews"
            or type(review_calls) is not int
            or not 1 <= review_calls <= json.loads(entry["recipe"])["review_reads"]
        ):
            raise ReviewInputError("historical complete review reader changed")
        pending = frozenset(
            (b.block_id, p.request_id, p.deadline_unix_ms)
            for b in caller.accounting.blocks
            for p in b.pending
        )
        if not entry["unknowns"].issubset(pending):
            raise ReviewInputError("historical physical unknown debit disappeared")
        if entry["witness"] is not None:
            require_current(entry["witness"], caller, entry["ledger"])
        return entry

    def activate(proposal, run, entry):
        from . import quarantine_command_suffix
        from .quarantine_command_execution import _configuration, _execution_owner
        from .quarantine_command_successor import start_guarded

        require_module_seal(quarantine_command_suffix)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_command_suffix.require_execution.__code__
            or frame.f_globals is not vars(quarantine_command_suffix)
            or cast(Any, frame.f_locals).get("token") is not proposal
            or cast(Any, frame.f_locals).get("run") is not run
            or cast(Any, frame.f_locals).get("entry") is not entry
            or run.caller in active
        ):
            raise ReviewInputError(
                "historical lease requires original closed preparation"
            )
        _require_default_profile(run.caller)
        recipe = json.loads(entry["recipe"])
        measured = recipe["root_measurement"]
        from . import quarantine_command_receipt as receipt_source

        require_module_seal(receipt_source)
        receipt_source._projection_binding(entry["page"])
        page_reads = 2 if entry["page"].pages else 1
        terminal_projection = measured.get("terminal_projection")
        if terminal_projection is not None:
            from . import historical_terminal_projection

            require_module_seal(historical_terminal_projection)
            historical_terminal_projection.require_activation(run, terminal_projection)
        if (
            not measured["all_growth_roots_fit"]
            or not all(
                row["original_validated_and_rendered"]
                for row in measured["growth_roots"]
            )
            or (
                recipe.get("contract"),
                recipe["comment_reads"],
                recipe["review_reads"],
                recipe.get("receipt_reads"),
                recipe["combined_loss_calls"],
            )
            not in {
                ("closed-inline-c4-reserved-verification-v1", 4, 3, 1, 105),
                ("closed-inline-c5-original-no-verification-v1", 5, 3, 1, 105),
                ("closed-inline-c5-r2-p2-no-verification-v1", 5, 2, 2, 104),
            }
            or recipe.get("ordinary_required") != 105
            or recipe.get("receipt_reads") != page_reads
            or (page_reads == 2 and recipe["source_review_reads"] != 2)
            or (recipe["comment_reads"] == 5 and not recipe.get("local_headroom"))
            or run.caller.budget_view.ordinary_remaining() < 105
            or any(block.pending for block in run.caller.accounting.blocks)
        ):
            raise ReviewInputError("historical closed source profile is unsupported")
        state = run.caller.accounting
        terminal = next(
            b for b in state.blocks if b.block_id == run.caller._terminal_block
        )
        if terminal.undispatched < 50 or state.prior_nonstartup_liability:
            raise ReviewInputError("historical protected terminal tail is unavailable")
        lease = HistoricalLease()
        saved = dict(
            caller=run.caller,
            ledger=run.ledger,
            identity=run.identity,
            run=run,
            authority=entry["authority"],
            authority_bytes=entry["authority_bytes"],
            target=entry["target"],
            target_binding=entry["binding"],
            guard=entry["guard"],
            configuration=_configuration(state.configuration),
            owner=_execution_owner(state.owner),
            ordinary_block=run.caller._block,
            terminal_block=run.caller._terminal_block,
            remaining=state.remaining,
            prior=state.prior_nonstartup_liability,
            intent=canonical_bytes(state.action["intent"]),
            recipe=entry["recipe"],
            captured=entry["captured"],
            page=entry["page"],
            page_binding=entry["page_binding"],
            witness=None,
            journal=None,
            ordinary=0,
            terminal=0,
            phase=0,
            start_sequence=run.caller._sequence,
            initial_unknown=tuple(entry["plan"].unknown),
            unknowns=frozenset(),
        )
        leases[lease] = saved
        active[run.caller] = lease
        try:
            saved["witness"] = start_guarded(run, entry["target"], lease)
            require(run.caller)
            from . import historical_invocation_census

            require_module_seal(historical_invocation_census)
            historical_invocation_census.issue(lease, saved)
            from . import historical_review_census

            require_module_seal(historical_review_census)
            historical_review_census.issue(lease, saved)
            from . import historical_terminal_fence

            require_module_seal(historical_terminal_fence)
            historical_terminal_fence.start(run, lease)
            if terminal_projection is not None:
                historical_terminal_fence.require_active(run, lease)
        except BaseException:
            active.pop(run.caller, None)
            leases.pop(lease, None)
            raise
        return lease

    def require_closed(lease, run):
        if (
            active.get(run.caller) is not lease
            or leases.get(lease, {}).get("run") is not run
        ):
            raise ReviewInputError("historical lease is copied or foreign")
        require(run.caller)

    def available(caller, ledger):
        entry = require(caller)
        if entry is None:
            return False
        if entry["ledger"] is not ledger:
            raise ReviewInputError("historical lease ledger changed")
        return True

    def before_request(caller):
        entry = require(caller)
        if entry is None:
            return
        from . import quarantine_command_namespace
        from .quarantine_command_phase import classify_original_mutation

        require_module_seal(quarantine_command_namespace)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_command_namespace.before_sequence.__code__
            or frame.f_globals is not vars(quarantine_command_namespace)
            or cast(Any, frame.f_locals).get("caller") is not caller
        ):
            raise ReviewInputError("historical lease request is not original pre-debit")
        # Pin only at the genuine pre-debit seam. A negative proof query made
        # during an in-flight successful HTTP request must not misclassify that
        # current reservation as an older physical unknown.
        entry["unknowns"] = frozenset(
            (b.block_id, p.request_id, p.deadline_unix_ms)
            for b in caller.accounting.blocks
            for p in b.pending
        )
        terminal = caller._terminal_reason is not None
        key, ceiling = ("terminal", 50) if terminal else ("ordinary", 105)
        if entry[key] >= ceiling:
            raise ReviewInputError(
                "historical source suffix has no remaining physical calls"
            )
        description = classify_original_mutation(caller)
        if description is not None:
            recipe = json.loads(entry["recipe"])
            phase = description.checkpoint_phase or description.kind
            expected = (
                "entry",
                "event",
                "decision-cursor",
                "journal-pending",
                "review",
                "journal-finish",
                "receipt",
                "terminal",
            )
            if entry["phase"] >= len(expected) or phase != expected[entry["phase"]]:
                raise ReviewInputError(
                    "historical closed source mutation order changed"
                )
            if phase == "review":
                method, path, body = recipe["review"]
                if (
                    description.method != method
                    or description.path != path
                    or description.body_sha256
                    != hashlib.sha256(canonical_bytes(body)).hexdigest()
                ):
                    raise ReviewInputError(
                        "historical native update differs from its closed plan"
                    )
            if phase == "receipt":
                if (
                    description.method != "POST"
                    or description.body_sha256
                    != hashlib.sha256(
                        canonical_bytes({"body": recipe["receipt"]})
                    ).hexdigest()
                ):
                    raise ReviewInputError(
                        "historical receipt differs from its source-rendered plan"
                    )
            if phase == "event":
                original = cast(Any, frame.f_back)
                request = original.f_locals["request"]
                body = json.loads(request.data)
                if (
                    body != {"body": recipe["event_exact_body"]}
                    or len(canonical_bytes(body)) > recipe["event_wire_bound"]
                ):
                    raise ReviewInputError(
                        "historical event exceeds its promised wire bound"
                    )
            if phase in {
                "entry",
                "decision-cursor",
                "journal-pending",
                "journal-finish",
                "terminal",
            }:
                candidate = caller._checkpoint
                maximum = max(
                    row["terminal_root_bytes"]
                    if phase == "terminal"
                    else row["ordinary_root_bytes"]
                    for row in recipe["root_measurement"]["growth_roots"]
                )
                if (
                    candidate is None
                    or len(canonical_bytes(candidate.to_dict())) > maximum
                ):
                    raise ReviewInputError(
                        "historical checkpoint exceeds its complete root projection"
                    )
            entry["phase"] += 1
        entry[key] += 1

    def bind_journal(journal):
        if _original_callerless_ledger(journal.ledger):
            return False
        entry = require(journal.ledger.caller)
        if entry is None:
            return False
        from . import application
        from .quarantine_command_execution import _source_frame

        frame = _source_frame(
            application, application.GitHubApplication._refinalize_command
        )
        if (
            cast(Any, frame.f_locals).get("journal") is not journal
            or cast(Any, frame.f_locals).get("ledger") is not entry["ledger"]
            or cast(Any, frame.f_locals).get("record") is not journal._record
            or journal._record is not entry["caller"]._effect_record
            or entry["phase"] != 3
            or entry["journal"] not in (None, journal)
        ):
            raise ReviewInputError(
                "historical journal is not the own confirmed successor"
            )
        if entry["journal"] is None:
            entry["journal_effects"] = canonical_bytes(journal.effects)
        entry["journal"] = journal
        return True

    def require_journal(journal):
        if _original_callerless_ledger(journal.ledger):
            return False
        entry = require(journal.ledger.caller)
        return entry is not None and entry["journal"] is journal

    def retained_prefix_source(journal):
        """Read exact inline bytes under the existing lease, without HTTP.

        The mutable journal list and its confirmed flags are not provenance.
        Reconstruct the selected complete carrier independently and match the
        prefix captured at the original own-successor restore boundary.
        """
        from review_sensei import finding_lifecycle_store, session
        from review_sensei.source_registry import require_instance_source

        from . import finding_lifecycle, historical_prefix_sizing, review_run

        require_module_seal(historical_prefix_sizing)
        source = sys._getframe(1)
        if (
            source.f_code
            is not historical_prefix_sizing.retained_for_candidate.__code__
            or source.f_globals is not vars(historical_prefix_sizing)
            or source.f_locals.get("journal") is not journal
        ):
            raise ReviewInputError("historical prefix requires original sizing source")
        own = object.__getattribute__(journal, "__dict__")
        if type(own) is not dict or any(type(key) is not str for key in own):
            raise ReviewInputError("historical prefix journal namespace changed")
        if type(own.get("ledger")) is not review_run.ActionRunLedger:
            return None
        _ledger, source_caller, _record = historical_prefix_sizing._owner_references(
            journal
        )
        if active.get(source_caller) is None:
            return None
        ledger, caller, record, record_snapshot = (
            historical_prefix_sizing._owned_inputs(journal)
        )
        # The snapshot rejects instance serializers and hostile nested values
        # before require() traverses the existing successor's retained record.
        entry = require(caller)
        if entry is None:
            return None
        if (
            entry["journal"] is not journal
            or entry["phase"] != 3
            or entry["ledger"] is not ledger
            or entry["caller"] is not caller
            or historical_prefix_sizing._identity_fields(journal.identity)
            != historical_prefix_sizing._identity_fields(entry["identity"])
            or record is not caller._effect_record
            or record.record_sha256 != caller._root
            or caller._checkpoint is not None
        ):
            raise ReviewInputError("historical retained prefix owner changed")
        reference = record.finding_lifecycle
        if reference is None or reference["manifest"]["encoding"] != "inline-json-v1":
            return None
        require_module_seal(session)
        require_module_seal(finding_lifecycle_store)
        require_module_seal(finding_lifecycle)
        require_instance_source(ledger, methods=("evidence_producer",))
        if historical_prefix_sizing._owned_inputs(journal)[
            3
        ] != record_snapshot or canonical_bytes(
            session.SessionRecord.from_dict(record.to_dict()).to_dict()
        ) != (canonical_bytes(record.to_dict())):
            raise ReviewInputError("historical retained prefix root changed")
        stored = finding_lifecycle_store._decode_selected_lifecycle(ledger, record)
        if (
            stored is None
            or stored.get("schema_version") == "finding-lifecycle-packed-v4"
        ):
            return None
        retained = finding_lifecycle._validate(
            finding_lifecycle._unpack_journal(stored)
        )
        if canonical_bytes(retained["effects"]) != entry[
            "journal_effects"
        ] or canonical_bytes(
            retained.get("guarded_publication_pair")
        ) != canonical_bytes(journal._guarded_publication_pair):
            raise ReviewInputError(
                "historical retained prefix differs from owned evidence"
            )
        return retained

    def receipt_preparation(run, authority, target):
        from . import quarantine_command_execution
        from .session_ledger import parse_session_comment

        require_module_seal(quarantine_command_execution)
        frame = sys._getframe(1)
        if (
            frame.f_code
            is not quarantine_command_execution.prepare_receipt_phase.__code__
            or frame.f_globals is not vars(quarantine_command_execution)
        ):
            raise ReviewInputError("historical receipt preparation source changed")
        entry = require(run.caller)
        if entry is None:
            return None
        if (
            entry["run"] is not run
            or entry["authority"] is not authority
            or entry["target"] is not target
        ):
            raise ReviewInputError("historical receipt preparation owner changed")
        rows = json.loads(entry["captured"]["rows"])
        roots = [
            row
            for row in rows
            if (parsed := parse_session_comment(row["body"], identity=run.identity))
            is not None
            and parsed.record_sha256 == run.caller._root
        ]
        if len(roots) != 1:
            raise ReviewInputError("historical receipt original root is ambiguous")
        from .quarantine_lifecycle_transfer import _target_binding

        return dict(
            run=run,
            authority=authority,
            target=target,
            target_binding=_target_binding(target),
            captured={**entry["captured"], "owner": run.caller.accounting.owner},
            rows=rows,
            root_id=roots[0]["id"],
            projection=entry["page"],
            projection_binding=entry["page_binding"],
            authority_bytes=entry["authority_bytes"],
            successor_witness=entry["witness"],
        )

    def observe_census(ledger, captured):
        from . import quarantine_command_suffix

        require_module_seal(quarantine_command_suffix)
        frame = sys._getframe(1)
        if (
            frame.f_code is not quarantine_command_suffix.complete_census.__code__
            or frame.f_globals is not vars(quarantine_command_suffix)
        ):
            raise ReviewInputError("historical receipt census source changed")
        entry = require(ledger.caller)
        if entry is None:
            return
        if (
            cast(Any, frame.f_locals).get("ledger") is not ledger
            or cast(Any, frame.f_locals)["completed"].get(ledger) is not captured
        ):
            raise ReviewInputError("historical receipt census custody changed")
        entry["final_census"] = {**captured, "owner": ledger.caller.accounting.owner}

    def take_final_census(run):
        from . import quarantine_command_execution

        require_module_seal(quarantine_command_execution)
        frame = sys._getframe(1)
        if (
            frame.f_code
            is not quarantine_command_execution.require_receipt_phase.__code__
            or frame.f_globals is not vars(quarantine_command_execution)
        ):
            raise ReviewInputError("historical final receipt source changed")
        entry = require(run.caller)
        if entry is None:
            return None
        return entry.pop("final_census", None)

    def lifecycle_execution_context(ledger, identity, *, known, added_parts):
        entry = require(ledger.caller)
        if entry is None:
            return None
        from . import finding_lifecycle, quarantine_capacity
        from .quarantine_command_phase import _optional_frame

        require_module_seal(quarantine_capacity)
        source = sys._getframe(1)
        values = cast(Any, source.f_locals)
        if (
            source.f_code
            is not quarantine_capacity._historical_lifecycle_execution.__code__
            or source.f_globals is not vars(quarantine_capacity)
            or values.get("ledger") is not ledger
            or values.get("identity") is not identity
            or values.get("known") is not known
            or values.get("added_parts") != added_parts
        ):
            raise ReviewInputError(
                "historical lifecycle context requires original extension"
            )
        checkpoint = _optional_frame(
            finding_lifecycle, finding_lifecycle.FindingLifecycleJournal.checkpoint
        )
        preflight = _optional_frame(
            finding_lifecycle,
            finding_lifecycle.FindingLifecycleJournal._preflight_effect,
        )
        frame = checkpoint if checkpoint is not None else preflight
        if (
            entry["ledger"] is not ledger
            or entry["identity"] != identity
            or added_parts != 0
            or frame is None
            or frame.f_locals.get("self") is not entry["journal"]
            or entry["phase"] not in (3, 5)
        ):
            raise ReviewInputError(
                "historical lifecycle prefix is outside its closed inline plan"
            )
        journal = entry["journal"]
        initial = json.loads(entry["journal_effects"])
        planned = json.loads(entry["recipe"])["review"]
        request = finding_lifecycle._request_identity(planned[2])
        key = finding_lifecycle._digest(
            {"method": planned[0], "path": planned[1], "request": request}
        )
        if (
            len(journal.effects) != len(initial) + 1
            or canonical_bytes(journal.effects[:-1]) != entry["journal_effects"]
            or journal.effects[-1]["request_sha256"] != key
            or journal.effects[-1]["request"] != request
            or journal.effects[-1]["method"] != planned[0]
            or journal.effects[-1]["path"] != planned[1]
            or "review_update_precondition" in journal.effects[-1]
            or journal._body_receipts(int(planned[1].rsplit("/", 1)[-1]))
        ):
            raise ReviewInputError(
                "historical lifecycle contains an unquoted native effect"
            )
        if checkpoint is None:
            request_frame = _optional_frame(
                finding_lifecycle, finding_lifecycle.FindingLifecycleJournal.request
            )
            if (
                request_frame is None
                or request_frame.f_locals.get("self") is not journal
                or preflight is None
                or preflight.f_locals.get("capacity_only") is not False
                or request_frame.f_locals.get("method") != planned[0]
                or request_frame.f_locals.get("path") != planned[1]
                or canonical_bytes(request_frame.f_locals.get("body"))
                != canonical_bytes(planned[2])
            ):
                raise ReviewInputError("historical native preflight source changed")
        token = LifecycleContext()
        contexts[token] = (
            entry,
            ledger.caller._root,
            tuple(known.items()),
            added_parts,
        )
        return token

    def consume_lifecycle_execution_context(
        token, proof, operation, *, known, added_parts
    ):
        from . import quarantine_capacity

        require_module_seal(quarantine_capacity)
        frame = sys._getframe(1)
        values = cast(Any, frame.f_locals)
        if (
            frame.f_code
            is not quarantine_capacity.activate_lifecycle_extension.__code__
            or frame.f_globals is not vars(quarantine_capacity)
            or values.get("proof") is not proof
            or values.get("operation") is not operation
        ):
            raise ReviewInputError(
                "historical lifecycle consumption requires original writer"
            )
        saved = contexts.pop(token, None) if type(token) is LifecycleContext else None
        if saved is None:
            raise ReviewInputError("historical lifecycle context is copied or consumed")
        entry, root, expected, parts = saved
        if (
            require(entry["caller"]) is not entry
            or proof.ledger is not entry["ledger"]
            or entry["caller"]._root != root
            or tuple(known.items()) != expected
            or added_parts != parts
            or proof.plan.worst_read_calls()
            > json.loads(entry["recipe"])["comment_reads"]
            or tuple(proof.plan.unknown) != entry["initial_unknown"]
            or operation is not entry["caller"]._active_operation
        ):
            raise ReviewInputError("historical lifecycle execution context changed")

    return (
        activate,
        require_closed,
        available,
        before_request,
        bind_journal,
        require_journal,
        retained_prefix_source,
        lifecycle_execution_context,
        consume_lifecycle_execution_context,
        receipt_preparation,
        observe_census,
        take_final_census,
    )


(
    activate,
    require_closed,
    available,
    before_request,
    bind_journal,
    require_journal,
    retained_prefix_source,
    lifecycle_execution_context,
    consume_lifecycle_execution_context,
    receipt_preparation,
    observe_census,
    take_final_census,
) = _registry()
del _registry

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
