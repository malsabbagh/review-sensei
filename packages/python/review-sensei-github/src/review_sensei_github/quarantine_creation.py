"""One-shot separate-root creation under the new run's unchanged startup slot.

The workflow owns PR serialization. No REST call is CAS; exact fences/readback
refuse conflicts. No method in this module PATCHes, completes or deletes the
predecessor. Stored and unstored lost acknowledgments never authorize a retry.
"""

from __future__ import annotations

import hashlib
import json
import weakref
from urllib.parse import quote

from review_sensei.api_work import initial_accounting
from review_sensei.bounded_evidence import (
    MAX_PART_BYTES,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import origin_scope, validate_origins
from review_sensei.quarantine_lineage import lineage_scope
from review_sensei.quarantine_state import assessment_cursor

from .api_run_owner import ApiRunActionsEvidence
from .api_run_plan import owner_allocations
from .errors import GitHubPublicationTransientError
from .http import MAX_PAGINATION_PAGES
from .quarantine_activation import CreationSlot, registered_activation
from .quarantine_evidence import (
    CurrentReadScope,
    read_published_source,
    reconstruct_failed_source,
)
from .quarantine_isolation import authenticate_isolated_predecessor
from .quarantine_origin import encode_predecessor, origin_binding, source_binding
from .quarantine_selection import select_quarantine
from .session_ledger import parse_session_comment

_ISSUED: weakref.WeakSet[PreparedQuarantine] = weakref.WeakSet()


class PreparedQuarantine:
    def __init__(
        self,
        *,
        ledger,
        identity,
        previous,
        slot,
        configuration,
        owner,
        scope,
        sources,
        isolation,
        actions_token,
    ):
        self.ledger, self.identity, self.previous, self.slot = (
            ledger,
            identity,
            previous,
            slot,
        )
        self.configuration, self.owner, self.scope = configuration, owner, scope
        self.sources = canonical_bytes(sources)
        self.isolation = isolation
        self.actions_token = actions_token
        self._consumed = False

    def __setattr__(self, name, value):
        if self in _ISSUED:
            raise ReviewInputError("issued quarantine preparation is immutable")
        object.__setattr__(self, name, value)

    def require(self):
        caller = self.ledger._startup_caller
        caller._require_ledger(self.ledger, self.identity)
        if (
            self not in _ISSUED
            or self._consumed
            or caller._state is not None
            or caller._reservation_started
        ):
            raise ReviewInputError("quarantine preparation is foreign or consumed")
        self.slot.require_process(
            http=self.ledger.http, identity=self.identity, run_id=caller.run.run_id
        )
        self.isolation.require_scope(
            http=self.ledger.http,
            identity=self.identity,
            previous=self.previous,
            predecessor_comment_id=self.slot.registration["predecessor_comment_id"],
            current_head=self.scope.head_sha,
        )


def prepare_quarantine(
    *,
    ledger,
    identity,
    previous,
    slot,
    configuration,
    owner,
    metadata_token,
    actions_token,
):
    caller = ledger._startup_caller
    caller._require_ledger(ledger, identity)
    if type(slot) is not CreationSlot or previous.quarantine_lineage is not None:
        raise ReviewInputError(
            "quarantine requires an issued creation slot and original predecessor"
        )
    slot.require_process(http=ledger.http, identity=identity, run_id=caller.run.run_id)
    registration = registered_activation(
        identity, slot.registration["predecessor_comment_id"]
    )
    if (
        registration != slot.registration
        or registration["predecessor_record_sha256"] != previous.record_sha256
        or previous.transaction is None
        or registration["transaction_id"] != previous.transaction.transaction_id
        or slot._context is None
        or configuration.commit != json.loads(slot._context)["policy_commit"]
        or owner.run_attempt != 1
        or owner.generation != 0
        or owner.job != caller.job
        or caller.run.run_id == previous.api_run_accounting["identity"]["run_id"]
    ):
        raise ReviewInputError("quarantine preparation binding conflicts")
    scope = CurrentReadScope(
        ledger,
        identity,
        registration["base_sha"],
        registration["head_sha"],
        metadata_token,
    )
    with scope.read_only_window():
        failed = None
        if previous.transaction.phase == "publication_succeeded":
            from .quarantine_evidence import read_published_tail_sources

            published = read_published_tail_sources(
                ledger=ledger, previous=previous, current_scope=scope
            )
            sources = []
            from review_sensei.session import SessionRecord

            for kind, (source, inventory) in published.items():
                sources.append(
                    {
                        "kind": kind,
                        "source": source,
                        "inventory": inventory,
                        "binding": source_binding(
                            SessionRecord.from_dict(source["record"]),
                            ledger.evidence_producer(),
                        ),
                    }
                )
            result = None
        else:
            failed, inventory, result = reconstruct_failed_source(
                ledger=ledger, previous=previous, current_scope=scope
            )
            sources = [
                {
                    "kind": "retained-result",
                    "source": failed,
                    "inventory": inventory,
                    "binding": source_binding(previous, ledger.evidence_producer()),
                }
            ]
            earlier = read_published_source(
                ledger=ledger, previous=previous, current_scope=scope
            )
            if earlier is not None:
                from review_sensei.session import SessionRecord

                sources.append(
                    {
                        "kind": "published-assessment",
                        "source": earlier[0],
                        "inventory": earlier[1],
                        "binding": source_binding(
                            SessionRecord.from_dict(earlier[0]["record"]),
                            ledger.evidence_producer(),
                        ),
                    }
                )
        isolation = authenticate_isolated_predecessor(
            actions=ApiRunActionsEvidence(
                ledger.http, token=actions_token, command_run_id=str(caller.run.run_id)
            ),
            metadata_token=metadata_token,
            identity=identity,
            previous=previous,
            retained_result=result,
            failed_source=failed,
            predecessor_comment_id=registration["predecessor_comment_id"],
            current_head=scope.head_sha,
        )
    prepared = PreparedQuarantine(
        ledger=ledger,
        identity=identity,
        previous=previous,
        slot=slot,
        configuration=configuration,
        owner=owner,
        scope=scope,
        sources=sources,
        isolation=isolation,
        actions_token=actions_token,
    )
    _ISSUED.add(prepared)
    prepared.require()
    return prepared


def _get(prepared, suffix, token):
    ledger, identity = prepared.ledger, prepared.identity
    status, value = ledger.http.request(
        "GET", ledger.http.repository_path(identity.repository, suffix), token=token
    )
    if status != 200 or not isinstance(value, dict):
        raise ReviewInputError("quarantine current authority evidence is unavailable")
    return value


def _fence(prepared, *, revalidate_slot=False):
    """Fresh current head/base, policy and numeric maintainer before each effect."""
    ledger, identity, slot = prepared.ledger, prepared.identity, prepared.slot
    caller = ledger._startup_caller
    slot.require_process(http=ledger.http, identity=identity, run_id=caller.run.run_id)
    if slot.registration != registered_activation(
        identity, slot.registration["predecessor_comment_id"]
    ):
        raise ReviewInputError("quarantine registration was revoked or changed")
    context = json.loads(slot._context)
    actor = context["actor"]
    if revalidate_slot:
        # This exact slot's fresh validation already reads and verifies the
        # same numeric maintainer. Keep one permission read in this pre-effect
        # fence, followed by the independent current policy/head checks.
        slot.revalidate(
            http=ledger.http,
            identity=identity,
            run_id=caller.run.run_id,
            token=prepared.actions_token,
            metadata_token=prepared.scope.metadata_token,
            policy_commit=prepared.configuration.commit,
        )
    else:
        permission = _get(
            prepared,
            f"/collaborators/{actor}/permission",
            prepared.scope.metadata_token,
        )
        person = permission.get("user")
        role = permission.get("permission")
        if role == "write" and permission.get("role_name") == "maintain":
            role = "maintain"
        if (
            role not in {"admin", "maintain"}
            or not isinstance(person, dict)
            or person.get("id") != context["event"]["sender"]["id"]
            or type(person.get("id")) is not int
            or person.get("type") != "User"
            or person.get("login") != actor
        ):
            raise ReviewInputError("quarantine maintainer authority was revoked")
    # Read the same authenticated default-branch ref selected by bootstrap.
    branch = getattr(ledger, "_quarantine_policy_branch", None)
    if not isinstance(branch, str) or not branch:
        raise ReviewInputError(
            "quarantine trusted default policy branch is unavailable"
        )
    reference = _get(
        prepared,
        "/git/ref/heads/" + quote(branch, safe="/"),
        prepared.scope.metadata_token,
    )
    obj = reference.get("object")
    if (
        not isinstance(obj, dict)
        or obj.get("type") != "commit"
        or obj.get("sha") != prepared.configuration.commit
    ):
        raise ReviewInputError("quarantine current policy changed")
    prepared.scope.require()


def _complete_comments(ledger, identity):
    ledger._inventory_complete = False
    ledger._list_refresh = True
    return ledger._bounded_comments(identity)


def _body(part):
    return (
        "ReviewSensei immutable evidence part v1\n```json\n"
        + canonical_bytes(part).decode()
        + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
    )


def _stage(prepared, document, binding, count):
    ledger, identity = prepared.ledger, prepared.identity
    parts = partition_evidence(document, binding=binding, item_count=count)
    bodies = [_body(part) for part in parts]
    if any(len(body.encode()) > MAX_PART_BYTES for body in bodies):
        raise ReviewInputError("quarantine immutable carrier exceeds its bound")
    items = ledger._cached_comments()
    retained = [
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("body"), str)
        and item["body"].endswith("<!-- reviewsensei:evidence-part:v1 -->")
    ]
    from .quarantine_published_tail import late_growth

    reserve = late_growth(prepared.isolation.receipt)
    if (
        len(retained) + len(parts) + reserve["evidence_parts"] > MAX_STORED_PARTS
        or sum(len(item["body"].encode()) for item in retained)
        + sum(len(b.encode()) for b in bodies)
        + reserve["evidence_bytes"]
        > MAX_STORED_PART_BYTES
    ):
        raise ReviewInputError("quarantine complete retained storage capacity exceeded")
    ids = []
    for body in bodies:
        # Do not restore a previous process's grants from an orphan part. The
        # positive first-execution slot remains mandatory even for identical IO.
        _fence(prepared)
        try:
            status, payload = ledger._request(
                "POST", ledger._comments_path(identity), body={"body": body}
            )
        except GitHubPublicationTransientError:
            status, payload = 0, None
        if status == 201 and isinstance(payload, dict):
            ledger._part_owner(payload, identity)
            if payload.get("body") != body or type(payload.get("id")) is not int:
                raise ReviewInputError("quarantine immutable create response conflicts")
            identifier = str(payload["id"])
            if payload["id"] < 1 or len(identifier) > 19:
                raise ReviewInputError(
                    "quarantine returned carrier identity exceeds its planned bound"
                )
            ledger._merge_comments([payload])
        elif status in {0, 202, 409, 422, 429} or status >= 500:
            raise ReviewInputError(
                "quarantine immutable create remains uncertain; stop before root creation and never retry"
            )
        else:
            raise ReviewInputError("quarantine immutable create failed")
        ids.append(identifier)
    return evidence_manifest(
        document, binding=binding, item_count=count, parts=parts, storage_ids=tuple(ids)
    )


