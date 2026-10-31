"""Authenticate every immutable quarantine origin and complete decision state.

Installed registration, native numeric Bot ownership, the original creation
execution, immutable predecessor bytes and independently reconstructed context
form the chain. No dictionary, hash or diagnostic object grants a write.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from dataclasses import dataclass

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import (
    ENCODING,
    canonical_bytes,
    decode_evidence,
    encode_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import (
    pending_assessments,
    validate_inventory,
    validate_origins,
)
from review_sensei.quarantine_lineage import normalize_lineage
from review_sensei.quarantine_state import read_assessment_state
from review_sensei.session import MAX_SESSION_COMMENT_BYTES, SessionRecord

from .api_run_owner import ApiRunActionsEvidence
from .quarantine_activation import WORKFLOW_PATH, registered_activation
from .quarantine_evidence import read_published_source, reconstruct_failed_source
from .quarantine_isolation import authenticate_isolated_predecessor

_ARCHIVE_FIELDS = {
    "schema_version",
    "predecessor_comment_id",
    "predecessor_record",
    "predecessor_record_representation",
    "creation_run_id",
    "creation_policy_commit",
    "creation_configuration_digest",
    "registration_sha256",
    "target_isolation",
    "assessment_origins",
}


def encode_predecessor(record):
    """Lossless snapshot representation; no authority or accounting changes."""
    value = encode_evidence(
        record.to_dict(), max_decoded_bytes=MAX_SESSION_COMMENT_BYTES
    )
    # Both the encoded carrier and decoded record retain finite independent caps.
    decode_evidence(
        value,
        max_encoded_bytes=MAX_SESSION_COMMENT_BYTES,
        max_decoded_bytes=MAX_SESSION_COMMENT_BYTES,
    )
    return value


def read_predecessor(archive):
    if (
        not isinstance(archive, dict)
        or archive.get("predecessor_record_representation") != ENCODING
    ):
        raise ReviewInputError(
            "quarantine predecessor snapshot representation is unsupported"
        )
    value = decode_evidence(
        archive.get("predecessor_record"),
        max_encoded_bytes=MAX_SESSION_COMMENT_BYTES,
        max_decoded_bytes=MAX_SESSION_COMMENT_BYTES,
    )
    if not isinstance(value, dict):
        raise ReviewInputError("quarantine predecessor snapshot is not a record")
    return SessionRecord.from_dict(value)


def origin_binding(
    identity, registration, previous, *, producer, configuration_digest, base_sha=None
):
    base_sha = registration["base_sha"] if base_sha is None else base_sha
    if not isinstance(base_sha, str) or re.fullmatch(r"[a-f0-9]{40}", base_sha) is None:
        raise ReviewInputError("quarantine origin has no resolved base")
    return {
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "pull_request": identity.pull_request,
        "base_sha": base_sha,
        "head_sha": registration["head_sha"],
        "policy_digest": hashlib.sha256(canonical_bytes(registration)).hexdigest(),
        "configuration_digest": configuration_digest,
        "generation": previous.generation + 1,
        "producer": producer,
        "purpose": "evidence",
        "schema_version": "1.0",
    }


def source_binding(record, producer):
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    transaction = record.transaction
    if transaction is None:
        raise ReviewInputError("quarantine source transaction is absent")
    return {
        "repository": record.repository,
        "repository_id": record.repository_id,
        "pull_request": record.pull_request,
        "base_sha": transaction.base_sha,
        "head_sha": transaction.head_sha,
        "policy_digest": transaction.policy_digest,
        "configuration_digest": state.configuration.digest,
        "generation": record.generation,
        "producer": producer,
        "purpose": "evidence",
        "schema_version": "1.0",
    }


_AUTHENTICATED: weakref.WeakSet[AuthenticatedQuarantine] = weakref.WeakSet()


@dataclass(frozen=True, eq=False)
class AuthenticatedQuarantine:
    record_sha256: str
    lineage_bytes: bytes
    archive_bytes: bytes
    inventories_bytes: bytes
    state_bytes: bytes
    source_documents_bytes: bytes = b""
    original_caller: object = None
    original_identity: object = None

    @property
    def lineage(self):
        return json.loads(self.lineage_bytes)

    @property
    def archive(self):
        return json.loads(self.archive_bytes)

    @property
    def inventories(self):
        return json.loads(self.inventories_bytes)

    @property
    def state(self):
        return json.loads(self.state_bytes)

    @property
    def pending(self):
        return pending_assessments(
            self.archive["assessment_origins"],
            self.inventories,
            self.state,
            scope=self.lineage["scope_sha256"],
        )


class QuarantineOriginReader:
    def __init__(
        self, *, metadata_token, actions_token, current_scope, policy_commit=None
    ):
        self.metadata_token, self.actions_token = metadata_token, actions_token
        self.current_scope = current_scope
        self.policy_commit = policy_commit
        self._authenticated_sources: dict[tuple[str, str], bytes] = {}

    def prime_prepared(self, prepared, record, archive):
        """Reuse this process's independently authenticated original sources.

        Called only after complete owned carrier readback and fresh creation
        fences. This is neither dictionary authority nor a cross-process cache.
        The ordinary read still verifies every carrier, origin binding and state.
        """
        from .quarantine_creation import require_prepared_sources

        if (
            prepared.slot.publication_scope is not None
            and archive.get("publication_scope") != prepared.slot.publication_scope
        ):
            raise ReviewInputError("quarantine prepared publication scope changed")

        sources = require_prepared_sources(
            prepared, ledger=prepared.ledger, record=record
        )
        scope = self.current_scope
        accounting = ApiRunAccounting.from_document(record.api_run_accounting)
        if (
            getattr(prepared.ledger, "_quarantine_origin_reader", None) is not self
            or scope.ledger is not prepared.ledger
            or scope.identity != prepared.identity
            or scope.base_sha != prepared.scope.base_sha
            or scope.head_sha != prepared.scope.head_sha
            or scope.metadata_token != prepared.scope.metadata_token
            or self.metadata_token != prepared.scope.metadata_token
            or self.actions_token != prepared.actions_token
            or accounting.identity != prepared.ledger._startup_caller.run
            or accounting.configuration != prepared.configuration
            or accounting.owner != prepared.owner
            or accounting.startup != prepared.ledger._startup_caller.plan
            or archive["creation_run_id"] != accounting.identity.run_id
            or archive["creation_policy_commit"] != prepared.configuration.commit
            or archive["creation_configuration_digest"] != prepared.configuration.digest
            or archive["registration_sha256"]
            != record.quarantine_lineage["registration_sha256"]
        ):
            raise ReviewInputError(
                "quarantine prepared creation, reader or ledger scope changed"
            )
        if (
            read_predecessor(archive).to_dict() != prepared.previous.to_dict()
            or archive["target_isolation"] != prepared.isolation.receipt
        ):
            raise ReviewInputError("quarantine prepared immutable predecessor changed")
        origins = validate_origins(archive["assessment_origins"])
        by_kind = {item["kind"]: item for item in sources}
        if set(by_kind) != {origin["kind"] for origin in origins["origins"]}:
            raise ReviewInputError("quarantine prepared origins changed")
        inventories, source_documents = {}, {}
        for origin in origins["origins"]:
            item = by_kind[origin["kind"]]
            if (
                origin["source_manifest"]["binding"] != item["binding"]
                or origin["inventory_manifest"]["binding"] != item["binding"]
            ):
                raise ReviewInputError("quarantine prepared origin binding changed")
            inventories[origin["scope_sha256"]] = item["inventory"]
            source_documents[origin["kind"]] = item["source"]
        key = (
            record.quarantine_lineage["origin_manifest"]["sha256"],
            record.quarantine_lineage["scope_sha256"],
        )
        self._authenticated_sources[key] = canonical_bytes(
            {"sources": source_documents, "inventories": inventories}
        )

    def _creator(self, ledger, identity, archive, registration):
        def get(suffix, token):
            status, value = ledger.http.request(
                "GET",
                ledger.http.repository_path(identity.repository, suffix),
                token=token,
            )
            if status != 200 or not isinstance(value, dict):
                raise ReviewInputError("quarantine creation source is unavailable")
            return value

        # Read the original attempt positively. A later rerun cannot replace the
        # registered creation attempt, even if it changes latest-run metadata.
        run = get(
            f"/actions/runs/{archive['creation_run_id']}/attempts/1", self.actions_token
        )
        repository = run.get("repository")
        actor, trigger = run.get("actor"), run.get("triggering_actor")
        if (
            type(run.get("id")) is not int
            or run["id"] != archive["creation_run_id"]
            or not isinstance(repository, dict)
            or type(repository.get("id")) is not int
            or repository["id"] != identity.repository_id
            or repository.get("full_name") != identity.repository
            or type(run.get("workflow_id")) is not int
            or run["workflow_id"] != registration["workflow_id"]
            or type(run.get("run_number")) is not int
            or run["run_number"] != registration["run_number"]
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] != 1
            or run.get("event") != "workflow_dispatch"
            or run.get("path") != WORKFLOW_PATH
            or run.get("head_sha") != archive["creation_policy_commit"]
            or run.get("status") not in {"in_progress", "completed"}
            or not isinstance(actor, dict)
            or not isinstance(trigger, dict)
            or actor.get("type") != "User"
            or trigger.get("type") != "User"
            or type(actor.get("id")) is not int
            or actor["id"] < 1
            or type(trigger.get("id")) is not int
            or trigger["id"] != actor["id"]
            or not isinstance(actor.get("login"), str)
            or actor.get("login") != trigger.get("login")
        ):
            raise ReviewInputError("quarantine registered creation execution conflicts")
        source = get(
            f"/contents/{WORKFLOW_PATH}?ref={archive['creation_policy_commit']}",
            self.metadata_token,
        )
        if (
            source.get("type") != "file"
            or source.get("sha") != registration["workflow_blob"]
        ):
            raise ReviewInputError(
                "quarantine registered creation workflow source changed"
            )
        if registration["schema_version"] == "quarantine-published-activation-v3":
            from .quarantine_publication import authenticate_publication

            proof = authenticate_publication(
                http=ledger.http,
                token=self.metadata_token,
                intent=registration["publication"],
                source=archive["creation_policy_commit"],
            )
            proof.require_before(run.get("created_at"))
            if (
                proof.receipt != archive.get("publication_scope")
                or self.policy_commit != proof.receipt["source_commit"]
                or self.current_scope.base_sha != proof.receipt["source_commit"]
                or self.current_scope.head_sha != registration["head_sha"]
            ):
                raise ReviewInputError(
                    "quarantine original publication/live scope conflicts"
                )

    def read(self, ledger, identity, record):
        lineage = normalize_lineage(record.quarantine_lineage)
        if lineage is None or record.identity() != identity:
            raise ReviewInputError("quarantine current record scope is absent")
        registration = registered_activation(
            identity, lineage["predecessor_comment_id"]
        )
        registration_digest = hashlib.sha256(canonical_bytes(registration)).hexdigest()
        if registration_digest != lineage["registration_sha256"]:
            raise ReviewInputError("quarantine installed activation scope changed")
        manifest = lineage["origin_manifest"]
        archive = ledger.read_evidence(
            identity, manifest, expected_binding=manifest["binding"]
        )
        if (
            not isinstance(archive, dict)
            or set(archive)
            != _ARCHIVE_FIELDS
            | (
                {"publication_scope"}
                if registration["schema_version"]
                == "quarantine-published-activation-v3"
                else set()
            )
            or archive["schema_version"]
            != (
                "quarantine-published-origin-v1"
                if lineage["schema_version"] == "quarantine-published-tail-v1"
                else "quarantine-origin-v1"
            )
            or type(archive["predecessor_comment_id"]) is not int
            or not 1 <= archive["predecessor_comment_id"] <= 9_007_199_254_740_991
            or type(archive["creation_run_id"]) is not int
            or not 1 <= archive["creation_run_id"] <= 9_007_199_254_740_991
            or not isinstance(archive["creation_policy_commit"], str)
            or re.fullmatch(r"[a-f0-9]{40}", archive["creation_policy_commit"]) is None
            or not isinstance(archive["creation_configuration_digest"], str)
            or re.fullmatch(r"[a-f0-9]{64}", archive["creation_configuration_digest"])
            is None
        ):
            raise ReviewInputError("quarantine immutable origin is unsupported")
        previous = read_predecessor(archive)
        old = ApiRunAccounting.from_document(previous.api_run_accounting)
        current_accounting = ApiRunAccounting.from_document(record.api_run_accounting)
        if current_accounting.identity.run_id == archive["creation_run_id"] and (
            current_accounting.configuration.commit != archive["creation_policy_commit"]
            or current_accounting.configuration.digest
            != archive["creation_configuration_digest"]
        ):
            raise ReviewInputError(
                "quarantine creation accounting configuration conflicts"
            )
        if (
            previous.identity() != identity
            or previous.transaction is None
            or previous.record_sha256 != registration["predecessor_record_sha256"]
            or previous.record_sha256 != lineage["predecessor_record_sha256"]
            or previous.transaction.transaction_id != registration["transaction_id"]
            or archive["predecessor_comment_id"] != lineage["predecessor_comment_id"]
            or archive["creation_run_id"] != lineage["creation_run_id"]
            or type(archive["creation_run_id"]) is not int
            or archive["creation_run_id"] == old.identity.run_id
            or archive["registration_sha256"] != registration_digest
            or manifest["binding"]
            != origin_binding(
                identity,
                registration,
                previous,
                producer=ledger.evidence_producer(),
                configuration_digest=archive["creation_configuration_digest"],
                base_sha=archive["creation_policy_commit"]
                if registration["schema_version"]
                == "quarantine-published-activation-v3"
                else None,
            )
            or record.created_at != previous.created_at
            or record.expires_at != previous.expires_at
            or record.completed_initial_reviews < previous.completed_initial_reviews
            or record.completed_verification_rounds
            < previous.completed_verification_rounds
        ):
            raise ReviewInputError(
                "quarantine predecessor, creation or preserved session scope conflicts"
            )
        origins = validate_origins(archive["assessment_origins"])
        if len(origins["origins"]) != lineage["origin_count"]:
            raise ReviewInputError("quarantine assessment origin count conflicts")
        cache_key = manifest["sha256"], lineage["scope_sha256"]
        cached = self._authenticated_sources.get(cache_key)
        inventories = {}
        sources = {}
        for origin in origins["origins"]:
            source = ledger.read_evidence(
                identity,
                origin["source_manifest"],
                expected_binding=origin["source_manifest"]["binding"],
            )
            inventory = ledger.read_evidence(
                identity,
                origin["inventory_manifest"],
                expected_binding=origin["inventory_manifest"]["binding"],
            )
            inventories[origin["scope_sha256"]] = validate_inventory(inventory)
            sources[origin["kind"]] = source
        source_snapshot = canonical_bytes(
            {"sources": sources, "inventories": inventories}
        )
        if lineage["schema_version"] == "quarantine-published-tail-v1":
            from .quarantine_evidence import read_published_tail_sources

            if origins["schema_version"] != "quarantine-published-origins-v1" or set(
                sources
            ) != {"published-current", "published-earlier"}:
                raise ReviewInputError(
                    "published-tail immutable origin union conflicts"
                )
            for origin in origins["origins"]:
                source = sources[origin["kind"]]
                if not isinstance(source, dict) or not isinstance(
                    source.get("record"), dict
                ):
                    raise ReviewInputError("published-tail source record is absent")
                if origin["source_manifest"]["binding"] != source_binding(
                    SessionRecord.from_dict(source["record"]),
                    ledger.evidence_producer(),
                ):
                    raise ReviewInputError(
                        "published-tail original source binding conflicts"
                    )
            if cached != source_snapshot:
                with self.current_scope.read_only_window():
                    self._creator(ledger, identity, archive, registration)
                    published = read_published_tail_sources(
                        ledger=ledger,
                        previous=previous,
                        current_scope=self.current_scope,
                        retained_sources=sources,
                    )
                    caller = getattr(ledger, "caller", None) or getattr(
                        ledger, "_startup_caller", None
                    )
                    if caller is None:
                        raise ReviewInputError(
                            "published-tail reader has no current lifecycle"
                        )
                    isolation = authenticate_isolated_predecessor(
                        actions=ApiRunActionsEvidence(
                            ledger.http,
                            token=self.actions_token,
                            command_run_id=str(caller.run.run_id),
                        ),
                        metadata_token=self.metadata_token,
                        identity=identity,
                        previous=previous,
                        retained_result=None,
                        predecessor_comment_id=lineage["predecessor_comment_id"],
                        current_head=self.current_scope.head_sha,
                        creation_run_id=archive["creation_run_id"],
                    )
                    if isolation.receipt != archive["target_isolation"]:
                        raise ReviewInputError(
                            "published-tail isolation contract changed"
                        )
                    for origin in origins["origins"]:
                        expected_source, expected_inventory = published[origin["kind"]]
                        if (
                            expected_source != sources[origin["kind"]]
                            or inventories[origin["scope_sha256"]] != expected_inventory
                        ):
                            raise ReviewInputError(
                                "published-tail assessment membership conflicts"
                            )
                self._authenticated_sources[cache_key] = source_snapshot
        else:
            failed = sources.get("retained-result")
            if (
                not isinstance(failed, dict)
                or set(failed)
                != {
                    "schema_version",
                    "predecessor_record_sha256",
                    "retained_result",
                    "original_context_source",
                    "original_admission_context",
                }
                or failed["schema_version"] != "quarantine-failed-result-source-v1"
                or failed["predecessor_record_sha256"] != previous.record_sha256
            ):
                raise ReviewInputError("quarantine failed-result source is unsupported")
            if "published-assessment" in sources:
                published = sources["published-assessment"]
                if (
                    not isinstance(published, dict)
                    or set(published)
                    != {"schema_version", "record", "review_id", "review_body"}
                    or published["schema_version"]
                    != "quarantine-published-assessment-source-v1"
                ):
                    raise ReviewInputError("quarantine published source is unsupported")
            for origin in origins["origins"]:
                source_record = (
                    previous
                    if origin["kind"] == "retained-result"
                    else SessionRecord.from_dict(
                        sources["published-assessment"]["record"]
                    )
                )
                if origin["source_manifest"]["binding"] != source_binding(
                    source_record, ledger.evidence_producer()
                ):
                    raise ReviewInputError(
                        "quarantine original source binding conflicts"
                    )
            if cached != source_snapshot:
                with self.current_scope.read_only_window():
                    self.current_scope.require()
                    self._creator(ledger, identity, archive, registration)
                    reconstructed, expected_inventory, result = (
                        reconstruct_failed_source(
                            ledger=ledger,
                            previous=previous,
                            current_scope=self.current_scope,
                            retained_result=failed["retained_result"],
                        )
                    )
                    if canonical_bytes(reconstructed) != canonical_bytes(failed):
                        raise ReviewInputError(
                            "quarantine original context source changed"
                        )
                    caller = getattr(ledger, "caller", None) or getattr(
                        ledger, "_startup_caller", None
                    )
                    if caller is None:
                        raise ReviewInputError(
                            "quarantine reader has no current lifecycle"
                        )
                    actions = ApiRunActionsEvidence(
                        ledger.http,
                        token=self.actions_token,
                        command_run_id=str(caller.run.run_id),
                        job=old.owner.job,
                    )
                    isolation = authenticate_isolated_predecessor(
                        actions=actions,
                        metadata_token=self.metadata_token,
                        identity=identity,
                        previous=previous,
                        retained_result=result,
                        failed_source=reconstructed,
                        predecessor_comment_id=lineage["predecessor_comment_id"],
                        current_head=self.current_scope.head_sha,
                        creation_run_id=archive["creation_run_id"],
                    )
                    if isolation.receipt != archive["target_isolation"]:
                        raise ReviewInputError(
                            "quarantine source-bound target isolation changed"
                        )
                    earlier = read_published_source(
                        ledger=ledger,
                        previous=previous,
                        current_scope=self.current_scope,
                        retained_source=sources.get("published-assessment"),
                    )
                    expected = {"retained-result": expected_inventory}
                    if earlier is not None:
                        expected["published-assessment"] = earlier[1]
                    if set(sources) != set(expected):
                        raise ReviewInputError(
                            "quarantine assessment origins are incomplete"
                        )
                    for origin in origins["origins"]:
                        source_record = (
                            previous
                            if origin["kind"] == "retained-result"
                            else SessionRecord.from_dict(
                                sources["published-assessment"]["record"]
                            )
                        )
                        if (
                            origin["source_manifest"]["binding"]
                            != source_binding(source_record, ledger.evidence_producer())
                            or inventories[origin["scope_sha256"]]
                            != expected[origin["kind"]]
                        ):
                            raise ReviewInputError(
                                "quarantine original assessment membership conflicts"
                            )
                    self.current_scope.require()
                self._authenticated_sources[cache_key] = source_snapshot
        state_manifest = lineage["assessment_state_manifest"]
        binding = state_manifest["binding"]
        if (
            binding["producer"] != ledger.evidence_producer()
            or binding["policy_digest"] != lineage["scope_sha256"]
            or binding["generation"] > record.generation
        ):
            raise ReviewInputError(
                "quarantine assessment current state scope conflicts"
            )
        state_document = ledger.read_evidence(
            identity, state_manifest, expected_binding=binding
        )
        state = read_assessment_state(
            state_document,
            expected_binding=binding,
            reader=lambda identifier: ledger._read_part(identity, identifier),
            budget=ledger.evidence_budget,
        )
        if len(state["settlements"]) != state_manifest["item_count"]:
            raise ReviewInputError(
                "quarantine assessment complete state count conflicts"
            )
        pending_assessments(origins, inventories, state, scope=lineage["scope_sha256"])
        from .caller_operation import caller_for_http

        authenticated = AuthenticatedQuarantine(
            record.record_sha256,
            canonical_bytes(lineage),
            canonical_bytes(archive),
            canonical_bytes(inventories),
            canonical_bytes(state),
            canonical_bytes(sources),
            caller_for_http(ledger.http),
            identity,
        )
        _AUTHENTICATED.add(authenticated)
        from .quarantine_native_scope import bind_native_scope

        bind_native_scope(ledger, identity, authenticated)
        return authenticated