def _archive(prepared, origins):
    ledger, identity, old, slot = (
        prepared.ledger,
        prepared.identity,
        prepared.previous,
        prepared.slot,
    )
    caller = ledger._startup_caller
    archive = {
        "schema_version": (
            "quarantine-published-origin-v1"
            if old.transaction.phase == "publication_succeeded"
            else "quarantine-origin-v1"
        ),
        "predecessor_comment_id": slot.registration["predecessor_comment_id"],
        "predecessor_record": encode_predecessor(old),
        "predecessor_record_representation": "zlib-json-v1",
        "creation_run_id": caller.run.run_id,
        "creation_policy_commit": prepared.configuration.commit,
        "creation_configuration_digest": prepared.configuration.digest,
        "registration_sha256": hashlib.sha256(
            canonical_bytes(slot.registration)
        ).hexdigest(),
        "target_isolation": prepared.isolation.receipt,
        "assessment_origins": validate_origins(
            {
                "schema_version": (
                    "quarantine-published-origins-v1"
                    if old.transaction.phase == "publication_succeeded"
                    else "quarantine-assessment-origins-v1"
                ),
                "origins": origins,
            }
        ),
    }
    binding = origin_binding(
        identity,
        slot.registration,
        old,
        producer=ledger.evidence_producer(),
        configuration_digest=prepared.configuration.digest,
    )
    return archive, binding


def _candidate(prepared, origin_manifest, origin_count):
    ledger, _identity, old, slot = (
        prepared.ledger,
        prepared.identity,
        prepared.previous,
        prepared.slot,
    )
    caller = ledger._startup_caller
    binding = origin_manifest["binding"]
    lineage = {
        "schema_version": (
            "quarantine-published-tail-v1"
            if old.transaction.phase == "publication_succeeded"
            else "quarantine-lineage-v1"
        ),
        "predecessor_comment_id": slot.registration["predecessor_comment_id"],
        "predecessor_record_sha256": old.record_sha256,
        "creation_run_id": caller.run.run_id,
        "registration_sha256": hashlib.sha256(
            canonical_bytes(slot.registration)
        ).hexdigest(),
        "origin_count": origin_count,
        "origin_manifest": origin_manifest,
    }
    lineage["scope_sha256"] = lineage_scope(lineage)
    lineage["assessment_state_manifest"] = inline_manifest(
        assessment_cursor(scope=lineage["scope_sha256"]),
        binding={**binding, "policy_digest": lineage["scope_sha256"]},
        item_count=0,
    )
    allocation = owner_allocations(prepared.configuration.limit, caller.plan.liability)
    state = (
        initial_accounting(
            caller.run,
            prepared.configuration,
            prepared.owner,
            caller.plan,
            prior_nonstartup_liability_upper_bound=0,
        )
        .reserve("hosted-review", allocation.first_work)
        .reserve("hosted-terminal", allocation.first_terminal)
    )
    record = old.quarantine_api_accounting(state.to_document(), lineage)
    return record


def require_prepared_sources(prepared, *, ledger, record):
    """An issued immutable process proof, never a serialized cache grant."""
    if (
        type(prepared) is not PreparedQuarantine
        or prepared not in _ISSUED
        or not prepared._consumed
        or prepared.ledger is not ledger
    ):
        raise ReviewInputError("quarantine prepared source proof is unavailable")
    caller = ledger._startup_caller
    caller._require_ledger(ledger, prepared.identity)
    prepared.slot.require_process(
        http=ledger.http, identity=prepared.identity, run_id=caller.run.run_id
    )
    if (
        caller._state is not None
        or not caller._reservation_started
        or record.quarantine_lineage["predecessor_record_sha256"]
        != prepared.previous.record_sha256
        or record.quarantine_lineage["creation_run_id"] != caller.run.run_id
    ):
        raise ReviewInputError("quarantine prepared source proof binding conflicts")
    prepared.isolation.require_scope(
        http=ledger.http,
        identity=prepared.identity,
        previous=prepared.previous,
        predecessor_comment_id=prepared.slot.registration["predecessor_comment_id"],
        current_head=prepared.scope.head_sha,
    )
    return json.loads(prepared.sources)


def _roots(ledger, identity, items):
    from .session_ledger import _marker_shaped

    roots = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("body"), str)
            or not _marker_shaped(item["body"])
        ):
            continue
        user = item.get("user")
        if (
            not isinstance(user, dict)
            or user.get("type") != "Bot"
            or not isinstance(user.get("login"), str)
            or user["login"].casefold() != ledger.app_slug.casefold()
        ):
            continue
        ledger._part_owner(item, identity)
        record = parse_session_comment(item["body"], identity=identity)
        if record is None or type(item.get("id")) is not int or item["id"] < 1:
            raise ReviewInputError("quarantine root marker is malformed or unsupported")
        roots.append((item["id"], record))
    return roots


def plan_quarantine(prepared):
    """Complete conservative plan before the first carrier POST.

    Unknown returned storage IDs use their maximum supported 19-digit width.
    The aggregate archive uses zlib's documented compressBound upper bound
    (https://github.com/madler/zlib/blob/v1.3.1/compress.c), then the existing
    23,500-byte codec chunk size. It never assumes an aggregate single part.
    """
    ledger = prepared.ledger
    sources = json.loads(prepared.sources)
    origins, source_parts, wire_bytes = [], 0, 0
    next_id = 1_000_000_000_000_000_000

    def manifest(document, binding, count):
        nonlocal next_id, source_parts, wire_bytes
        parts = partition_evidence(document, binding=binding, item_count=count)
        ids = tuple(str(next_id + i) for i in range(len(parts)))
        next_id += len(parts)
        source_parts += len(parts)
        wire_bytes += sum(len(_body(part).encode()) for part in parts)
        return evidence_manifest(
            document, binding=binding, item_count=count, parts=parts, storage_ids=ids
        )

    for item in sources:
        source = manifest(item["source"], item["binding"], 1)
        count = len(item["inventory"]["assessments"])
        inventory = inline_manifest(
            item["inventory"], binding=item["binding"], item_count=count
        )
        if inventory is None:
            inventory = manifest(item["inventory"], item["binding"], count)
        origin = {
            "schema_version": "quarantine-assessment-origin-v1",
            "kind": item["kind"],
            "source_manifest": source,
            "inventory_manifest": inventory,
        }
        origin["scope_sha256"] = origin_scope(origin)
        origins.append(origin)
    origins.sort(key=lambda item: item["scope_sha256"])
    archive, binding = _archive(prepared, origins)
    size = len(canonical_bytes(archive))
    upper = size + (size >> 12) + (size >> 14) + (size >> 25) + 13
    origin_parts = (upper + 23_499) // 23_500
    # Closed maximum-width manifest: all mutable-width fields are bounded before
    # rendering the initial root. Actual hashes have exactly these lengths.
    maximum = {
        "encoding": "partitioned-json-v1",
        "binding": binding,
        "item_count": 1,
        "decoded_bytes": size,
        "sha256": "f" * 64,
        "encoded_bytes": origin_parts * MAX_PART_BYTES,
        "parts": [
            {
                "storage_id": str(next_id + i),
                "bytes": MAX_PART_BYTES - 512,
                "sha256": "f" * 64,
            }
            for i in range(origin_parts)
        ],
    }
    record = _candidate(prepared, maximum, len(origins))
    body = ledger._render_comment(
        repository_id=prepared.identity.repository_id,
        pull_request=prepared.identity.pull_request,
        record=record,
    )
    retained = [
        item
        for item in ledger._cached_comments()
        if isinstance(item, dict)
        and isinstance(item.get("body"), str)
        and item["body"].endswith("<!-- reviewsensei:evidence-part:v1 -->")
    ]
    parts = source_parts + origin_parts
    wire_bytes += origin_parts * MAX_PART_BYTES
    # One immutable event plus one normal terminal-run archive per individual
    # decision, and four reserved normal review/verification carriers. No hold
    # item, reason, old reservation or existing part is removed to make room.
    obligations = sum(len(item["inventory"]["assessments"]) for item in sources)
    future_parts = 2 * obligations + 4
    future_bytes = future_parts * MAX_PART_BYTES
    stored_bytes = sum(len(item["body"].encode()) for item in retained)
    from .quarantine_published_tail import late_growth

    reserve = late_growth(prepared.isolation.receipt)
    if (
        len(retained) + parts + future_parts + reserve["evidence_parts"]
        > MAX_STORED_PARTS
        or stored_bytes + wire_bytes + future_bytes + reserve["evidence_bytes"]
        > MAX_STORED_PART_BYTES
    ):
        raise ReviewInputError(
            "quarantine complete future retention capacity exceeded before staging"
        )
    from .quarantine_capacity import install_capacity

    native_tail = ledger._quarantine_native_comment_bounds
    tail = list(native_tail) if native_tail is not None else [65_536 + 1024] * 2
    comment_bounds = (
        [MAX_PART_BYTES] * parts + [len(json.dumps({"body": body}).encode())] + tail
    )
    install_capacity(
        ledger,
        prepared.identity,
        prepared.isolation.receipt,
        token=prepared.scope.metadata_token,
        future_parts=len(comment_bounds),
        future_bytes=sum(comment_bounds),
        comment_bounds=comment_bounds,
    )
    return {
        "parts": parts,
        "origin_parts": origin_parts,
        "wire_bytes_upper_bound": wire_bytes,
        "root_bytes_upper_bound": len(body.encode()),
        "late_growth_reserve": reserve,
        "future_parts": future_parts,
        "future_bytes_upper_bound": future_bytes,
        # Three current fences plus one POST per part; two complete bounded
        # listings, slot revalidation, final current fences and one root POST.
        "requests": 4 * parts + 2 * MAX_PAGINATION_PAGES + 6,
    }


def create_quarantine(prepared, *, actions_token):
    """Consume the issued process preparation and slot before one new-root POST."""
    if type(prepared) is not PreparedQuarantine:
        raise ReviewInputError("quarantine requires issued complete preparation")
    if actions_token != prepared.actions_token:
        raise ReviewInputError("quarantine creation Actions capability changed")
    prepared.require()
    ledger, identity, old, slot = (
        prepared.ledger,
        prepared.identity,
        prepared.previous,
        prepared.slot,
    )
    caller = ledger._startup_caller
    caller._reservation_started = True
    caller._producer = ledger.evidence_producer()
    object.__setattr__(prepared, "_consumed", True)
    sources = json.loads(prepared.sources)
    plan = plan_quarantine(prepared)
    ledger.evidence_budget.preflight(plan["requests"])
    origins = []
    for item in sources:
        manifest = _stage(prepared, item["source"], item["binding"], 1)
        count = len(item["inventory"]["assessments"])
        inventory = inline_manifest(
            item["inventory"], binding=item["binding"], item_count=count
        )
        if inventory is None:
            inventory = _stage(prepared, item["inventory"], item["binding"], count)
        origin = {
            "schema_version": "quarantine-assessment-origin-v1",
            "kind": item["kind"],
            "source_manifest": manifest,
            "inventory_manifest": inventory,
        }
        origin["scope_sha256"] = origin_scope(origin)
        origins.append(origin)
    origins.sort(key=lambda item: item["scope_sha256"])
    archive, binding = _archive(prepared, origins)
    origin_manifest = _stage(prepared, archive, binding, 1)
    record = _candidate(prepared, origin_manifest, len(origins))
    caller._activation = record, "hosted-review"
    items = _complete_comments(ledger, identity)
    roots = _roots(ledger, identity, items)
    if (
        len(roots) != 1
        or roots[0][0] != slot.registration["predecessor_comment_id"]
        or roots[0][1].record_sha256 != old.record_sha256
    ):
        raise ReviewInputError(
            "quarantine original root changed or another successor exists"
        )
    ledger._part_owner(
        next(
            item
            for item in items
            if isinstance(item, dict) and item.get("id") == roots[0][0]
        ),
        identity,
    )
    body = ledger._render_comment(
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        record=record,
    )
    # Prepare once, then authenticate exact carrier bytes against that process
    # proof. The first later process independently reconstructs all sources.
    prior_reuse = ledger._reuse_listed_parts
    ledger._reuse_listed_parts = True
    ledger._quarantine_complete_read = True
    ledger._verified_parts = {}
    ledger._part_digests = {}
    try:
        reader = ledger._quarantine_origin_reader
        reader.prime_prepared(prepared, record, archive)
        reader.read(ledger, identity, record)
    finally:
        ledger._reuse_listed_parts = prior_reuse
        ledger._quarantine_complete_read = False
    _fence(prepared, revalidate_slot=True)
    ledger.evidence_budget.preflight(1 + MAX_PAGINATION_PAGES)
    slot.consume_creation(http=ledger.http, identity=identity, run_id=caller.run.run_id)
    try:
        status, payload = ledger._request(
            "POST", ledger._comments_path(identity), body={"body": body}
        )
    except GitHubPublicationTransientError:
        status, payload = 0, None
    if status not in {0, 200, 201, 202, 409, 422, 429} and status < 500:
        raise ReviewInputError(
            "quarantine new-root create failed; slot remains consumed"
        )
    items = _complete_comments(ledger, identity)
    roots = _roots(ledger, identity, items)
    selected = select_quarantine(ledger, identity, items, roots)
    if (
        selected is None
        or selected[1].record_sha256 != record.record_sha256
        or selected[0] == slot.registration["predecessor_comment_id"]
        or status in {200, 201}
        and (
            not isinstance(payload, dict)
            or type(payload.get("id")) is not int
            or payload["id"] != selected[0]
        )
    ):
        raise ReviewInputError(
            "quarantine new-root create remains unconfirmed; no retry"
        )
    caller._accept_activation(ledger, selected[1])
    caller.bind_terminal_reservation("hosted-terminal")
    return selected[1]
