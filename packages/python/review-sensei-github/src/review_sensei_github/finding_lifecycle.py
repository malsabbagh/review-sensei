"""Ordinary finding lifecycle checkpoints under selected session authority.

The transport journal records intent before dispatch. It never retries an unknown
write: only exact, owned native readback can discharge that intent. Tokens are
neither part of the document nor included in request identities.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from typing import Any

from review_sensei.bounded_evidence import (
    canonical_bytes,
    decode_evidence,
    encode_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle import (
    decode_lifecycle_state,
    encode_lifecycle_state,
)
from review_sensei.finding_lifecycle_store import _sizing_producer
from review_sensei.session import SessionIdentity

VERSION = "finding-lifecycle-journal-v1"
CURRENT_VERSION = "finding-lifecycle-journal-v2"
MAX_EFFECTS = 32
MAX_RECEIPT_BYTES = 512 * 1024


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


_ATOM_FIELDS = {"native_target", "target", "thread_id", "node_id"}
_NUMERIC_FIELDS = {"id", "review_id", "comment_id", "producer_id", "app_id"}


def _pack_journal(document):
    """Separate unknown native scalars from the deterministically packed state.

    Independent scalar allocation makes the future wire bound insensitive to
    how well an as-yet-unobserved GitHub ID happens to compress. These are
    representation indexes, never native identities or authority.
    """
    if len(canonical_bytes(document)) > 1_048_576:
        raise ReviewInputError("finding lifecycle complete byte capacity exceeded")
    atoms: list[list[Any]] = []
    aliases: dict[tuple[str, Any], int] = {}
    # A closed pair includes hashes and a PATCH path derived from a future
    # native ID. Independent slots keep their entropy AND equality relationships
    # outside the compressed skeleton. Existing v3 readers already support it.
    paired = document.get("schema_version") == "finding-lifecycle-journal-v3"

    variable_digests = {
        "request_sha256",
        "start_receipt_sha256",
        "complete_request_sha256",
        "start_request_sha256",
    }

    def visit(value, field=None):
        kind = None
        independent = paired and (
            field in variable_digests or field in {"path", "$response_status"}
        )
        if (
            value is not None
            and (
                field in _ATOM_FIELDS
                or (
                    paired
                    and field == "path"
                    and isinstance(value, str)
                    and re.fullmatch(r"/repos/[^/]+/[^/]+/check-runs/[0-9]+", value)
                )
            )
            and isinstance(value, str)
        ):
            if not value or not value.isprintable() or len(value.encode("utf-8")) > 256:
                raise ReviewInputError(
                    "finding lifecycle native identity exceeds supported wire bounds"
                )
            kind = "native"
        elif (
            value is not None
            and (
                field in _NUMERIC_FIELDS
                or (
                    paired
                    and (
                        field == "$response_status"
                        or (isinstance(field, str) and field.endswith("_id"))
                    )
                )
            )
            and type(value) is int
        ):
            if not 0 < value <= 9_223_372_036_854_775_807:
                raise ReviewInputError(
                    "finding lifecycle numeric identity exceeds supported wire bounds"
                )
            kind = "integer"
        elif (
            (
                field in {"proof_sha256", "target_proof_sha256", "body_sha256"}
                or (paired and field in variable_digests)
            )
            and isinstance(value, str)
            and re.fullmatch(r"[a-f0-9]{64}", value)
        ):
            kind = "digest"
        if kind is not None:
            alias = (kind, value)
            if not independent and kind != "integer" and alias in aliases:
                return {"$native": aliases[alias]}
            index = len(atoms)
            atoms.append([kind, value])
            if not independent:
                aliases[alias] = index
            return {"$native": index}
        if isinstance(value, dict):
            if "$native" in value:
                raise ReviewInputError("finding lifecycle reserved representation key")
            return {key: visit(value[key], key) for key in sorted(value)}
        if isinstance(value, (tuple, list)):
            return [
                visit(
                    item,
                    "$response_status"
                    if paired and field == "response" and index == 0
                    else None,
                )
                for index, item in enumerate(value)
            ]
        return value

    skeleton = visit(document)
    if len(atoms) > 8192:
        raise ReviewInputError("finding lifecycle scalar capacity exceeded")
    return {
        "schema_version": "finding-lifecycle-packed-v3",
        "journal": encode_evidence(skeleton, max_decoded_bytes=1_048_576),
        "native_atoms": atoms,
    }


def _thread_effect_target(effect):
    """Closed known mutation target; this does not authorize a native write."""
    if effect.get("path") != "/graphql" or effect.get("method") != "POST":
        return None
    request = effect.get("request")
    if not isinstance(request, dict):
        return None
    query, variables = request.get("query"), request.get("variables")
    if not isinstance(query, str) or not isinstance(variables, dict):
        return None
    if set(variables) == {"threadId"}:
        target = variables["threadId"]
    elif (
        set(variables) == {"input"}
        and isinstance(variables["input"], dict)
        and set(variables["input"]) == {"threadId"}
    ):
        target = variables["input"]["threadId"]
    else:
        return None
    if (
        not isinstance(target, str)
        or not target
        or not target.isprintable()
        or len(target.encode()) > 256
    ):
        return None
    names = re.findall(r"\b(?:unresolveReviewThread|resolveReviewThread)\b", query)
    if len(names) != 1:
        return None
    name = names[0]
    return name, target, name == "resolveReviewThread"


def _confirmed_thread_response_matches(effect):
    target = _thread_effect_target(effect)
    return target is not None and canonical_bytes(
        effect.get("response")
    ) == canonical_bytes(
        [
            200,
            {
                "id": None,
                "producer_id": None,
                "app_id": None,
                "mutation": {
                    "name": target[0],
                    "thread_id": target[1],
                    "resolved": target[2],
                },
            },
        ]
    )


def _closed_pair_path_bound(packed, journal):
    """One source-generated numeric PATCH path, never an arbitrary native ID."""
    import inspect

    from review_sensei.source_registry import (
        require_instance_source,
        require_module_seal,
    )

    from . import guarded_pending_publication

    frame = inspect.currentframe()
    source = frame.f_back.f_back if frame and frame.f_back else None
    if source is None:
        raise ReviewInputError("guarded path sizing source is unavailable")
    require_instance_source(journal, methods=("_preflight_effect",))
    if source.f_code is FindingLifecycleJournal._preflight_effect.__code__:
        valid = source.f_locals.get("self") is journal
    else:
        require_module_seal(guarded_pending_publication)
        valid = (
            source.f_code is guarded_pending_publication._three_checkpoints.__code__
            and source.f_locals.get("journal") is journal
        )
    if not valid:
        raise ReviewInputError("guarded path sizing source changed")
    document = _unpack_journal(packed)
    if document.get("schema_version") != "finding-lifecycle-journal-v3":
        return {}
    _require_effect_scope(document, journal.identity, journal.head_sha)
    from .guarded_publication_batch import validate_pair

    validate_pair(document["effects"], document["guarded_publication_pair"])
    complete_index = document["guarded_publication_pair"]["complete_index"]
    identifier = document["effects"][0]["response"][1]["id"]
    prefix = f"/repos/{journal.identity.repository}/check-runs/"
    if type(identifier) is not int or not 0 < identifier <= 9_223_372_036_854_775_807:
        raise ReviewInputError("guarded check ID exceeds its source domain")
    if document["effects"][complete_index]["path"] != prefix + str(identifier):
        raise ReviewInputError("guarded check path changed")
    skeleton = decode_evidence(
        packed["journal"], max_encoded_bytes=1_048_576, max_decoded_bytes=1_048_576
    )
    if not isinstance(skeleton, dict):
        raise ReviewInputError("guarded pair skeleton is not an object")
    reference = skeleton["effects"][complete_index]["path"]
    if not isinstance(reference, dict) or set(reference) != {"$native"}:
        return {}
    index = reference["$native"]
    count = 0

    def census(value):
        nonlocal count
        if isinstance(value, dict):
            if value == {"$native": index}:
                count += 1
            for child in value.values():
                census(child)
        elif isinstance(value, list):
            for child in value:
                census(child)

    census(skeleton)
    if count != 1 or packed["native_atoms"][index] != [
        "native",
        prefix + str(identifier),
    ]:
        raise ReviewInputError("guarded check path slot is aliased or changed")
    return {index: prefix + "9223372036854775807"}


def _maximum_native_allocation(packed, *, preserve_observed=False, _pair_source=None):
    """A sizing-only value, never passed to stage or installed as authority."""
    import copy

    path_bounds = (
        {} if _pair_source is None else _closed_pair_path_bound(packed, _pair_source)
    )
    result = copy.deepcopy(packed)
    seen: dict[tuple[str, Any], Any] = {}
    for index, atom in enumerate(result["native_atoms"]):
        if index in path_bounds:
            atom[1] = path_bounds[index]
            continue
        key = (atom[0], atom[1])
        if key not in seen:
            if atom[0] == "native" and (
                not preserve_observed or str(atom[1]).startswith("capacity_")
            ):
                # canonical_bytes uses ASCII escapes: printable two-byte BMP
                # scalars cost six JSON bytes each. This covers the full
                # 256-UTF8-byte ID domain, including quote/backslash/astral IDs.
                prefix = "".join(
                    "ê" if bit == "1" else "é" for bit in format(index, "016b")
                )
                seen[key] = prefix + "é" * (128 - len(prefix))
            elif atom[0] == "integer" and not preserve_observed:
                seen[key] = 9_223_372_036_854_775_807 - index
            else:
                seen[key] = atom[1]
        atom[1] = seen[key]
    return result


def _unpack_journal(stored):
    if not isinstance(stored, dict):
        raise ReviewInputError("finding lifecycle carrier schema is unsupported")
    if stored.get("schema_version") == "finding-lifecycle-packed-v1" and set(
        stored
    ) == {"schema_version", "journal"}:
        return decode_evidence(
            stored["journal"], max_encoded_bytes=1_048_576, max_decoded_bytes=1_048_576
        )
    if stored.get("schema_version") == "finding-lifecycle-packed-v2" and set(
        stored
    ) == {"schema_version", "journal", "native_atoms"}:
        old_atoms = stored["native_atoms"]
        if not isinstance(old_atoms, list) or any(
            not isinstance(a, dict) or set(a) != {"kind", "value"} for a in old_atoms
        ):
            raise ReviewInputError("finding lifecycle scalar schema is unsupported")
        stored = {
            **stored,
            "schema_version": "finding-lifecycle-packed-v3",
            "native_atoms": [[a["kind"], a["value"]] for a in old_atoms],
        }
    if (
        set(stored) != {"schema_version", "journal", "native_atoms"}
        or stored["schema_version"] != "finding-lifecycle-packed-v3"
    ):
        raise ReviewInputError("finding lifecycle carrier schema is unsupported")
    atoms = stored["native_atoms"]
    if not isinstance(atoms, list) or len(atoms) > 8192:
        raise ReviewInputError("finding lifecycle scalar capacity exceeded")
    expected = 0

    def visit(value):
        nonlocal expected
        if isinstance(value, dict) and set(value) == {"$native"}:
            index = value["$native"]
            if (
                type(index) is not int
                or index < 0
                or index > expected
                or index >= len(atoms)
            ):
                raise ReviewInputError("finding lifecycle scalar ordering conflicts")
            atom = atoms[index]
            if index == expected:
                expected += 1
            if not isinstance(atom, list) or len(atom) != 2:
                raise ReviewInputError("finding lifecycle scalar schema is unsupported")
            kind, item = atom
            if not isinstance(kind, str):
                raise ReviewInputError("finding lifecycle scalar kind is invalid")
            if kind == "integer":
                valid = type(item) is int and 0 < item <= 9_223_372_036_854_775_807
            elif kind == "native":
                valid = (
                    isinstance(item, str)
                    and bool(item)
                    and item.isprintable()
                    and len(item.encode("utf-8")) <= 256
                )
            else:
                pattern = {
                    "native": r"[A-Za-z0-9_:=-]{1,256}",
                    "digest": r"[a-f0-9]{64}",
                    "commit": r"[a-f0-9]{40}",
                }.get(kind)
                valid = (
                    pattern is not None
                    and isinstance(item, str)
                    and re.fullmatch(pattern, item) is not None
                )
            if not valid:
                raise ReviewInputError("finding lifecycle scalar bounds are invalid")
            return item
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    value = visit(
        decode_evidence(
            stored["journal"], max_encoded_bytes=1_048_576, max_decoded_bytes=1_048_576
        )
    )
    if expected != len(atoms):
        raise ReviewInputError("finding lifecycle scalar table has unused entries")
    if len(canonical_bytes(value)) > 1_048_576:
        raise ReviewInputError("finding lifecycle reconstructed byte capacity exceeded")
    if _pack_journal(value) != stored:
        raise ReviewInputError(
            "finding lifecycle scalar representation is noncanonical"
        )
    return value


def _request_identity(value):
    """Commit exact rendered prose by digest, without duplicating it in history.

    Full finding text remains in the lossless lifecycle codec. Unknown writes
    are reconciled against owned native bytes; this commitment never authorizes
    reconstructing or blindly sending a missing projection.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "body" and isinstance(item, str):
                raw = item.encode("utf-8")
                result[key] = {
                    "$body_sha256": hashlib.sha256(raw).hexdigest(),
                    "utf8_bytes": len(raw),
                }
            else:
                result[key] = _request_identity(item)
        return result
    if isinstance(value, list):
        return [_request_identity(item) for item in value]
    return value


def _body_matches(observed, expected):
    if isinstance(expected, str):
        return observed == expected  # Authenticated complete legacy intent.
    return (
        isinstance(observed, str)
        and isinstance(expected, dict)
        and set(expected) == {"$body_sha256", "utf8_bytes"}
        and type(expected["utf8_bytes"]) is int
        and len(observed.encode("utf-8")) == expected["utf8_bytes"]
        and hashlib.sha256(observed.encode("utf-8")).hexdigest()
        == expected["$body_sha256"]
    )


def _request_matches(observed, expected):
    return isinstance(observed, dict) and all(
        _body_matches(observed.get(key), item)
        if key == "body"
        else observed.get(key) == item
        for key, item in expected.items()
    )


def _validate(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "lifecycle",
        "effects",
        "unknown_trust_effect",
        "native_receipts",
    }
    if not isinstance(document, dict) or document.get("schema_version") not in {
        VERSION,
        CURRENT_VERSION,
        "finding-lifecycle-journal-v3",
    }:
        raise ReviewInputError("finding lifecycle journal schema is unsupported")
    if document["schema_version"] in {CURRENT_VERSION, "finding-lifecycle-journal-v3"}:
        fields.add("accepted_semantic_history")
    if document["schema_version"] == "finding-lifecycle-journal-v3":
        fields.add("guarded_publication_pair")
    if set(document) != fields:
        raise ReviewInputError("finding lifecycle journal schema is unsupported")
    history = document.get("accepted_semantic_history")
    if history is not None:
        from types import SimpleNamespace

        from .accepted_semantic_history import validate_history

        if not isinstance(history, dict):
            raise ReviewInputError("finding lifecycle semantic history is invalid")
        validate_history(
            history,
            SimpleNamespace(
                repository_id=history.get("repository_id"),
                pull_request=history.get("pull_request"),
            ),
        )
    decode_lifecycle_state(document["lifecycle"])
    effects = document["effects"]
    if not isinstance(effects, list) or len(effects) > MAX_EFFECTS:
        raise ReviewInputError("finding lifecycle effect capacity exceeded")
    pair = document.get("guarded_publication_pair")
    seen = set()
    pending = False
    for effect_index, effect in enumerate(effects):
        effect_fields = {
            "method",
            "path",
            "request",
            "request_sha256",
            "status",
            "response",
        }
        if isinstance(effect, dict) and "check_update_precondition" in effect:
            effect_fields.add("check_update_precondition")
            precondition = effect["check_update_precondition"]
            if (
                effect.get("method") != "PATCH"
                or not isinstance(precondition, dict)
                or set(precondition)
                != {"check_run_id", "head_sha", "app_id", "app_slug"}
                or any(
                    type(precondition.get(k)) is not int
                    or not 0 < precondition[k] <= 9_223_372_036_854_775_807
                    for k in ("check_run_id", "app_id")
                )
                or not isinstance(precondition.get("head_sha"), str)
                or re.fullmatch(r"[a-f0-9]{40}", precondition["head_sha"]) is None
                or not isinstance(precondition.get("app_slug"), str)
                or not re.fullmatch(r"[a-z0-9-]{1,100}", precondition["app_slug"])
            ):
                raise ReviewInputError(
                    "finding lifecycle check update precondition is invalid"
                )
        if isinstance(effect, dict) and "review_update_precondition" in effect:
            effect_fields.add("review_update_precondition")
            precondition = effect["review_update_precondition"]
            if (
                effect.get("method") != "PUT"
                or not isinstance(precondition, dict)
                or set(precondition)
                != {"review_id", "head_sha", "producer_id", "body_sha256"}
                or any(
                    type(precondition.get(k)) is not int
                    or not 0 < precondition[k] <= 9_223_372_036_854_775_807
                    for k in ("review_id", "producer_id")
                )
                or not isinstance(precondition.get("head_sha"), str)
                or re.fullmatch(r"[a-f0-9]{40}", precondition["head_sha"]) is None
                or not isinstance(precondition.get("body_sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", precondition["body_sha256"]) is None
            ):
                raise ReviewInputError(
                    "finding lifecycle review update precondition is invalid"
                )
        if not isinstance(effect, dict) or set(effect) != effect_fields:
            raise ReviewInputError("finding lifecycle effect schema is invalid")
        if effect["method"] not in {"POST", "PATCH", "PUT", "DELETE"} or not isinstance(
            effect["path"], str
        ):
            raise ReviewInputError("finding lifecycle effect target is invalid")
        if not isinstance(effect["request"], dict) or effect[
            "request_sha256"
        ] != _digest(
            {
                "method": effect["method"],
                "path": effect["path"],
                "request": effect["request"],
            }
        ):
            raise ReviewInputError("finding lifecycle intent integrity failed")

        def check_bodies(value):
            if isinstance(value, dict):
                if "$body_sha256" in value:
                    if (
                        set(value) != {"$body_sha256", "utf8_bytes"}
                        or not isinstance(value["$body_sha256"], str)
                        or re.fullmatch(r"[a-f0-9]{64}", value["$body_sha256"]) is None
                        or type(value["utf8_bytes"]) is not int
                        or not 0 <= value["utf8_bytes"] <= 512 * 1024
                    ):
                        raise ReviewInputError(
                            "finding lifecycle exact body commitment is invalid"
                        )
                else:
                    for item in value.values():
                        check_bodies(item)
            elif isinstance(value, list):
                for item in value:
                    check_bodies(item)

        check_bodies(effect["request"])
        if effect["request_sha256"] in seen or (
            pending and not (pair is not None and effect_index == 2)
        ):
            raise ReviewInputError("finding lifecycle effects are contradictory")
        seen.add(effect["request_sha256"])
        if effect["status"] == "pending":
            if effect["response"] is not None:
                raise ReviewInputError(
                    "pending finding effect has a fabricated receipt"
                )
            pending = True
        elif effect["status"] in {"confirmed", "rejected"}:
            response = effect["response"]
            if (
                not isinstance(response, list)
                or len(response) != 2
                or type(response[0]) is not int
            ):
                raise ReviewInputError("finding lifecycle receipt is invalid")
            if len(canonical_bytes(response[1])) > MAX_RECEIPT_BYTES:
                raise ReviewInputError(
                    "finding lifecycle receipt exceeds response capacity"
                )
        else:
            raise ReviewInputError("finding lifecycle effect status is invalid")
    if document["schema_version"] == "finding-lifecycle-journal-v3":
        from .guarded_publication_batch import validate_pair

        validate_pair(effects, pair)
    receipts = document["native_receipts"]
    receipt_fields = {
        "schema_version",
        "repository_id",
        "pull_request",
        "head_sha",
        "base_sha",
        "producer_id",
        "review_id",
        "instance",
        "body_sha256",
        "thread_id",
        "comment_id",
        "native_head_sha",
    }
    if not isinstance(receipts, list) or len(receipts) > 200:
        raise ReviewInputError("finding lifecycle native receipt capacity exceeded")
    receipt_instances = set()
    for receipt in receipts:
        if (
            not isinstance(receipt, dict)
            or set(receipt) != receipt_fields
            or receipt["schema_version"] != "native-finding-receipt-v1"
        ):
            raise ReviewInputError(
                "finding lifecycle native receipt schema is unsupported"
            )
        if receipt["instance"] in receipt_instances:
            raise ReviewInputError(
                "finding lifecycle native receipt identity duplicated"
            )
        receipt_instances.add(receipt["instance"])
        for field in ("repository_id", "pull_request", "producer_id", "review_id"):
            if type(receipt[field]) is not int or receipt[field] <= 0:
                raise ReviewInputError(
                    "finding lifecycle native receipt identity is invalid"
                )
        for field in ("head_sha", "base_sha"):
            if (
                not isinstance(receipt[field], str)
                or re.fullmatch(r"[a-f0-9]{40}", receipt[field]) is None
            ):
                raise ReviewInputError(
                    "finding lifecycle native receipt snapshot is invalid"
                )
        for field in ("instance", "body_sha256"):
            if (
                not isinstance(receipt[field], str)
                or re.fullmatch(r"[a-f0-9]{64}", receipt[field]) is None
            ):
                raise ReviewInputError(
                    "finding lifecycle native receipt digest is invalid"
                )
        if receipt["thread_id"] is not None and (
            not isinstance(receipt["thread_id"], str)
            or not receipt["thread_id"]
            or type(receipt["comment_id"]) is not int
            or receipt["comment_id"] <= 0
        ):
            raise ReviewInputError("finding lifecycle native thread receipt is invalid")
        if receipt["thread_id"] is None:
            if (
                receipt["comment_id"] is not None
                or receipt["native_head_sha"] is not None
            ):
                raise ReviewInputError(
                    "finding lifecycle body receipt cannot claim native inline identity"
                )
        elif (
            not isinstance(receipt["native_head_sha"], str)
            or re.fullmatch(r"[a-f0-9]{40}", receipt["native_head_sha"]) is None
        ):
            raise ReviewInputError("finding lifecycle native head is invalid")
    if type(document["unknown_trust_effect"]) is not bool:
        raise ReviewInputError("finding lifecycle uncertainty is invalid")
    return document


def _require_effect_scope(document, identity, head_sha):
    prefix = "/repos/" + identity.repository + "/"
    for effect in document["effects"]:
        path, request = effect["path"], effect["request"]
        if path == "/graphql":
            variables = request.get("variables")
            if not isinstance(variables, dict):
                raise ReviewInputError("finding lifecycle mutation scope is invalid")
            thread = variables.get("threadId") or (variables.get("input") or {}).get(
                "threadId"
            )
            if not isinstance(thread, str) or not any(
                r["thread_id"] == thread for r in document["native_receipts"]
            ):
                raise ReviewInputError(
                    "finding lifecycle mutation has no selected native binding"
                )
            continue
        if not path.startswith(prefix):
            raise ReviewInputError("finding lifecycle intent repository conflicts")
        pull = re.search(r"/pulls/([0-9]+)/reviews", path)
        if pull is not None and int(pull[1]) != identity.pull_request:
            raise ReviewInputError("finding lifecycle intent pull request conflicts")
        if "check_update_precondition" in effect:
            precondition = effect["check_update_precondition"]
            if (
                precondition["head_sha"] != head_sha
                or path != f"{prefix}check-runs/{precondition['check_run_id']}"
            ):
                raise ReviewInputError(
                    "finding lifecycle check update precondition scope conflicts"
                )
        if "review_update_precondition" in effect:
            precondition = effect["review_update_precondition"]
            if (
                precondition["head_sha"] != head_sha
                or path
                != f"{prefix}pulls/{identity.pull_request}/reviews/{precondition['review_id']}"
            ):
                raise ReviewInputError(
                    "finding lifecycle review update precondition scope conflicts"
                )
        if (
            effect["method"] == "POST"
            and path.endswith("/reviews")
            and request.get("commit_id") != head_sha
        ):
            raise ReviewInputError("finding lifecycle review intent snapshot conflicts")
        if (
            effect["method"] == "POST"
            and path.endswith("/check-runs")
            and request.get("head_sha") != head_sha
        ):
            raise ReviewInputError("finding lifecycle check intent snapshot conflicts")


def restore_finding_lifecycle(
    reviewer, ledger, identity: SessionIdentity, *, loaded=None, _retained=None
):
    """Read only from the independently selected current session root."""
    from review_sensei.finding_lifecycle_store import (
        read_finding_lifecycle,
        select_finding_lifecycle,
    )

    if _retained is not None:
        if loaded is not None:
            raise ReviewInputError(
                "quarantine lifecycle transfer cannot combine sources"
            )
        from .quarantine_lifecycle_transfer import consume

        record, document = consume(_retained, ledger, identity)
        document = _validate(document)
    else:
        if loaded is None:
            selected, stored = select_finding_lifecycle(ledger, identity)
        else:
            selected, stored = loaded, None
        if selected.status not in {"ok", "migrated", "missing"}:
            raise ReviewInputError(
                "finding lifecycle selected session is unavailable: " + selected.status
            )
        record = selected.record
        if record is None or record.finding_lifecycle is None:
            return record, None
        if loaded is not None:
            stored = read_finding_lifecycle(ledger, record, _selected_load=selected)
        from .finding_lifecycle_dictionary import resolve_stored

        stored = resolve_stored(ledger, record, stored)
        document = _validate(_unpack_journal(stored))
    restored = decode_lifecycle_state(document["lifecycle"])
    state = restored.state
    binding = record.finding_lifecycle["manifest"]["binding"]
    if state.head_sha != binding["head_sha"] or state.base_sha != binding["base_sha"]:
        raise ReviewInputError(
            "finding lifecycle inner snapshot conflicts with selected authority"
        )
    _require_effect_scope(document, identity, binding["head_sha"])
    key = (identity.repository_id, identity.pull_request)
    from review_sensei.active_finding_window import ActiveWindowRoundState

    expected_producer = binding["producer"]
    for receipt in document["native_receipts"]:
        if (receipt["repository_id"], receipt["pull_request"]) != key or (
            expected_producer.startswith("github-bot:")
            and str(receipt["producer_id"]) != expected_producer.split(":", 1)[1]
        ):
            raise ReviewInputError(
                "finding lifecycle receipt scope conflicts with selected authority"
            )
    history = record.finding_lifecycle.get(
        "accepted_semantic_history", document.get("accepted_semantic_history")
    )
    if history is not None and history.get("qualification") != "unavailable":
        from .accepted_semantic_history import validate_history

        validate_history(history, identity)
    from .quarantine_namespace import inherited_namespace_is_historical

    if inherited_namespace_is_historical(ledger, record, state, document):
        if (
            key in reviewer._active_window_states
            or key in reviewer._drift_states
            or key in reviewer._unknown_effect_holds
        ):
            raise ReviewInputError(
                "quarantine finding namespace requires an independent current projection"
            )
        # The exact old reference and all effects remain in the immutable
        # predecessor/origin. They are not fresh findings or replay authority.
        return record, None
    if isinstance(state, ActiveWindowRoundState):
        reviewer._active_window_states[key] = state
        reviewer._drift_states.pop(key, None)
        if restored.input_identity is not None:
            reviewer._active_window_inputs[key] = restored.input_identity
    else:
        reviewer._drift_states[key] = state
        reviewer._active_window_states.pop(key, None)
        if restored.input_identity is not None:
            reviewer._drift_inputs[key] = restored.input_identity
        for obligation, concerns in restored.concern_history.items():
            reviewer._drift_concern_history[(*key, obligation)] = concerns
    reviewer._accepted_semantic_histories = getattr(
        reviewer, "_accepted_semantic_histories", {}
    )
    reviewer._accepted_semantic_histories[key] = history
    reviewer._native_finding_receipt_records = getattr(
        reviewer, "_native_finding_receipt_records", {}
    )
    reviewer._native_finding_receipt_records[key] = {
        receipt["instance"]: receipt for receipt in document["native_receipts"]
    }
    if document["unknown_trust_effect"]:
        reviewer._unknown_effect_holds.add(key)
    return record, document


class _JournalTransport:
    """Exact source adapter; callers cannot select its dispatch implementation."""

    def __init__(self, journal, delegate):
        from .command_finalization_plan import register_journal_transport

        self.journal = journal
        self.delegate = delegate
        register_journal_transport(self, journal, delegate)

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def request(self, method, path, **kwargs):
        return self.journal.request(self.delegate, method, path, **kwargs)

    def preflight_native_batch(self, requests, *, native_calls):
        return self.journal.preflight_native_batch(requests, native_calls=native_calls)


class FindingLifecycleJournal:
    """One admitted operation; caches are projections of this selected root."""

    def __init__(
        self,
        reviewer,
        ledger,
        identity,
        *,
        operation_id,
        base_sha,
        head_sha,
        policy_digest,
        configuration_digest,
        document=None,
        app_slug=None,
        record=None,
        review_token=None,
        check_token=None,
        native_budget=None,
    ):
        self._native_budget = native_budget
        self.reviewer = reviewer
        self.ledger = ledger
        self.identity = identity
        self.operation_id = operation_id
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.policy_digest = policy_digest
        self.configuration_digest = configuration_digest
        self.app_slug = app_slug
        self._record = record
        self._initial_record = record
        self._review_token = review_token
        self._check_token = check_token
        self.effects = [] if document is None else list(_validate(document)["effects"])
        self._guarded_publication_pair = (
            None if document is None else document.get("guarded_publication_pair")
        )
        self._issued_review_update_precondition: tuple[object, dict, object] | None = (
            None
        )
        self._batch_body_updates: tuple[tuple[dict[str, Any], dict[str, Any]], ...] = ()
        self._planned = None
        self._input_identity = None
        self._armed = False
        self._replaying = document is not None
        self._effect_cursor = 0
        # Reference-v2 is authoritative after metadata-only close/rollover.
        # Preserve older inner-history envelopes only while no v2 metadata exists;
        # new writes do not duplicate reference history inside finding evidence.
        reference = None if record is None else record.finding_lifecycle
        if reference is not None and "accepted_semantic_history" in reference:
            history = None
        elif document is not None:
            history = document.get("accepted_semantic_history")
        else:
            history = getattr(reviewer, "_accepted_semantic_histories", {}).get(
                (identity.repository_id, identity.pull_request)
            )
        if history is not None:
            from .accepted_semantic_history import validate_history

            validate_history(history, identity)
        self._accepted_semantic_history = (
            None if history is None else json.loads(canonical_bytes(history))
        )

    def _state(self):
        key = (self.identity.repository_id, self.identity.pull_request)
        state = self._planned
        if state is None:
            state = self.reviewer._drift_states.get(
                key
            ) or self.reviewer._active_window_states.get(key)
        if state is None:
            raise ReviewInputError("finding lifecycle has no complete admitted state")
        if state.head_sha != self.head_sha or state.base_sha != self.base_sha:
            raise ReviewInputError(
                "finding lifecycle checkpoint snapshot conflicts with admitted operation"
            )
        identity = self._input_identity
        if identity is None:
            identity = self.reviewer._drift_inputs.get(
                key
            ) or self.reviewer._active_window_inputs.get(key)
        history = {
            oid: concerns
            for (rid, pr, oid), concerns in self.reviewer._drift_concern_history.items()
            if (rid, pr) == key
        }
        return encode_lifecycle_state(
            state, input_identity=identity, concern_history=history
        )

    def _document(self, *, lifecycle=None, effects=None, native_receipts=None):
        return _validate(
            {
                "schema_version": "finding-lifecycle-journal-v3"
                if getattr(self, "_guarded_publication_pair", None) is not None
                else CURRENT_VERSION
                if self._accepted_semantic_history is not None
                else VERSION,
                **(
                    {"accepted_semantic_history": self._accepted_semantic_history}
                    if self._accepted_semantic_history is not None
                    or getattr(self, "_guarded_publication_pair", None) is not None
                    else {}
                ),
                **(
                    {"guarded_publication_pair": self._guarded_publication_pair}
                    if getattr(self, "_guarded_publication_pair", None) is not None
                    else {}
                ),
                "lifecycle": self._state() if lifecycle is None else lifecycle,
                "effects": self.effects if effects is None else effects,
                "native_receipts": list(
                    self.reviewer.native_finding_receipts(
                        repository_id=self.identity.repository_id,
                        pull_request=self.identity.pull_request,
                    )
                )
                if native_receipts is None
                else native_receipts,
                "unknown_trust_effect": (
                    self.identity.repository_id,
                    self.identity.pull_request,
                )
                in self.reviewer._unknown_effect_holds,
            }
        )

    def _pending_wire(self, document):
        from .command_finalization_plan import pack_authorized_pending

        packed = pack_authorized_pending(self, document)
        return _pack_journal(document) if packed is None else packed

    def preflight_native_tail(self, *, calls):
        budget = (
            self._native_budget
            or self.reviewer.finalizer.evidence_budget
            or self.reviewer.finalizer._operation_budget
        )
        if budget is None:
            raise ReviewInputError(
                "finding lifecycle original native budget is unavailable"
            )
        budget.preflight(calls)

    @_sizing_producer
    def preflight_native_batch(self, requests, *, native_calls, _sizing_factories):
        """Price the complete known batch without issuing authority or a write."""
        from .reply_finalization_plan import require_reply_cleanup_batch

        require_reply_cleanup_batch(self, requests)
        import copy

        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

        if type(native_calls) is not int or native_calls < 0:
            raise ReviewInputError("finding lifecycle batch native tail is invalid")
        if not isinstance(requests, tuple) or len(requests) > MAX_EFFECTS:
            raise ReviewInputError("finding lifecycle batch request capacity exceeded")
        new = []
        known = {e["request_sha256"] for e in self.effects}
        for method, path, raw in requests:
            if not (
                method == "PATCH"
                and re.fullmatch(
                    re.escape(f"/repos/{self.identity.repository}/pulls/comments/")
                    + r"[1-9][0-9]*",
                    path,
                )
                and isinstance(raw, dict)
                and set(raw) == {"body"}
                and isinstance(raw["body"], str)
                or method == "POST"
                and path == "/graphql"
                and self._is_effect(method, path, raw)
                and "resolveReviewThread" in str(raw.get("query", ""))
            ):
                raise ReviewInputError(
                    "finding lifecycle batch request scope is unsupported"
                )
            request = _request_identity(raw)
            key = _digest({"method": method, "path": path, "request": request})
            legacy = _digest({"method": method, "path": path, "request": raw})
            if key in known or legacy in known:
                for retained in self.effects:
                    if (
                        retained["request_sha256"] in {key, legacy}
                        and retained["path"] == "/graphql"
                        and retained["status"] == "confirmed"
                        and not _confirmed_thread_response_matches(retained)
                    ):
                        raise ReviewInputError(
                            "finding lifecycle retained mutation receipt conflicts"
                        )
                continue
            known.update((key, legacy))
            new.append(
                {
                    "method": method,
                    "path": path,
                    "request": request,
                    "request_sha256": key,
                    "status": "pending",
                    "response": None,
                }
            )
        if len(self.effects) + len(new) > MAX_EFFECTS:
            raise ReviewInputError(
                "finding lifecycle known batch exceeds effect capacity before dispatch"
            )
        if new and any(e["status"] == "pending" for e in self.effects):
            raise ReviewInputError(
                "finding lifecycle prior native effect remains uncertain"
            )
        if len(new) <= 1:
            return
        ordered = None
        if len(new) == len(requests) and all(e["path"] == "/graphql" for e in new):
            from .ordered_native_batch import issue_ordered_batch

            ordered = issue_ordered_batch(self, requests)
        shadow = copy.copy(self)
        shadow.effects = copy.deepcopy(self.effects)
        receipts = self.reviewer.native_finding_receipts(
            repository_id=self.identity.repository_id,
            pull_request=self.identity.pull_request,
        )
        updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for effect in new:
            if effect["method"] == "PATCH":
                cid = int(effect["path"].rsplit("/", 1)[1])
                matches = [r for r in receipts if r["comment_id"] == cid]
                if len(matches) != 1:
                    raise ReviewInputError(
                        "finding lifecycle batch comment has no selected native binding"
                    )
                old = matches[0]
                updates.append(
                    (
                        old,
                        {
                            **old,
                            "body_sha256": effect["request"]["body"]["$body_sha256"],
                        },
                    )
                )
        shadow._batch_body_updates = tuple(updates)
        documents: list[dict[str, Any]] = []
        stop_documents: list[dict[str, Any]] = []
        confirmed_instances: set[str] = set()
        prefix_states = list(getattr(shadow, "_finalizer_capacity_states", ()))
        lifecycle_key = (self.identity.repository_id, self.identity.pull_request)
        current_state = (
            shadow._planned
            or self.reviewer._drift_states.get(lifecycle_key)
            or self.reviewer._active_window_states.get(lifecycle_key)
        )
        projection_sources = tuple(
            state for state in (current_state, *prefix_states) if state is not None
        )
        for effect in new:
            if ordered is not None:
                from .finding_lifecycle_consumers import project_confirmed_slot_release

                target = _thread_effect_target(effect)
                if target is None:
                    raise ReviewInputError("ordered native batch target is invalid")
                confirmed_instances.update(
                    receipt["instance"]
                    for receipt in receipts
                    if receipt["thread_id"] == target[1]
                )
                prefix_states.extend(
                    project_confirmed_slot_release(
                        state,
                        key=lifecycle_key,
                        receipts={receipt["instance"]: receipt for receipt in receipts},
                        instances=confirmed_instances,
                        head_sha=self.head_sha,
                    )
                    for state in projection_sources
                )
                shadow._finalizer_capacity_states = tuple(prefix_states)
            shadow.effects.append(effect)
            _require_effect_scope(shadow._document(), self.identity, self.head_sha)
            pending, future, confirmed = shadow._preflight_effect(capacity_only=True)
            pending = self._pending_wire(_unpack_journal(pending))
            documents.append(pending)
            stop_documents.extend((pending, future))
            # Only a private sizing shadow advances; real pending intents and
            # their immutable uncertainty are never modified or dispatched.
            shadow.effects = confirmed
        # Successful dispatch confirms in memory. The next pending checkpoint
        # carries that confirmation; only the final receipt adds another root.
        # A failure ends at any prefix. The last retained checkpoint is the
        # conservative largest pending/confirmed/finalization envelope across
        # every stop point, not optimistically the longest successful prefix.
        documents.append(max(stop_documents, key=lambda doc: len(canonical_bytes(doc))))
        exact_proof = None
        if ordered is not None:
            original_bytes = canonical_bytes(self._record.to_dict())
            original_effects = canonical_bytes(self.effects)
            pending_bytes = tuple(canonical_bytes(doc) for doc in documents[:-1])
            exact_proof = _sizing_factories[0](
                self.ledger,
                self._record,
                tuple(documents),
                frozenset(range(len(documents) - 1)),
                lambda: (
                    self._record is ordered.record
                    and canonical_bytes(self._record.to_dict()) == original_bytes
                    and canonical_bytes(self.effects) == original_effects
                    and not ordered._bound
                    and not ordered._closed
                ),
            )
        from review_sensei.bounded_evidence import EvidenceReadBudget
        from review_sensei.session import LocalSessionLedger

        native_budget = (
            self._native_budget
            or self.reviewer.finalizer.evidence_budget
            or self.reviewer.finalizer._operation_budget
        )
        ledger_budget = getattr(self.ledger, "evidence_budget", None)
        separate_local_domains = (
            type(self.ledger) is LocalSessionLedger
            and type(ledger_budget) is EvidenceReadBudget
            and type(native_budget) is EvidenceReadBudget
            and ledger_budget is not native_budget
        )
        preflight_finding_lifecycle(
            self.ledger,
            self._record,
            document=documents[0],
            future_documents=tuple(documents[1:]),
            operation_id=self.operation_id,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy_digest=self.policy_digest,
            configuration_digest=self.configuration_digest,
            item_count=len(shadow.effects) + 1,
            reserved_tail_calls=6 if separate_local_domains else native_calls + 6,
            exact_documents=exact_proof,
        )
        self.preflight_native_tail(calls=native_calls)
        if ordered is not None:
            self._ordered_native_batch = ordered
            self._ordered_pending_documents = pending_bytes
            return ordered.approve()

    @_sizing_producer
    def preflight_reply_finalization(self, plan, *, http, budget, _sizing_factories):
        """Price a source-issued reply tail before report or authority publication.

        Shadow states are capacity data only. A newly allocated assessment review
        ID is unknown, so subsequent checkpoints retain generic encoding bounds.
        """
        import copy

        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

        from .api_run_requests import ApiRunSessionLedger
        from .reply_finalization_plan import ReplyFinalizationPlan
        from .review_run import ActionRunLedger

        if type(plan) is not ReplyFinalizationPlan:
            raise ReviewInputError("reply finalization capacity plan is unissued")
        ReplyFinalizationPlan.require(plan, self, http=http, budget=budget)
        payload = json.loads(plan.payload)
        reads = payload.get("execution_read_tail")
        capabilities = payload.get("execution_capability_tail")
        cleanup_calls = payload.get("cleanup_native_calls")
        report_calls = payload.get("report_native_calls")
        if (
            payload.get("read_contract")
            != "closed-inline-reply-single-complete-page-v1"
            or not isinstance(reads, dict)
            or not isinstance(capabilities, dict)
            or any(
                type(value) is not int or value < 0
                for value in (*reads.values(), *capabilities.values())
            )
            or type(cleanup_calls) is not int
            or cleanup_calls < 0
            or type(report_calls) is not int
            or report_calls < 0
            or type(self.ledger) not in {ApiRunSessionLedger, ActionRunLedger}
            or getattr(self.ledger.load, "__func__", None)
            is not ApiRunSessionLedger.load
        ):
            raise ReviewInputError("reply_finalization_capacity_read_tail_unqualified")
        requests = payload.get("ordered_requests")
        cleanup = payload.get("cleanup_requests")
        if (
            not isinstance(requests, list)
            or not requests
            or len(requests) > MAX_EFFECTS
            or not isinstance(cleanup, list)
            or not isinstance(plan.states, tuple)
            or len(plan.states) < 2
        ):
            raise ReviewInputError("reply finalization capacity plan is incomplete")
        review_path = f"/repos/{self.identity.repository}/pulls/{self.identity.pull_request}/reviews"
        check_prefix = f"/repos/{self.identity.repository}/check-runs"
        phase = "authority"
        observed_cleanup = []
        for value in requests:
            if not isinstance(value, list) or len(value) != 3:
                raise ReviewInputError("reply finalization request is invalid")
            method, path, raw = value
            if not isinstance(raw, dict) or not isinstance(path, str):
                raise ReviewInputError("reply finalization request is invalid")
            if phase == "authority":
                if not (
                    method == "POST"
                    and path == review_path
                    and set(raw) == {"commit_id", "event", "body"}
                    and raw["commit_id"] == self.head_sha
                    and raw["event"] == "COMMENT"
                    and isinstance(raw["body"], str)
                ):
                    raise ReviewInputError("reply finalization authority scope changed")
                phase = "cleanup"
            elif path == "/graphql" and phase == "cleanup":
                if not (
                    method == "POST"
                    and self._is_effect(method, path, raw)
                    and "resolveReviewThread" in str(raw.get("query", ""))
                ):
                    raise ReviewInputError("reply finalization cleanup scope changed")
                observed_cleanup.append(value)
            elif path == review_path and phase == "cleanup":
                if not (
                    method == "POST"
                    and set(raw) == {"commit_id", "event", "body"}
                    and raw["commit_id"] == self.head_sha
                    and raw["event"] == "APPROVE"
                    and isinstance(raw["body"], str)
                ):
                    raise ReviewInputError("reply finalization approval scope changed")
                phase = "check"
            elif phase in {"cleanup", "check"} and (
                method == "POST"
                and path == check_prefix
                or method == "PATCH"
                and re.fullmatch(re.escape(check_prefix) + r"/[1-9][0-9]*", path)
            ):
                if not self._is_effect(method, path, raw):
                    raise ReviewInputError("reply finalization check scope changed")
                phase = "done"
            else:
                raise ReviewInputError("reply finalization request order changed")
        if observed_cleanup != cleanup or phase != "done":
            raise ReviewInputError("reply finalization complete check tail is missing")
        if any(effect["status"] == "pending" for effect in self.effects):
            raise ReviewInputError("reply finalization prior outcome remains uncertain")

        shadow = copy.copy(self)
        shadow.reviewer = copy.copy(self.reviewer)
        shadow.effects = copy.deepcopy(self.effects)
        shadow._planned = plan.states[0]
        shadow._finalizer_capacity_states = tuple(plan.states)
        documents: list[dict[str, Any]] = []
        branches: list[tuple[tuple[dict[str, Any], ...], frozenset[int]]] = []
        pending_commitments: dict[str, bytes] = {}
        seen = {effect["request_sha256"] for effect in self.effects}
        for position, (method, path, raw) in enumerate(requests):
            request = _request_identity(raw)
            digest = _digest({"method": method, "path": path, "request": request})
            legacy = _digest({"method": method, "path": path, "request": raw})
            if digest in seen or legacy in seen:
                raise ReviewInputError(
                    "reply finalization retained replay is unqualified"
                )
            seen.update((digest, legacy))
            if len(shadow.effects) >= MAX_EFFECTS:
                raise ReviewInputError("reply finalization effect capacity exceeded")
            effect = dict(
                method=method,
                path=path,
                request=request,
                request_sha256=digest,
                status="pending",
                response=None,
            )
            if method == "PATCH" and path.startswith(check_prefix + "/"):
                precondition = self._check_precondition(path)
                if precondition is not None:
                    effect["check_update_precondition"] = precondition
            shadow.effects.append(effect)
            _require_effect_scope(shadow._document(), self.identity, self.head_sha)
            pending, future, confirmed = shadow._preflight_effect(capacity_only=True)
            if position == 0:
                # Only this pending state precedes the unknown new native ID.
                pending_commitments[digest] = canonical_bytes(pending)
            # After authority publication, both native IDs and the released
            # prefix can differ from this shadow. The maximum candidate covers
            # every supplied state and pending/rejected/confirmed outcome; use
            # its generic bound for EACH reached checkpoint, not only the last.
            documents.append(pending if position == 0 else future)
            branches.append((tuple((*documents, future)), frozenset()))
            shadow.effects = confirmed
            # Later real finalization authenticates the authority POST first.
            # This detached projection never installs that positive proof live.
            if position == 0:
                shadow._planned = plan.states[1]

        alternatives = tuple(reversed(branches))
        documents = list(alternatives[0][0])

        def valid():
            ReplyFinalizationPlan.require(plan, self, http=http, budget=budget)
            return True

        proof = _sizing_factories[2](self.ledger, self._record, alternatives, valid)
        restore_calls = plan.price_restore_calls()
        native_calls = (
            sum(reads.values())
            + sum(capabilities.values())
            + cleanup_calls
            + report_calls
            + restore_calls
            + 2 * sum(path != "/graphql" for _, path, _ in requests)
        )
        result = preflight_finding_lifecycle(
            self.ledger,
            self._record,
            document=documents[0],
            future_documents=tuple(documents[1:]),
            operation_id=self.operation_id,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy_digest=self.policy_digest,
            configuration_digest=self.configuration_digest,
            item_count=len(shadow.effects) + 1,
            reserved_tail_calls=native_calls + 6,
            branch_documents=proof,
        )
        self.preflight_native_tail(calls=native_calls)
        ReplyFinalizationPlan.bind_capacity(plan, pending_documents=pending_commitments)
        return result

    def _command_branch_documents(self, plan, *, http, budget):
        """Derive exact success/stop checkpoint sizes without admitting an action.

        Requires the actual source-observed native plan, but emits only detached
        documents. It neither binds a hypothetical intent nor grants its execution.
        Original runtime preflight still binds the admitted intent and source root.
        """
        import copy

        from .command_finalization_plan import CommandFinalizationPlan

        if type(plan) is not CommandFinalizationPlan:
            raise ReviewInputError("command finalization capacity plan is unissued")
        from review_sensei.source_registry import require_instance_source

        require_instance_source(
            plan, methods=("require", "can_reuse_retained_lifecycle")
        )
        plan.require(self, http=http, budget=budget)
        payload = json.loads(plan.payload)
        requests = tuple(
            request
            for request in (
                payload.get("review_update"),
                *payload.get("cleanup_requests", ()),
                payload.get("check_request"),
                payload.get("conditional_approval"),
            )
            if request is not None
        )
        if any(effect["status"] == "pending" for effect in self.effects):
            raise ReviewInputError(
                "command finalization prior outcome remains uncertain"
            )
        dictionary_base = None
        dictionary_manifest = self._record.finding_lifecycle["manifest"]
        if dictionary_manifest["encoding"] == "partitioned-json-v1":
            dictionary_base = {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": _pack_journal(self._document()),
            }
        shadow = copy.copy(self)
        shadow.reviewer = copy.copy(self.reviewer)
        shadow.effects = copy.deepcopy(self.effects)
        from .command_finalization_plan import (
            cleanup_capacity_states,
            command_preconfirmed_instances,
        )

        shadow._finalizer_capacity_states = cleanup_capacity_states(plan)
        receipts = list(
            self.reviewer.native_finding_receipts(
                repository_id=self.identity.repository_id,
                pull_request=self.identity.pull_request,
            )
        )
        shadow.reviewer.native_finding_receipts = lambda **kwargs: tuple(receipts)
        documents: list[dict[str, Any]] = []
        stops: list[dict[str, Any]] = []
        branches: list[tuple[tuple[dict[str, Any], ...], frozenset[int]]] = []
        body_updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        exact_indices: set[int] = set()
        pending_commitments: dict[str, bytes] = {}
        predictable = True
        seen = {effect["request_sha256"] for effect in self.effects}
        confirmed_instances: set[str] = set()
        preconfirmed_instances = command_preconfirmed_instances(plan)
        key = (self.identity.repository_id, self.identity.pull_request)
        initial_state = (
            shadow._planned
            or self.reviewer._drift_states.get(key)
            or self.reviewer._active_window_states.get(key)
        )
        states = tuple(
            state
            for state in (initial_state, *cleanup_capacity_states(plan))
            if state is not None
        )
        cleanup_complete = False
        for method, path, raw in requests:
            if [method, path, raw] == payload.get("conditional_approval"):
                # The real finalizer installs the successful disposition outcome
                # after cleanup/check and before approval. This detached copy is
                # sizing only; the source plan supplies both exact outcomes.
                matching = [
                    state for state in plan.states if type(state) is type(initial_state)
                ]
                if len(matching) != 2:
                    raise ReviewInputError(
                        "command finalization disposition outcomes are unqualified"
                    )
                projected_success = matching[1]
                for old, updated in body_updates:
                    projected_success = shadow._updated_binding_proof_state(
                        projected_success, old, updated
                    )
                if confirmed_instances or preconfirmed_instances:
                    from .finding_lifecycle_consumers import (
                        project_confirmed_slot_release,
                    )

                    projected_success = project_confirmed_slot_release(
                        projected_success,
                        key=key,
                        receipts={r["instance"]: r for r in receipts},
                        instances=confirmed_instances | preconfirmed_instances,
                        head_sha=self.head_sha,
                    )
                shadow._planned = projected_success
            if (
                path != "/graphql"
                and (confirmed_instances or preconfirmed_instances)
                and not cleanup_complete
                and [method, path, raw] != payload.get("review_update")
            ):
                from .finding_lifecycle_consumers import project_confirmed_slot_release

                shadow._planned = project_confirmed_slot_release(
                    shadow._planned or initial_state,
                    key=key,
                    receipts={r["instance"]: r for r in receipts},
                    instances=confirmed_instances | preconfirmed_instances,
                    head_sha=self.head_sha,
                )
                cleanup_complete = True
            request = _request_identity(raw)
            digest = _digest({"method": method, "path": path, "request": request})
            legacy = _digest({"method": method, "path": path, "request": raw})
            if digest in seen or legacy in seen:
                if path == "/graphql":
                    raise ReviewInputError(
                        "command finalization retained cleanup replay is unqualified"
                    )
                continue
            seen.update((digest, legacy))
            if len(shadow.effects) >= MAX_EFFECTS:
                raise ReviewInputError("command finalization effect capacity exceeded")
            effect = dict(
                method=method,
                path=path,
                request=request,
                request_sha256=digest,
                status="pending",
                response=None,
            )
            if method == "PUT":
                before = payload.get("review_before")
                commitment = self._native_review_commitment(before)
                if commitment is None or commitment["review_id"] != int(
                    path.rsplit("/", 1)[-1]
                ):
                    raise ReviewInputError("command finalization review source changed")
                body_receipts = self._body_receipts(commitment["review_id"])
                if body_receipts:
                    if not self._receipts_match_update_precondition(
                        body_receipts, commitment
                    ):
                        raise ReviewInputError(
                            "command finalization body receipt precondition changed"
                        )
                    effect["review_update_precondition"] = commitment
            if method == "PATCH" and "/check-runs/" in path:
                check_precondition = self._check_precondition(path)
                if check_precondition is not None:
                    effect["check_update_precondition"] = check_precondition
            shadow.effects.append(effect)
            _require_effect_scope(shadow._document(), self.identity, self.head_sha)
            pending, future, confirmed = shadow._preflight_effect(capacity_only=True)
            if predictable:
                exact_indices.add(len(documents))
                pending_commitments[digest] = canonical_bytes(pending)
            if dictionary_base is not None and predictable:
                from .finding_lifecycle_dictionary import pack as pack_dictionary

                pending = pack_dictionary(
                    _unpack_journal(pending),
                    base=dictionary_base,
                    manifest=dictionary_manifest,
                    current_binding=dictionary_manifest["binding"],
                )
            documents.append(pending)
            stops.extend((pending, future))
            branches.append((tuple((*documents, future)), frozenset(exact_indices)))
            shadow.effects = confirmed
            if method == "PUT":
                new_receipts = []
                for old in receipts:
                    updated = old
                    if (
                        old["review_id"] == int(path.rsplit("/", 1)[-1])
                        and old["thread_id"] is None
                    ):
                        updated = {
                            **old,
                            "body_sha256": request["body"]["$body_sha256"],
                        }
                        body_updates.append((old, updated))
                        shadow._planned = shadow._updated_binding_proof_state(
                            shadow._planned or initial_state, old, updated
                        )
                    new_receipts.append(updated)
                receipts[:] = new_receipts
            elif path == "/graphql":
                from .finding_lifecycle_consumers import project_confirmed_slot_release

                target = _thread_effect_target(effect)
                if target is None:
                    raise ReviewInputError(
                        "command finalization cleanup target changed"
                    )
                confirmed_instances.update(
                    r["instance"] for r in receipts if r["thread_id"] == target[1]
                )
                shadow._finalizer_capacity_states = tuple(
                    project_confirmed_slot_release(
                        state,
                        key=key,
                        receipts={r["instance"]: r for r in receipts},
                        instances=confirmed_instances,
                        head_sha=self.head_sha,
                    )
                    for state in states
                )
            elif "check_update_precondition" not in effect:
                # New check/approval identities are not predictable source IDs.
                predictable = False
        if not documents:
            # Still price the final retained checkpoint and source restore.
            documents = [_pack_journal(shadow._document())]
            branches = [(tuple(documents), frozenset())]
        # Each stop branch owns only its reached pending prefix and its own
        # conservative final checkpoint. Later native effects are prohibited
        # after a source-planned cleanup stop, not silently added to that branch.
        alternatives = tuple(reversed(branches))

        return (
            alternatives,
            pending_commitments,
            len(shadow.effects) + 1,
            requests,
            dictionary_base,
        )

    @_sizing_producer
    def _preflight_preentry_command_branches(self, plan, *, _sizing_factories):
        """Price source-observed finalization branches, not a whole action route.

        Intent/entry, receipt publication, final action settlement and own-CAS
        transfer remain separate obligations. This method never binds _PREVIEW.
        """
        import sys

        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle
        from review_sensei.source_registry import (
            require_instance_source,
            require_module_seal,
        )

        from . import trust_disposition
        from .trust_effects import verification_calls

        require_instance_source(
            plan, methods=("require", "can_reuse_retained_lifecycle")
        )
        require_module_seal(sys.modules[trust_disposition.__name__])
        for instance in (
            self,
            self.reviewer,
            self.ledger,
            self.ledger.caller,
            plan.budget,
        ):
            require_instance_source(instance, methods=())
            methods = tuple(
                name
                for name in dir(type(instance))
                if not name.startswith("__") and callable(getattr(type(instance), name))
            )
            require_instance_source(instance, methods=methods)
        if getattr(self.ledger, "business_guard", None) is not None:
            raise ReviewInputError(
                "command pre-entry sizing cannot replace an existing guard"
            )
        preview, details, intent, valid, guard = (
            trust_disposition.consume_preentry_sizing_context(plan, self)
        )
        alternatives, _, item_count, requests, _ = self._command_branch_documents(
            plan, http=plan.http, budget=plan.budget
        )
        payload = json.loads(plan.payload)
        reads, capabilities = (
            payload.get("execution_read_tail"),
            payload.get("execution_capability_tail"),
        )
        if (
            payload.get("read_contract") != "closed-plain-single-complete-page-v1"
            or not isinstance(reads, dict)
            or not isinstance(capabilities, dict)
            or any(
                type(n) is not int or n < 0
                for n in (*reads.values(), *capabilities.values())
            )
        ):
            raise ReviewInputError("command pre-entry read tail is unqualified")
        # No own-CAS transfer has happened, so retain the complete restore price.
        reference = self._record.finding_lifecycle["manifest"]
        restore = (
            10
            if reference["encoding"] == "inline-json-v1"
            else 20 + len(reference["parts"])
        )
        snapshot = preview.disposition_snapshot
        if (
            snapshot is not None
            and snapshot["state_manifest"]["encoding"] != "inline-json-v1"
        ):
            raise ReviewInputError(
                "command pre-entry disposition partition pricing is unsupported"
            )
        native_calls = sum(reads.values()) + sum(capabilities.values())
        native_calls += payload.get("cleanup_native_calls", 0)
        native_calls += 2 * sum(path != "/graphql" for _, path, _ in requests)
        native_calls += int(
            bool(
                payload.get("review_update")
                and self._body_receipts(
                    int(payload["review_update"][1].rsplit("/", 1)[-1])
                )
            )
        )
        native_calls += restore + verification_calls(self.ledger.caller) * len(requests)
        branch_proof = _sizing_factories[2](
            self.ledger, self._record, alternatives, valid
        )
        projected = _sizing_factories[1](
            self.ledger,
            self._record,
            preview,
            details["staged_parts"],
            intent,
            plan,
            valid,
        )
        documents = alternatives[0][0]
        previous_guard = getattr(self.ledger, "business_guard", None)
        self.ledger.business_guard = guard
        try:
            result = preflight_finding_lifecycle(
                self.ledger,
                self._record,
                document=documents[0],
                future_documents=documents[1:],
                operation_id=self.operation_id,
                base_sha=self.base_sha,
                head_sha=self.head_sha,
                policy_digest=self.policy_digest,
                configuration_digest=self.configuration_digest,
                item_count=item_count,
                reserved_tail_calls=native_calls + 6,
                projected_root=projected,
                branch_documents=branch_proof,
            )
        finally:
            self.ledger.business_guard = previous_guard
        valid()
        return {
            **result,
            "scope": "finalization-checkpoint-branches-only",
            "native_requests": len(requests),
            "restore_calls": restore,
            "whole_route_admitted": False,
        }

    @_sizing_producer
    def preflight_command_finalization(
        self, plan, *, preview_record, capacity_details, http, budget, _sizing_factories
    ):
        """Whole-command admission; detached previews never become authority."""
        from .command_finalization_plan import CommandFinalizationPlan

        if type(plan) is not CommandFinalizationPlan:
            raise ReviewInputError("command finalization capacity plan is unissued")
        from review_sensei.source_registry import require_instance_source

        require_instance_source(
            plan, methods=("require", "can_reuse_retained_lifecycle")
        )
        plan.require(self, http=http, budget=budget)
        if (
            not isinstance(capacity_details, dict)
            or preview_record is None
            or preview_record.identity() != self._record.identity()
            or capacity_details.get("original_root") != self._record.record_sha256
            or capacity_details.get("preview_root") != preview_record.record_sha256
            or not isinstance(capacity_details.get("intent_sha256"), str)
            or not isinstance(capacity_details.get("staged_parts"), tuple)
        ):
            raise ReviewInputError("command finalization disposition preview changed")
        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

        from .api_run_requests import ApiRunSessionLedger
        from .review_run import ActionRunLedger

        payload = json.loads(plan.payload)
        reads = payload.get("execution_read_tail")
        capabilities = payload.get("execution_capability_tail")
        if (
            payload.get("read_contract") != "closed-plain-single-complete-page-v1"
            or not isinstance(reads, dict)
            or not isinstance(capabilities, dict)
            or any(
                type(value) is not int or value < 0
                for value in (*reads.values(), *capabilities.values())
            )
            or type(self.ledger) not in {ApiRunSessionLedger, ActionRunLedger}
            or getattr(self.ledger.load, "__func__", None)
            is not ApiRunSessionLedger.load
        ):
            raise ReviewInputError(
                "command_finalization_capacity_read_tail_unqualified"
            )
        caller = self.ledger.caller
        action = caller.accounting.action
        if (
            action is None
            or _digest(action["intent"]) != capacity_details["intent_sha256"]
        ):
            raise ReviewInputError("command finalization admitted intent changed")
        require_instance_source(self, methods=("_command_branch_documents",))
        alternatives, pending_commitments, item_count, requests, dictionary_base = (
            self._command_branch_documents(plan, http=http, budget=budget)
        )
        if dictionary_base is not None:
            plan.bind_dictionary_base(dictionary_base)
        documents = list(alternatives[0][0])

        def valid():
            plan.require(self, http=http, budget=budget)
            return (
                capacity_details.get("original_root") == self._record.record_sha256
                and capacity_details.get("preview_root") == preview_record.record_sha256
                and _digest(caller.accounting.action["intent"])
                == capacity_details["intent_sha256"]
            )

        branch_proof = _sizing_factories[2](
            self.ledger, self._record, alternatives, valid
        )
        projected = _sizing_factories[1](
            self.ledger,
            self._record,
            preview_record,
            capacity_details["staged_parts"],
            capacity_details["intent_sha256"],
            plan,
            valid,
        )
        reference = self._record.finding_lifecycle["manifest"]
        partitioned = reference["encoding"] != "inline-json-v1"
        restore_calls = (
            0
            if plan.can_reuse_retained_lifecycle(preview_record)
            else (20 + len(reference["parts"]))
            if partitioned
            else 10
        )
        snapshot = preview_record.disposition_snapshot
        snapshot_reads = 0
        if isinstance(snapshot, dict):
            from review_sensei.disposition_snapshot import normalize_snapshot

            snapshot = normalize_snapshot(snapshot)
            if snapshot is None or snapshot["schema_version"] != "1.0":
                raise ReviewInputError(
                    "command finalization snapshot reads are unqualified"
                )
            manifest = snapshot["state_manifest"]
            if manifest["encoding"] == "partitioned-json-v1":
                snapshot_reads = len(manifest["parts"])
            elif manifest["encoding"] != "inline-json-v1":
                raise ReviewInputError(
                    "command finalization snapshot reads are unqualified"
                )
        native_calls = sum(reads.values()) + sum(capabilities.values())
        native_calls += payload.get("cleanup_native_calls", 0)
        # Exact native write and one possible readback per non-cleanup effect.
        # Cleanup already carries its own complete mutation/readback tail.
        native_calls += 2 * sum(path != "/graphql" for _, path, _ in requests)
        native_calls += int(
            bool(
                payload.get("review_update")
                and self._body_receipts(
                    int(payload["review_update"][1].rsplit("/", 1)[-1])
                )
            )
        )
        native_calls += restore_calls + snapshot_reads
        result = preflight_finding_lifecycle(
            self.ledger,
            self._record,
            document=documents[0],
            future_documents=tuple(documents[1:]),
            operation_id=self.operation_id,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy_digest=self.policy_digest,
            configuration_digest=self.configuration_digest,
            item_count=item_count,
            reserved_tail_calls=native_calls + 6,
            projected_root=projected,
            branch_documents=branch_proof,
        )
        self.preflight_native_tail(calls=native_calls)
        plan.bind_preview_capacity(
            preview_record, capacity_details, pending_documents=pending_commitments
        )
        return result

    @_sizing_producer
    def _exact_pending_proof(self, documents, *, _sizing_factories):
        """Requalify exact bytes at each live prefix, never an unknown future."""
        from .command_finalization_plan import pack_authorized_pending

        logical = self._document()
        command_pending = pack_authorized_pending(self, logical)
        if command_pending is not None:
            expected_command = canonical_bytes(command_pending)
            if canonical_bytes(documents[0]) != expected_command:
                raise ReviewInputError("command dictionary pending content changed")
            return _sizing_factories[0](
                self.ledger,
                self._record,
                documents,
                frozenset({0}),
                lambda: (
                    canonical_bytes(pack_authorized_pending(self, self._document()))
                    == expected_command
                ),
            )
        ordered = getattr(self, "_ordered_native_batch", None)
        if ordered is None or not ordered._inflight or ordered._closed:
            return None
        expected = self._ordered_pending_documents[ordered.confirmed_prefix]
        if canonical_bytes(documents[0]) != expected:
            ordered.stop()
            raise ReviewInputError("ordered native batch pending content changed")
        return _sizing_factories[0](
            self.ledger,
            self._record,
            documents,
            frozenset({0}),
            lambda: (
                ordered is not None
                and ordered._inflight
                and not ordered._closed
                and ordered is getattr(self, "_ordered_native_batch", None)
                and canonical_bytes(documents[0]) == expected
            ),
        )

    def prepare_finalizer_capacity_states(
        self, states, *, cleanup_instances=frozenset()
    ):
        """Retain source-projected outcomes for sizing, never as live authority.

        A smaller logical post-disposition state can compress worse than the
        current state. Keep both exact projections through the final checkpoint;
        the ordinary per-effect/batch planner still prices every carrier write.
        """
        from review_sensei.active_finding_window import ActiveWindowRoundState
        from review_sensei.finding_drift import DriftEvaluationResult

        if not isinstance(states, tuple) or len(states) > 4:
            raise ReviewInputError("finding lifecycle finalizer sizing is invalid")
        key = (self.identity.repository_id, self.identity.pull_request)
        current = self._planned
        if current is None:
            current = self.reviewer._drift_states.get(
                key
            ) or self.reviewer._active_window_states.get(key)
        for state in states:
            if (
                not isinstance(state, (ActiveWindowRoundState, DriftEvaluationResult))
                or type(state) is not type(current)
                or (state.head_sha, state.base_sha) != (self.head_sha, self.base_sha)
            ):
                raise ReviewInputError(
                    "finding lifecycle finalizer sizing scope changed"
                )
        if not isinstance(cleanup_instances, frozenset):
            raise ReviewInputError("finding lifecycle cleanup sizing scope is invalid")
        if current is not None:
            receipts = self.reviewer.native_finding_receipts(
                repository_id=self.identity.repository_id,
                pull_request=self.identity.pull_request,
            )
            targets = {
                r["thread_id"]
                for r in receipts
                if r["instance"] in cleanup_instances
                and r["head_sha"] == self.head_sha
                and r["repository_id"] == self.identity.repository_id
                and r["pull_request"] == self.identity.pull_request
                and r["thread_id"] is not None
            }
            if isinstance(current, DriftEvaluationResult):
                candidates = tuple(
                    (
                        item.lifecycle_state,
                        item.resolution_proof_sha256,
                        item.thread_binding.slot_state if item.thread_binding else None,
                        item.thread_binding.thread_id if item.thread_binding else None,
                    )
                    for item in current.inventory.obligations
                )
            else:
                candidates = tuple(
                    (
                        item.lifecycle_state,
                        item.resolution_proof_sha256,
                        item.target_state,
                        item.native_target,
                    )
                    for item in current.authoritative_decision_inventory.obligations
                )
            release_targets = {
                target
                for lifecycle, proof, status, target in candidates
                if lifecycle == "verified-fixed"
                and proof is not None
                and status == "contingent"
                and target in targets
            }
            if len(release_targets) > 1:
                from .command_finalization_plan import command_cleanup_capacity_states

                qualified = command_cleanup_capacity_states(
                    self, states, cleanup_instances
                )
                if qualified is not None:
                    self._finalizer_capacity_states = qualified
                    return
                # Partial native success can release an arbitrary subset. The
                # all-or-none override endpoints do not bound that compression.
                raise ReviewInputError(
                    "finding_lifecycle_verified_fixed_subset_capacity_unqualified"
                )
        self._finalizer_capacity_states = states

    def _preflight_effect(self, *, capacity_only=False):
        """Qualify both intent and every bounded receipt/state outcome first."""
        import copy

        from review_sensei.active_finding_window import (
            ActiveWindowRoundState,
            commit_window_publication,
        )
        from review_sensei.context import finding_lifecycle_for_comment
        from review_sensei.finding_drift import (
            commit_drift_publication,
            project_drift_review_result,
        )
        from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle
        from review_sensei.human_assessment import finding_instance_fingerprint
        from review_sensei.models import ReviewResult

        state = self._planned
        if state is None:
            key = (self.identity.repository_id, self.identity.pull_request)
            state = self.reviewer._drift_states.get(
                key
            ) or self.reviewer._active_window_states.get(key)
        if state is None:
            raise ReviewInputError("finding lifecycle preflight has no admitted state")
        entries: Any
        targets: Any
        variants: list[Any]
        if isinstance(state, ActiveWindowRoundState):
            entries = state.display_active_findings.entries
            targets = {
                e.obligation_id: "capacity_" + e.obligation_id
                for e in entries
                if e.target_action in {"create", "reopen"}
            }
            variants = [
                commit_window_publication(state, created_targets=targets),
                commit_window_publication(state, uncertain_targets=targets),
            ]
            comments = {
                o.obligation_id: o.comment
                for o in state.authoritative_decision_inventory.obligations
            }
        else:
            entries = state.active_entries
            targets = {
                e.obligation_id: (
                    "capacity_" + e.obligation_id,
                    9_223_372_036_854_775_807 - i,
                )
                for i, e in enumerate(entries)
                if e.thread_action in {"create", "reopen"}
            }
            variants = [
                commit_drift_publication(state, created_threads=targets),
                commit_drift_publication(
                    state,
                    uncertain_threads={
                        k: (
                            v[0],
                            v[1],
                            hashlib.sha256(
                                ("capacity-uncertain:" + k).encode()
                            ).hexdigest(),
                        )
                        for k, v in targets.items()
                    },
                ),
            ]
            projected = project_drift_review_result(
                ReviewResult(summary="capacity-only", comments=(), provider="internal"),
                state,
            )
            comments = {e.obligation_id: c for e, c in zip(entries, projected.comments)}
        # Pure finalizer previews are sizing witnesses, not committed state.
        for preview in (
            *getattr(self, "_review_update_capacity_states", ()),
            *getattr(self, "_finalizer_capacity_states", ()),
        ):
            if type(preview) is not type(state) or (
                preview.head_sha,
                preview.base_sha,
            ) != (self.head_sha, self.base_sha):
                raise ReviewInputError(
                    "finding lifecycle review update sizing scope changed"
                )
            variants.append(preview)
        receipts = {
            r["instance"]: r
            for r in self.reviewer.native_finding_receipts(
                repository_id=self.identity.repository_id,
                pull_request=self.identity.pull_request,
            )
        }
        producer_context = getattr(self.reviewer, "_native_finding_context", None)
        if not isinstance(producer_context, dict):
            producer_context = {}
        producer_id = producer_context.get("producer_id") or 9_223_372_036_854_775_807
        assembled = producer_context.get("assembled")
        inline_instances = (
            {
                finding_instance_fingerprint(c)
                for c, _, _, _ in assembled.inline_comments
            }
            if assembled is not None
            else None
        )
        body_instances = (
            {item.instance for item in assembled.body_explanations}
            if assembled is not None
            else set()
        )
        for i, entry in enumerate(entries):
            comment = comments.get(entry.obligation_id)
            if comment is None:
                continue
            instance = finding_instance_fingerprint(comment)
            if (
                inline_instances is not None
                and instance not in inline_instances
                and instance not in body_instances
            ):
                continue
            body_only = instance in body_instances
            if instance not in receipts:
                receipts[instance] = {
                    "schema_version": "native-finding-receipt-v1",
                    "repository_id": self.identity.repository_id,
                    "pull_request": self.identity.pull_request,
                    "head_sha": self.head_sha,
                    "base_sha": self.base_sha,
                    "producer_id": producer_id,
                    "review_id": 9_223_372_036_854_775_807,
                    "instance": instance,
                    "body_sha256": hashlib.sha256(
                        ("capacity-body:" + instance).encode()
                    ).hexdigest(),
                    "thread_id": None
                    if body_only
                    else "capacity_" + entry.obligation_id,
                    "comment_id": None if body_only else 9_223_372_036_854_775_807 - i,
                    "native_head_sha": None if body_only else self.head_sha,
                }
        effects = copy.deepcopy(self.effects)
        # Every compact response has a fixed closed shape. Enumerate nullable
        # identity/mutation alternatives; native values themselves live outside
        # compressed bytes and receive their independent maximum allocations.
        candidates = []
        paired_variants = []
        for variant in variants:
            paired_variants.append((variant, receipts))
            updates = getattr(self, "_batch_body_updates", ())
            if updates:
                final_receipts = dict(receipts)
                for old, updated in updates:
                    final_receipts[old["instance"]] = updated
                    variant = self._updated_binding_proof_state(variant, old, updated)
                paired_variants.append((variant, final_receipts))
        for variant, variant_receipts in paired_variants:
            history = {
                oid: values
                for (
                    rid,
                    pr,
                    oid,
                ), values in self.reviewer._drift_concern_history.items()
                if (rid, pr)
                == (self.identity.repository_id, self.identity.pull_request)
            }
            if not isinstance(variant, ActiveWindowRoundState):
                for ob in variant.inventory.obligations:
                    concerns = {
                        ob.criterion.original_fingerprint,
                        finding_lifecycle_for_comment(
                            ob.synthetic_comment_for_placement()
                        ).fingerprint,
                    }
                    if ob.latest_comment is not None:
                        concerns.add(
                            finding_lifecycle_for_comment(ob.latest_comment).fingerprint
                        )
                    history[ob.obligation_id] = history.get(
                        ob.obligation_id, frozenset()
                    ) | frozenset(concerns)
            key = (self.identity.repository_id, self.identity.pull_request)
            input_identity = (
                self._input_identity
                or self.reviewer._drift_inputs.get(key)
                or self.reviewer._active_window_inputs.get(key)
            )
            lifecycle = encode_lifecycle_state(
                variant, input_identity=input_identity, concern_history=history
            )
            effect = effects[-1]
            options: list[dict[str, Any]]
            if effect["path"] == "/graphql":
                target = _thread_effect_target(effect)
                if target is None:
                    raise ReviewInputError(
                        "finding lifecycle mutation target is unsupported"
                    )
                name, target_id, resolved = target
                options = [
                    {
                        "id": None,
                        "producer_id": None,
                        "app_id": None,
                        "mutation": {
                            "name": name,
                            "thread_id": target_id,
                            "resolved": resolved,
                        },
                    }
                ]
            elif effect["method"] == "PUT":
                producer = self.ledger.evidence_producer()
                if (
                    not producer.startswith("github-bot:")
                    or not producer.split(":", 1)[1].isdecimal()
                ):
                    raise ReviewInputError(
                        "finding lifecycle review update producer is unqualified"
                    )
                options = [
                    {
                        "id": int(effect["path"].rsplit("/", 1)[-1]),
                        "producer_id": int(producer.split(":", 1)[1]),
                        "app_id": None,
                        "mutation": None,
                    }
                ]
            elif "check_update_precondition" in effect:
                precondition = effect["check_update_precondition"]
                options = [
                    {
                        "id": precondition["check_run_id"],
                        "producer_id": None,
                        "app_id": precondition["app_id"],
                        "mutation": None,
                    }
                ]
            elif "/check-runs" in effect["path"]:
                options = [
                    {
                        "id": 9_223_372_036_854_775_807,
                        "producer_id": None,
                        "app_id": app,
                        "mutation": None,
                    }
                    for app in (None, 9_223_372_036_854_775_807)
                ]
            else:
                options = [
                    {
                        "id": 9_223_372_036_854_775_807,
                        "producer_id": producer_id,
                        "app_id": None,
                        "mutation": None,
                    }
                ]
            for response in options:
                effects[-1]["status"] = "confirmed"
                effects[-1]["response"] = [200, response]
                candidate_receipts = list(variant_receipts.values())
                candidate_lifecycle = lifecycle
                for refreshed in (
                    (False, True)
                    if "review_update_precondition" in effect
                    else (False,)
                ):
                    if refreshed:
                        precondition = effect["review_update_precondition"]
                        new_digest = effect["request"]["body"]["$body_sha256"]
                        candidate_state = variant
                        candidate_receipts = []
                        for old in variant_receipts.values():
                            updated = old
                            if (
                                old["review_id"] == precondition["review_id"]
                                and old["thread_id"] is None
                            ):
                                updated = {**old, "body_sha256": new_digest}
                                candidate_state = self._updated_binding_proof_state(
                                    candidate_state, old, updated
                                )
                            candidate_receipts.append(updated)
                        candidate_lifecycle = encode_lifecycle_state(
                            candidate_state,
                            input_identity=input_identity,
                            concern_history=history,
                        )
                    document = self._document(
                        lifecycle=candidate_lifecycle,
                        effects=effects,
                        native_receipts=candidate_receipts,
                    )
                    outcomes = [
                        ("confirmed", document["effects"][-1]["response"]),
                        ("pending", None),
                    ]
                    outcomes.extend(
                        ("rejected", [status, None])
                        for status in (400, 401, 403, 404, 405, 410)
                    )
                    for effect_status, effect_response in outcomes:
                        for unknown_hold in {document["unknown_trust_effect"], True}:
                            outcome = copy.deepcopy(document)
                            outcome["unknown_trust_effect"] = unknown_hold
                            outcome["effects"][-1]["status"] = effect_status
                            outcome["effects"][-1]["response"] = effect_response
                            candidates.append(
                                _maximum_native_allocation(
                                    _pack_journal(outcome),
                                    _pair_source=self
                                    if outcome.get("schema_version")
                                    == "finding-lifecycle-journal-v3"
                                    else None,
                                    preserve_observed=True,
                                )
                            )
        future = max(candidates, key=lambda v: len(canonical_bytes(v)))
        pending = self._pending_wire(self._document())
        if capacity_only:
            return pending, future, effects
        plan = preflight_finding_lifecycle(
            self.ledger,
            self._record,
            document=pending,
            future_documents=(future,),
            operation_id=self.operation_id,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            policy_digest=self.policy_digest,
            configuration_digest=self.configuration_digest,
            item_count=len(self.effects) + 1,
            exact_documents=self._exact_pending_proof((pending, future)),
            reserved_tail_calls=6
            + int(any("review_update_precondition" in e for e in self.effects[-1:])),
        )
        self._future_wire_bound = len(canonical_bytes(future))
        # Reserve the proven future checkpoint and unchanged native/terminal
        # allowance, rather than the obsolete five-call local checkpoint guess.
        # Any whole-plan census overhead stays reserved conservatively here.
        self._native_receipt_tail_calls = (
            plan["conditional_calls"]
            - plan["checkpoints"][0]["conditional_calls"]
            + plan["reserved_tail_calls"]
        )
        return plan

    def checkpoint(self, *, reserved_tail_calls=0, pending_intent=False):
        from review_sensei.finding_lifecycle_store import stage_finding_lifecycle

        if self._record is None:
            loaded = self.ledger.load(self.identity)
            if loaded.status not in {"ok", "migrated"} or loaded.record is None:
                raise ReviewInputError(
                    "finding lifecycle authority disappeared before checkpoint"
                )
            self._record = loaded.record
        document = self._document()
        _require_effect_scope(document, self.identity, self.head_sha)
        packed = (
            self._pending_wire(document) if pending_intent else _pack_journal(document)
        )
        bound = getattr(self, "_future_wire_bound", None)
        if bound is not None and len(canonical_bytes(packed)) > bound:
            raise ReviewInputError(
                "finding lifecycle actual checkpoint exceeds pre-effect allocation; intent retained"
            )
        from contextlib import nullcontext

        from .api_run_requests import ApiRunSessionLedger
        from .finding_lifecycle_dictionary import is_dictionary

        dependency_scope = nullcontext()
        if is_dictionary(packed):
            guard = getattr(self.ledger, "finding_dictionary_checkpoint", None)
            if (
                not callable(guard)
                or getattr(guard, "__func__", None)
                is not ApiRunSessionLedger.finding_dictionary_checkpoint
            ):
                raise ReviewInputError(
                    "finding dictionary writer dependency fence is unqualified"
                )
            dependency_scope = guard(self._record, packed)
        from review_sensei.publication_intent_fusion import (
            begin_checkpoint,
            confirm_checkpoint,
        )

        fusion_before = self._record
        fused = begin_checkpoint(self, packed)
        with dependency_scope:
            self._record = stage_finding_lifecycle(
                self.ledger,
                self._record,
                document=packed,
                operation_id=self.operation_id,
                base_sha=self.base_sha,
                head_sha=self.head_sha,
                policy_digest=self.policy_digest,
                configuration_digest=self.configuration_digest,
                item_count=len(self.effects) + 1,
                reserved_tail_calls=reserved_tail_calls,
            )

        if fused:
            confirm_checkpoint(self, fusion_before)

        from .accepted_semantic_history import record_checkpoint_semantics

        record_checkpoint_semantics(self.ledger, self.identity, self._record, document)

    def plan(self, state, input_key):
        if state is None:
            return
        if (
            any(e["status"] == "pending" for e in self.effects)
            and state.head_sha != self.head_sha
        ):
            raise ReviewInputError(
                "finding lifecycle unknown effect requires original operation reconciliation"
            )
        self._planned = state
        self._input_identity = (self.head_sha, self.base_sha, input_key)
        self._armed = True

    def prepared_completion(self, prepared):
        """Advance a legacy round CAS only through this journal's own writes.

        The returned root still faces the ledger's fresh completion CAS. Any
        change outside lifecycle/generation/timestamp is a competing authority,
        not permission to refresh an old prepared reservation.
        """
        from dataclasses import fields, replace

        current = self._record
        before = prepared.record
        if (
            current is None
            or self._initial_record is None
            or self._initial_record.record_sha256 != before.record_sha256
            or prepared.reservation_id is None
            or self.operation_id != prepared.reservation_id
            or current.reservation_id != prepared.reservation_id
            or current.identity() != self.identity
            or current.generation < before.generation
            or any(effect["status"] == "pending" for effect in self.effects)
            or any(
                getattr(current, field.name) != getattr(before, field.name)
                for field in fields(before)
                if field.name
                not in {
                    "generation",
                    "updated_at",
                    "record_sha256",
                    "finding_lifecycle",
                }
            )
        ):
            raise ReviewInputError(
                "finding lifecycle prepared completion authority conflicts"
            )
        return replace(prepared, record=current)

    def finish(self):
        if not self._armed:
            return
        key = (self.identity.repository_id, self.identity.pull_request)
        current = self.reviewer._drift_states.get(
            key
        ) or self.reviewer._active_window_states.get(key)
        if (
            current is not None
            and current.head_sha == self.head_sha
            and current.base_sha == self.base_sha
        ):
            self._planned = None
            self._input_identity = None
        for effect_index, effect in enumerate(self.effects):
            if effect["status"] == "pending":
                from .guarded_publication_batch import require_cold_pair_order

                try:
                    require_cold_pair_order(self, effect_index)
                except ReviewInputError:
                    self.checkpoint()
                    raise
                http = getattr(self, "_readback_http", None)
                token = (
                    self._check_token
                    if "/check-runs" in effect["path"]
                    else self._review_token
                )
                response = (
                    None
                    if http is None or token is None
                    else self._readback(http, effect, token=token)
                )
                if response is None:
                    self.checkpoint()
                    raise ReviewInputError(
                        "finding lifecycle native intent remains uncertain; terminal accounting withheld"
                    )
                from .guarded_publication_batch import validate_review_receipt

                receipt = self._compact_response(response[1], effect=effect)
                validate_review_receipt(self, effect, receipt)
                effect["status"], effect["response"] = (
                    "confirmed",
                    [response[0], receipt],
                )
        self.checkpoint()

    @staticmethod
    def _is_effect(method, path, body):
        if method not in {"POST", "PATCH", "PUT", "DELETE"}:
            return False
        if path == "/graphql":
            return isinstance(body, dict) and str(
                body.get("query", "")
            ).lstrip().startswith("mutation")
        return True

    def request(
        self, http, method, path, *, token, body=None, _prepare_only=False, **kwargs
    ):
        if "/check-runs" in path:
            self._check_token = token
        elif method != "GET":
            self._review_token = token
        if not self._armed or not self._is_effect(method, path, body):
            self._observe_review_read(method, path, (0, None))
            self._observe_check_read(method, path, (0, None))
            response = http.request(method, path, token=token, body=body, **kwargs)
            self._observe_review_read(method, path, response)
            self._observe_check_read(method, path, response)
            return response
        if getattr(self, "_reply_native_stopped", False):
            raise ReviewInputError(
                "reply cleanup incomplete; later native dispatch stopped"
            )
        if getattr(self, "_command_native_stopped", False):
            raise ReviewInputError(
                "command cleanup incomplete; later native dispatch stopped"
            )
        raw_request = {} if body is None else body
        from .reply_finalization_plan import require_reply_native_request

        require_reply_native_request(self, method, path, raw_request)
        from .guarded_publication_batch import intercept_request

        handled, response = intercept_request(
            self,
            http,
            method,
            path,
            token=token,
            body=raw_request,
            kwargs=kwargs,
            prepare_only=_prepare_only,
        )
        if handled:
            return response
        ordered = getattr(self, "_ordered_native_batch", None)
        if ordered is not None:
            ordered.before_request(method, path, raw_request)
        request = _request_identity(raw_request)
        supported = (
            (
                method == "POST"
                and (path.endswith("/reviews") or path.endswith("/check-runs"))
            )
            or (
                method == "PATCH"
                and re.search(
                    r"/(?:check-runs|pulls/comments|reviews)/[1-9][0-9]*$", path
                )
            )
            or (
                method == "PUT"
                and re.fullmatch(
                    re.escape(
                        f"/repos/{self.identity.repository}/pulls/{self.identity.pull_request}/reviews/"
                    )
                    + r"[1-9][0-9]*",
                    path,
                )
                is not None
                and isinstance(raw_request, dict)
                and set(raw_request) == {"body"}
                and isinstance(raw_request["body"], str)
            )
            or (
                path == "/graphql"
                and ("resolveReviewThread" in str(request.get("query", "")))
            )
        )
        if not supported:
            raise ReviewInputError(
                "finding lifecycle native effect requires a supported exact readback contract"
            )
        key = _digest({"method": method, "path": path, "request": request})
        legacy_key = _digest({"method": method, "path": path, "request": raw_request})
        found = next(
            (e for e in self.effects if e["request_sha256"] in {key, legacy_key}), None
        )
        if found is None and method == "PATCH" and "/check-runs/" in path:
            for old in self.effects:
                if (
                    old["method"] == "POST"
                    and old["path"].endswith("/check-runs")
                    and old["status"] == "confirmed"
                    and isinstance(old["response"][1], dict)
                    and path == old["path"] + "/" + str(old["response"][1].get("id"))
                    and all(
                        old["request"].get(field) == value
                        for field, value in request.items()
                    )
                ):
                    found = old
                    break
        prepared = getattr(self, "_prepared_review_update", None)
        if found is not None and not _prepare_only and prepared is not None:
            effect, record, root = prepared
            caller = getattr(self.ledger, "caller", None)
            if (
                found is not effect
                or found["status"] != "pending"
                or found["method"] != method
                or found["path"] != path
                or found["request"] != request
                or found["request_sha256"] != key
                or self._record is not record
                or record.record_sha256 != root
                or (caller is not None and caller._root != root)
            ):
                raise ReviewInputError(
                    "finding lifecycle prepared review update authority changed"
                )
            self._prepared_review_update = None
            return self._dispatch_effect(
                http, found, method, path, token=token, body=body, **kwargs
            )
        if found is not None:
            if found["status"] == "rejected":
                return tuple(found["response"])
            # A retained response is knowledge, not proof that a mutable native
            # projection is unchanged. Re-read before substituting it for a write.
            response = self._readback(http, found, token=token)
            if response is None:
                raise ReviewInputError(
                    "finding lifecycle native outcome remains uncertain; no retry dispatched"
                )
            if found["status"] == "pending":
                found["status"], found["response"] = (
                    "confirmed",
                    [response[0], self._compact_response(response[1], effect=found)],
                )
                self.checkpoint()
            return response
        if any(e["status"] == "pending" for e in self.effects):
            raise ReviewInputError(
                "finding lifecycle prior native effect remains uncertain"
            )
        if len(self.effects) >= MAX_EFFECTS:
            raise ReviewInputError(
                "finding lifecycle operation effect capacity exceeded before dispatch"
            )
        effect = {
            "method": method,
            "path": path,
            "request": request,
            "request_sha256": key,
            "status": "pending",
            "response": None,
        }
        if method == "PATCH" and "/check-runs/" in path:
            precondition = self._check_precondition(path)
            if precondition is not None:
                effect["check_update_precondition"] = precondition
        if method == "PUT":
            review_id = int(path.rsplit("/", 1)[-1])
            if self._body_receipts(review_id):
                precondition = getattr(self, "_pending_update_precondition", None)
                if precondition is None or precondition["review_id"] != review_id:
                    raise ReviewInputError(
                        "finding lifecycle body update lacks authenticated precondition"
                    )
                effect["review_update_precondition"] = dict(precondition)
        allowed = getattr(self, "_command_prepriced_request_keys", None)
        if allowed is not None and key not in allowed:
            raise ReviewInputError(
                "command finalization request is outside its source plan"
            )
        expected = getattr(self, "_command_prepriced_pending_documents", {}).get(key)
        if (
            expected is not None
            and canonical_bytes(
                _pack_journal(self._document(effects=[*self.effects, effect]))
            )
            != expected
        ):
            if ordered is not None:
                ordered.stop()
            raise ReviewInputError("command finalization pending checkpoint changed")
        self.effects.append(effect)
        try:
            self._preflight_effect()
        except BaseException:
            # Pure/read-only qualification ran before any intent checkpoint or
            # native dispatch. It cannot manufacture an unknown native effect.
            self.effects.pop()
            raise
        # A pending root must leave funding for the known receipt/state commit
        # and ordinary terminal accounting, including the native dispatch.
        self.checkpoint(
            reserved_tail_calls=getattr(self, "_native_receipt_tail_calls", 11),
            pending_intent=True,
        )
        if _prepare_only:
            if self._record is None:
                raise ReviewInputError(
                    "finding lifecycle prepared review update has no owned root"
                )
            self._prepared_review_update = (
                effect,
                self._record,
                self._record.record_sha256,
            )
            return 0, None
        return self._dispatch_effect(
            http, effect, method, path, token=token, body=body, **kwargs
        )

    def _dispatch_effect(self, http, effect, method, path, *, token, body, **kwargs):
        from .errors import GitHubHTTPTransientError
        from .ordinary_native_recovery import native_attempt, recover

        with native_attempt(self, effect, method, path, body, token) as capture:
            try:
                response = http.request(method, path, token=token, body=body, **kwargs)
            except GitHubHTTPTransientError:
                recovered = recover(capture, token=token)
                if recovered is None:
                    raise
                return recovered
        if len(canonical_bytes(response[1])) > MAX_RECEIPT_BYTES:
            raise ReviewInputError(
                "finding lifecycle native receipt exceeds response capacity; intent retained"
            )
        if 200 <= response[0] < 300:
            if "check_update_precondition" in effect and (
                response[0] != 200
                or not self._check_update_matches(effect, response[1])
            ):
                raise ReviewInputError(
                    "finding lifecycle check update result is unconfirmed"
                )
            if effect["method"] == "PUT":
                commitment = self._native_review_commitment(response[1])
                if (
                    response[0] != 200
                    or commitment is None
                    or commitment["review_id"] != int(effect["path"].rsplit("/", 1)[-1])
                    or not _body_matches(
                        response[1].get("body"), effect["request"].get("body")
                    )
                ):
                    raise ReviewInputError(
                        "finding lifecycle review update result is unconfirmed"
                    )
            if effect["path"] == "/graphql" and response[0] != 200:
                raise ReviewInputError(
                    "finding lifecycle mutation status is unconfirmed"
                )
            receipt = self._compact_response(response[1], effect=effect)
            from .guarded_publication_batch import validate_review_receipt

            validate_review_receipt(self, effect, receipt)
            # Validate before changing pending. A foreign/malformed successful
            # response never becomes a confirmed journal receipt.
            effect["status"], effect["response"] = "confirmed", [response[0], receipt]
        elif response[0] in {400, 401, 403, 404, 405, 410}:
            effect["status"], effect["response"] = "rejected", [response[0], None]
        # Ambiguous rejection and server failures remain pending. A response is
        # not proof that the native business effect did not happen.
        # The durable pending intent remains authoritative until the following
        # checkpoint. A crash here is reconciled by native readback, not replay.
        return response

    def _check_commitment(self, value):
        if not isinstance(value, dict):
            return None
        app = value.get("app")
        slug = (self.app_slug or "").removesuffix("[bot]").casefold()
        if (
            type(value.get("id")) is not int
            or value["id"] <= 0
            or value.get("head_sha") != self.head_sha
            or value.get("name") != "ReviewSensei"
            or not isinstance(app, dict)
            or type(app.get("id")) is not int
            or app["id"] <= 0
            or not isinstance(app.get("slug"), str)
            or app["slug"].casefold() != slug
        ):
            return None
        return {
            "check_run_id": value["id"],
            "head_sha": self.head_sha,
            "app_id": app["id"],
            "app_slug": slug,
        }

    def _observe_check_read(self, method, path, response):
        from urllib.parse import parse_qs, urlsplit

        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if (
            method != "GET"
            or parsed.path
            != f"/repos/{self.identity.repository}/commits/{self.head_sha}/check-runs"
            or query.get("check_name") != ["ReviewSensei"]
            or query.get("per_page") != ["100"]
        ):
            return
        self._observed_check_reads = {}
        status, payload = response
        runs = payload.get("check_runs") if isinstance(payload, dict) else None
        if (
            status != 200
            or not isinstance(runs, list)
            or len(runs) >= 100
            or payload.get("total_count", len(runs)) != len(runs)
        ):
            return
        for run in runs:
            commitment = self._check_commitment(run)
            if commitment is not None:
                self._observed_check_reads[commitment["check_run_id"]] = (
                    run,
                    commitment,
                )

    def _check_precondition(self, path):
        matched = re.fullmatch(
            re.escape(f"/repos/{self.identity.repository}/check-runs/")
            + r"([1-9][0-9]*)",
            path,
        )
        if matched is None:
            return None
        observed = getattr(self, "_observed_check_reads", {}).get(int(matched[1]))
        if observed is None:
            return None
        if self._check_commitment(observed[0]) != observed[1]:
            raise ReviewInputError("finding lifecycle observed check identity changed")
        return dict(observed[1])

    def _check_update_matches(self, effect, payload):
        precondition = effect.get("check_update_precondition")
        return (
            isinstance(precondition, dict)
            and self._check_commitment(payload) == precondition
            and payload.get("errors") in (None, [])
            and _request_matches(payload, effect["request"])
        )

    def _body_receipts(self, review_id):
        return [
            r
            for r in self.reviewer.native_finding_receipts(
                repository_id=self.identity.repository_id,
                pull_request=self.identity.pull_request,
            )
            if r["review_id"] == review_id
            and r["thread_id"] is None
            and r["comment_id"] is None
        ]

    def _native_review_commitment(self, value):
        if not isinstance(value, dict):
            return None
        user = value.get("user")
        producer = self.ledger.evidence_producer()
        if (
            type(value.get("id")) is not int
            or value["id"] <= 0
            or value.get("commit_id") != self.head_sha
            or not isinstance(value.get("body"), str)
            or value.get("edited_by_human") is True
            or not isinstance(user, dict)
            or type(user.get("id")) is not int
            or user["id"] <= 0
            or user.get("type") != "Bot"
            or not producer.startswith("github-bot:")
            or str(user["id"]) != producer.split(":", 1)[1]
        ):
            return None
        return {
            "review_id": value["id"],
            "head_sha": self.head_sha,
            "producer_id": user["id"],
            "body_sha256": hashlib.sha256(value["body"].encode("utf-8")).hexdigest(),
        }

    def _observe_review_read(self, method, path, response):
        from urllib.parse import urlsplit

        if method != "GET":
            return
        prefix = f"/repos/{self.identity.repository}/pulls/{self.identity.pull_request}/reviews"
        target = urlsplit(path).path
        if (
            target != prefix
            and re.fullmatch(re.escape(prefix) + r"/[1-9][0-9]*", target) is None
        ):
            return
        body_ids = {
            r["review_id"]
            for r in self.reviewer.native_finding_receipts(
                repository_id=self.identity.repository_id,
                pull_request=self.identity.pull_request,
            )
            if r["thread_id"] is None and r["comment_id"] is None
        }
        observed = getattr(self, "_observed_review_reads", {})
        if target == prefix:
            observed = {review_id: (None, None) for review_id in body_ids}
        if target != prefix:
            selected = int(target.rsplit("/", 1)[-1])
            if selected in body_ids:
                observed[selected] = (None, None)
        if response[0] == 200:
            values = response[1] if isinstance(response[1], list) else [response[1]]
            for value in values:
                if (
                    isinstance(value, dict)
                    and type(value.get("id")) is int
                    and value["id"] in body_ids
                ):
                    observed[value["id"]] = (
                        value,
                        self._native_review_commitment(value),
                    )
        # At most one observation per already-bounded retained body review.
        self._observed_review_reads = observed

    def issue_review_update_precondition(self, native_review):
        review_id = native_review.get("id") if isinstance(native_review, dict) else None
        if not self._body_receipts(review_id):
            return None
        observed = getattr(self, "_observed_review_reads", {}).get(review_id)
        commitment = self._native_review_commitment(native_review)
        if (
            observed is None
            or observed[0] is not native_review
            or commitment is None
            or observed[1] != commitment
        ):
            raise ReviewInputError(
                "finding lifecycle review precondition lacks source-owned read"
            )
        receipts = self._body_receipts(review_id)
        if not self._receipts_match_update_precondition(receipts, commitment):
            recovered_update = any(
                e["method"] == "PUT"
                and e["path"]
                == f"/repos/{self.identity.repository}/pulls/{self.identity.pull_request}/reviews/{review_id}"
                and e["status"] != "rejected"
                and self._receipts_match_update_precondition(
                    receipts,
                    e.get("review_update_precondition"),
                    new_body=e["request"].get("body"),
                )
                and _body_matches(native_review.get("body"), e["request"].get("body"))
                for e in self.effects
            )
            if not recovered_update:
                raise ReviewInputError(
                    "finding lifecycle native body changed without owned update proof"
                )
        proof = object()
        self._issued_review_update_precondition = (
            proof,
            dict(commitment),
            native_review,
        )
        return proof

    def _receipts_match_update_precondition(
        self, receipts, precondition, *, new_body=None
    ):
        if not isinstance(precondition, dict) or set(precondition) != {
            "review_id",
            "head_sha",
            "producer_id",
            "body_sha256",
        }:
            return False
        allowed = {precondition["body_sha256"]}
        if isinstance(new_body, dict) and isinstance(new_body.get("$body_sha256"), str):
            allowed.add(new_body["$body_sha256"])
        return bool(receipts) and all(
            r["repository_id"] == self.identity.repository_id
            and r["pull_request"] == self.identity.pull_request
            and r["head_sha"] == self.head_sha == precondition["head_sha"]
            and r["base_sha"] == self.base_sha
            and r["review_id"] == precondition["review_id"]
            and r["producer_id"] == precondition["producer_id"]
            and r["body_sha256"] in allowed
            for r in receipts
        )

    def _refresh_body_update_receipts(self, effect, native_review):
        review_id = native_review.get("id")
        receipts = self._body_receipts(review_id)
        if not receipts:
            return "review_update_precondition" not in effect
        precondition = effect.get("review_update_precondition")
        commitment = self._native_review_commitment(native_review)
        if (
            commitment is None
            or not self._receipts_match_update_precondition(
                receipts, precondition, new_body=effect["request"].get("body")
            )
            or any(
                commitment[k] != precondition[k]
                for k in ("review_id", "head_sha", "producer_id")
            )
            or not _body_matches(
                native_review.get("body"), effect["request"].get("body")
            )
        ):
            return False
        key = (self.identity.repository_id, self.identity.pull_request)
        records = self.reviewer._native_finding_receipt_records[key]
        for old in receipts:
            updated = {**old, "body_sha256": commitment["body_sha256"]}
            self._refresh_binding_proof(old, updated)
            records[old["instance"]] = updated
        return True

    def _updated_binding_proof_state(self, state, old, updated):
        from dataclasses import replace

        from review_sensei.active_finding_window import ActiveWindowRoundState

        from .publication import NativeFindingReceipt

        before = NativeFindingReceipt(
            **{k: v for k, v in old.items() if k != "schema_version"}
        )
        after = NativeFindingReceipt(
            **{k: v for k, v in updated.items() if k != "schema_version"}
        )
        if before.proof_sha256 == after.proof_sha256:
            return state
        target, old_proof, new_proof = (
            before.target,
            before.proof_sha256,
            after.proof_sha256,
        )

        def slots(values):
            return tuple(
                replace(s, proof_sha256=new_proof)
                if s.target == target
                and s.state != "released"
                and s.proof_sha256 == old_proof
                else s
                for s in values
            )

        def refresh(state):
            if isinstance(state, ActiveWindowRoundState):
                inventory = state.authoritative_decision_inventory
                obligations = tuple(
                    replace(o, target_proof_sha256=new_proof)
                    if o.native_target == target
                    and o.target_state != "released"
                    and o.target_proof_sha256 == old_proof
                    else o
                    for o in inventory.obligations
                )
                return replace(
                    state,
                    authoritative_decision_inventory=replace(
                        inventory, obligations=obligations
                    ),
                    display_active_findings=replace(
                        state.display_active_findings,
                        slots=slots(state.display_active_findings.slots),
                    ),
                )
            inventory = state.inventory
            obligations = tuple(
                replace(
                    o, thread_binding=replace(o.thread_binding, proof_sha256=new_proof)
                )
                if o.thread_binding is not None
                and o.thread_binding.thread_id == target
                and o.thread_binding.slot_state != "released"
                and o.thread_binding.proof_sha256 == old_proof
                else o
                for o in inventory.obligations
            )
            return replace(
                state,
                inventory=replace(inventory, obligations=obligations),
                slots=slots(state.slots),
            )

        return refresh(state)

    def _refresh_binding_proof(self, old, updated):
        key = (self.identity.repository_id, self.identity.pull_request)
        for states in (
            self.reviewer._active_window_states,
            self.reviewer._drift_states,
        ):
            if key in states:
                states[key] = self._updated_binding_proof_state(
                    states[key], old, updated
                )
        if self._planned is not None:
            self._planned = self._updated_binding_proof_state(
                self._planned, old, updated
            )

    @contextmanager
    def prepare_review_update(
        self, path, *, token, body, precondition=None, capacity_states=()
    ):
        """Stage exact intent before entering the native update's fixed allowance.

        The one-use dispatch capability is local to this context. Retained or
        abandoned intents still require native readback; they cannot mint it.
        """
        if getattr(self, "_prepared_review_update", None) is not None:
            raise ReviewInputError(
                "finding lifecycle review update preparation is nested"
            )
        self._pending_update_precondition = None
        review_id = int(path.rsplit("/", 1)[-1])
        body_receipts = self._body_receipts(review_id)
        if body_receipts:
            existing = next(
                (
                    e
                    for e in self.effects
                    if e["method"] == "PUT"
                    and e["path"] == path
                    and e["request"] == _request_identity(body)
                ),
                None,
            )
            if existing is None:
                issued = getattr(self, "_issued_review_update_precondition", None)
                observed = getattr(self, "_observed_review_reads", {}).get(review_id)
                if (
                    issued is None
                    or issued[0] is not precondition
                    or observed is None
                    or observed[1] != issued[1]
                    or self._native_review_commitment(observed[0]) != issued[1]
                    or self._native_review_commitment(issued[2]) != issued[1]
                ):
                    raise ReviewInputError(
                        "finding lifecycle body update precondition is unissued or changed"
                    )
                value = issued[1]
                if not self._receipts_match_update_precondition(body_receipts, value):
                    raise ReviewInputError(
                        "finding lifecycle prewrite body does not match retained receipt"
                    )
                self._pending_update_precondition = value
        self._review_update_capacity_states = tuple(capacity_states)
        try:
            self.request(
                self._readback_http,
                "PUT",
                path,
                token=token,
                body=body,
                _prepare_only=True,
            )
            yield
            effect = next(
                (
                    e
                    for e in self.effects
                    if e["method"] == "PUT"
                    and e["path"] == path
                    and e["request"] == _request_identity(body)
                ),
                None,
            )
            if (
                effect is not None
                and "review_update_precondition" in effect
                and effect["status"] != "rejected"
            ):
                response = self._readback(self._readback_http, effect, token=token)
                if response is None:
                    raise ReviewInputError(
                        "finding lifecycle owned body update readback is unconfirmed"
                    )
                effect["status"], effect["response"] = (
                    "confirmed",
                    [response[0], self._compact_response(response[1], effect=effect)],
                )
        finally:
            self._prepared_review_update = None
            self._pending_update_precondition = None
            self._issued_review_update_precondition = None
            self._review_update_capacity_states = ()

    @staticmethod
    def _compact_response(payload, *, effect=None):
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ReviewInputError(
                "finding lifecycle native receipt shape is unsupported"
            )

        def identifier(value):
            if value is not None and (
                type(value) is not int or not 0 < value <= 9_223_372_036_854_775_807
            ):
                raise ReviewInputError(
                    "finding lifecycle native numeric identity exceeds preflight bound"
                )
            return value

        user, app = payload.get("user"), payload.get("app")
        mutation = None
        if "data" in payload:
            data = payload["data"]
            if not isinstance(data, dict) or len(data) != 1:
                raise ReviewInputError(
                    "finding lifecycle mutation receipt is unsupported"
                )
            name = next(iter(data))
            if name not in {"resolveReviewThread", "unresolveReviewThread"}:
                raise ReviewInputError(
                    "finding lifecycle mutation receipt purpose is unsupported"
                )
            value = data[name]
            thread = value.get("thread") if isinstance(value, dict) else None
            if (
                not isinstance(thread, dict)
                or not isinstance(thread.get("id"), str)
                or not thread["id"]
                or not thread["id"].isprintable()
                or len(thread["id"].encode("utf-8")) > 256
                or type(thread.get("isResolved")) is not bool
            ):
                raise ReviewInputError(
                    "finding lifecycle mutation receipt is incomplete"
                )
            mutation = {
                "name": name,
                "thread_id": thread["id"],
                "resolved": thread["isResolved"],
            }
        if effect is not None:
            check = "/check-runs" in effect["path"]
            graph = effect["path"] == "/graphql"
            if graph:
                target = _thread_effect_target(effect)
                if (
                    payload.get("errors") not in (None, [])
                    or target is None
                    or canonical_bytes(mutation)
                    != canonical_bytes(
                        {
                            "name": target[0],
                            "thread_id": target[1],
                            "resolved": target[2],
                        }
                    )
                ):
                    raise ReviewInputError(
                        "finding lifecycle mutation result is not confirmed"
                    )
            if not graph and (type(payload.get("id")) is not int or payload["id"] <= 0):
                raise ReviewInputError(
                    "finding lifecycle native result identity is not confirmed"
                )
            if not check:
                app = None
            if check or graph:
                user = None
            if not graph:
                mutation = None
        return {
            "id": None
            if effect is not None and effect["path"] == "/graphql"
            else identifier(payload.get("id")),
            "producer_id": identifier(user.get("id"))
            if isinstance(user, dict)
            else None,
            "app_id": identifier(app.get("id")) if isinstance(app, dict) else None,
            "mutation": mutation,
        }

    def _readback(self, http, effect, *, token):
        path, request = effect["path"], effect["request"]
        response = effect["response"]
        if effect["method"] == "PUT":
            selected = re.fullmatch(
                re.escape(
                    f"/repos/{self.identity.repository}/pulls/{self.identity.pull_request}/reviews/"
                )
                + r"([1-9][0-9]*)",
                path,
            )
            if selected is None:
                return None
            receipts = self._body_receipts(int(selected[1]))
            if receipts and not self._receipts_match_update_precondition(
                receipts,
                effect.get("review_update_precondition"),
                new_body=request.get("body"),
            ):
                return None
        candidates = None
        confirmed_check_successor = None
        if path == "/graphql":
            return self._readback_thread(effect, token=token)
        if (
            path.endswith("/check-runs")
            and effect["method"] == "POST"
            and response is None
        ):
            status, payload = http.request(
                "GET",
                path.rsplit("/check-runs", 1)[0]
                + "/commits/"
                + str(request.get("head_sha"))
                + "/check-runs?check_name=ReviewSensei&per_page=100",
                token=token,
            )
            runs = payload.get("check_runs") if isinstance(payload, dict) else None
            if (
                status == 200
                and isinstance(runs, list)
                and payload.get("total_count", len(runs)) == len(runs)
                and len(runs) < 100
            ):
                candidates = [
                    run
                    for run in runs
                    if isinstance(run, dict)
                    and all(run.get(k) == v for k, v in request.items())
                ]
        elif path.endswith("/reviews") and effect["method"] == "POST":
            status, values = http.request(
                "GET", path + "?per_page=100&page=1", token=token
            )
            if status == 200 and isinstance(values, list) and len(values) < 100:
                candidates = [
                    value
                    for value in values
                    if isinstance(value, dict)
                    and _body_matches(value.get("body"), request.get("body"))
                    and value.get("commit_id") == request.get("commit_id")
                    and value.get("state")
                    == {
                        "COMMENT": "COMMENTED",
                        "REQUEST_CHANGES": "CHANGES_REQUESTED",
                        "APPROVE": "APPROVED",
                    }.get(request.get("event", "COMMENT"))
                ]
        elif effect["method"] in {"PATCH", "PUT"} or (
            response is not None
            and isinstance(response[1], dict)
            and type(response[1].get("id")) is int
        ):
            object_path = (
                path
                if effect["method"] in {"PATCH", "PUT"}
                else path + "/" + str(response[1]["id"])
            )
            status, value = http.request("GET", object_path, token=token)
            if status == 200 and isinstance(value, dict):
                expected_request = request
                if "/check-runs" in path:
                    # A later confirmed update in this same ordered intent log
                    # legitimately supersedes the earlier pending projection.
                    for later in self.effects[self.effects.index(effect) + 1 :]:
                        if later["path"] != object_path or later["method"] != "PATCH":
                            continue
                        if later["status"] == "confirmed":
                            expected_request = {**expected_request, **later["request"]}
                        elif (
                            later["status"] == "pending"
                            and later is self.effects[-1]
                            and sum(
                                item["status"] == "pending" for item in self.effects
                            )
                            == 1
                            and self._check_update_matches(later, value)
                        ):
                            # A response-lost final update may supersede this
                            # earlier confirmed creation. The same fresh GET
                            # proves its exact source-bound object/request; do
                            # not infer success from a generic failure state.
                            expected_request = {**expected_request, **later["request"]}
                            confirmed_check_successor = later
                candidates = (
                    [value] if _request_matches(value, expected_request) else []
                )
        if candidates is None or len(candidates) != 1:
            return None
        value = candidates[0]
        if "check_update_precondition" in effect and not self._check_update_matches(
            effect, value
        ):
            return None
        if effect["method"] == "PUT":
            expected_path = re.escape(
                f"/repos/{self.identity.repository}/pulls/{self.identity.pull_request}/reviews/"
            )
            selected = re.fullmatch(expected_path + r"([1-9][0-9]*)", path)
            if (
                selected is None
                or type(value.get("id")) is not int
                or value["id"] != int(selected[1])
                or value.get("commit_id") != self.head_sha
                or set(request) != {"body"}
            ):
                return None
        user = value.get("user", value.get("app", {}))
        # Never accept an arbitrary matching foreign projection. Exact numeric
        # ownership is independently selected by the ledger's evidence resolver.
        producer = self.ledger.evidence_producer()
        if "/check-runs" in path:
            expected_slug = (
                self.app_slug[:-5]
                if self.app_slug and self.app_slug.endswith("[bot]")
                else self.app_slug
            )
            if not isinstance(user, dict) or user.get("slug") != expected_slug:
                return None
            if response is not None and isinstance(response[1], dict):
                old_app_id = response[1].get("app_id")
                if old_app_id is not None and user.get("id") != old_app_id:
                    return None
        elif producer.startswith("github-bot:"):
            if (
                not isinstance(user, dict)
                or str(user.get("id")) != producer.split(":", 1)[1]
            ):
                return None
        elif (
            not isinstance(user, dict)
            or user.get("type") != "Bot"
            or user.get("login") != self.app_slug
        ):
            return None
        producer_context = getattr(self.reviewer, "_native_finding_context", None)
        expected_producer = (
            producer_context.get("producer_id")
            if isinstance(producer_context, dict)
            else None
        )
        if (
            "user" in value
            and expected_producer is not None
            and user.get("id") != expected_producer
        ):
            return None
        if value.get("edited_by_human") is True:
            return None
        if (
            response is not None
            and isinstance(response[1], dict)
            and value.get("id") != response[1].get("id")
        ):
            return None
        if effect["method"] == "PUT" and not self._refresh_body_update_receipts(
            effect, value
        ):
            return None
        if path.endswith("/reviews") and effect["method"] == "POST":
            rid = value.get("id")
            if type(rid) is not int or rid <= 0:
                return None
            expected = request.get("comments", [])
            status, inline = http.request(
                "GET",
                path + "/" + str(rid) + "/comments?per_page=100&page=1",
                token=token,
            )
            if (
                status != 200
                or not isinstance(inline, list)
                or len(inline) >= 100
                or not isinstance(expected, list)
                or len(inline) != len(expected)
            ):
                return None

            def signature(comment):
                normalized = _request_identity(comment)
                return tuple(
                    normalized.get(field)
                    if field != "body"
                    else canonical_bytes(normalized.get(field))
                    for field in ("path", "line", "side", "body")
                )

            if sorted(map(signature, inline), key=repr) != sorted(
                map(signature, expected), key=repr
            ):
                return None
            producer_context = getattr(self.reviewer, "_native_finding_context", None)
            producer_id = (
                producer_context.get("producer_id")
                if isinstance(producer_context, dict)
                else None
            )
            if producer.startswith("github-bot:"):
                producer_id = int(producer.split(":", 1)[1])
            for comment in inline:
                author = comment.get("user")
                if (
                    not isinstance(author, dict)
                    or author.get("login") != self.app_slug
                    or author.get("type") != "Bot"
                    or type(comment.get("id")) is not int
                    or comment.get("pull_request_review_id") != rid
                    or (producer_id is not None and author.get("id") != producer_id)
                ):
                    return None
            if inline and not self._adopt_review_readback(value, inline, token=token):
                return None
            # Body-only recovery uses this already authenticated unique review
            # and complete empty inline inventory. Keep issuance inside the real
            # readback, not a helper accepting an arbitrary caller-supplied body.
            if not inline:
                from review_sensei.active_finding_window import (
                    ActiveWindowRoundState,
                    reconcile_uncertain_slot,
                )
                from review_sensei.human_assessment import finding_instance_fingerprint
                from review_sensei.presentation import render_finding

                from .publication import (
                    AssembledReview,
                    NativeFindingReceipt,
                    parse_finding_index,
                )

                context = getattr(self.reviewer, "_native_finding_context", None)
                key = (self.identity.repository_id, self.identity.pull_request)
                state = self.reviewer._active_window_states.get(key)
                if (
                    type(producer_id) is int
                    and producer_id > 0
                    and isinstance(context, dict)
                    and (
                        context.get("repository_id"),
                        context.get("pull_request"),
                        context.get("head_sha"),
                        context.get("base_sha"),
                        context.get("producer_id"),
                    )
                    == (*key, self.head_sha, self.base_sha, producer_id)
                ):
                    assembled = context.get("assembled")
                    body = value.get("body")
                    index = (
                        parse_finding_index(
                            body,
                            repository_id=key[0],
                            pull_request=key[1],
                            head_sha=self.head_sha,
                        )
                        if isinstance(body, str)
                        else None
                    )
                    if (
                        type(assembled) is AssembledReview
                        and isinstance(body, str)
                        and index is not None
                        and index.base_sha == self.base_sha
                    ):
                        indexed = {
                            entry.instance
                            for entry in index.entries
                            if entry.body_placed
                        }
                        explanations = {
                            item.instance: item for item in assembled.body_explanations
                        }
                        receipts = {
                            instance: NativeFindingReceipt(
                                self.identity.repository_id,
                                self.identity.pull_request,
                                self.head_sha,
                                self.base_sha,
                                producer_id,
                                rid,
                                instance,
                                hashlib.sha256(body.encode("utf-8")).hexdigest(),
                            )
                            for instance, item in explanations.items()
                            if instance in indexed and render_finding(item.view) in body
                        }
                        if isinstance(state, ActiveWindowRoundState):
                            for (
                                obligation
                            ) in state.authoritative_decision_inventory.obligations:
                                if (
                                    obligation.target_state != "uncertain"
                                    or obligation.native_target is None
                                    or obligation.comment is None
                                ):
                                    continue
                                receipt = receipts.get(
                                    finding_instance_fingerprint(obligation.comment)
                                )
                                if receipt is not None and (
                                    obligation.native_target.startswith("intent:")
                                    or obligation.native_target == receipt.target
                                ):
                                    state = reconcile_uncertain_slot(
                                        state,
                                        target=obligation.native_target,
                                        resolved_state="active",
                                        proof_sha256=receipt.proof_sha256,
                                        confirmed_target=receipt.target,
                                    )
                        if receipts:
                            if isinstance(state, ActiveWindowRoundState):
                                self.reviewer._active_window_states[key] = state
                            self.reviewer._native_finding_receipt_records.setdefault(
                                key, {}
                            ).update(
                                {
                                    instance: receipt.to_dict()
                                    for instance, receipt in receipts.items()
                                }
                            )
                            context["receipts"] = (rid, receipts)
                            if self._planned is not None and isinstance(
                                state, ActiveWindowRoundState
                            ):
                                self._planned = state
        if confirmed_check_successor is not None:
            # Mutate only after every original object/owner/body guard above
            # passed. As with an immediate native response, the next intent or
            # finish checkpoint retains this exact receipt before any terminal
            # accounting; a crash before then simply requires fresh readback.
            receipt = [
                200,
                self._compact_response(value, effect=confirmed_check_successor),
            ]
            (
                confirmed_check_successor["status"],
                confirmed_check_successor["response"],
            ) = ("confirmed", receipt)
        return (200, value)

    def _adopt_review_readback(self, review, inline, *, token):
        """Bind exact retained review intents to freshly opened native threads.

        Review/readback equality alone cannot clear an uncertain slot. The
        existing bounded native-thread reader supplies numeric comment/thread
        identity and independent author, review, head and body bindings. No
        caller-supplied receipt or synthetic target can promote a slot here.
        """
        from dataclasses import replace

        from review_sensei.active_finding_window import (
            ActiveWindowRoundState,
            reconcile_uncertain_slot,
        )
        from review_sensei.finding_drift import commit_drift_publication
        from review_sensei.human_assessment import finding_instance_fingerprint

        from .publication import (
            _native_finding_receipts_from_nodes,
            finding_instance_from_body,
        )

        producer_id = review.get("user", {}).get("id")
        if type(producer_id) is not int or producer_id <= 0:
            return False
        expected = {}
        anchors = {}
        for comment in inline:
            instance = finding_instance_from_body(comment.get("body"))
            if instance is None or instance in expected:
                return False
            expected[instance] = comment["body"]
            anchors[instance] = (comment.get("path"), comment.get("line"))
        previous_budget = self.reviewer.finalizer.evidence_budget
        budget = self._native_budget or getattr(self.ledger, "evidence_budget", None)
        if budget is None:
            return False
        self.reviewer.finalizer.evidence_budget = budget
        try:
            self.reviewer._published_finding_suppression(
                token=token,
                repository=self.identity.repository,
                repository_id=self.identity.repository_id,
                pull_request=self.identity.pull_request,
                head_sha=self.head_sha,
                base_sha=self.base_sha,
                app_slug=self.app_slug,
            )
        finally:
            self.reviewer.finalizer.evidence_budget = previous_budget
        receipts = _native_finding_receipts_from_nodes(
            getattr(self.reviewer, "_native_binding_nodes", ()),
            repository_id=self.identity.repository_id,
            pull_request=self.identity.pull_request,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            producer_id=producer_id,
            review_id=review["id"],
            app_slug=self.app_slug,
            expected=expected,
            expected_anchors=anchors,
        )
        if set(receipts) != set(expected):
            return False
        key = (self.identity.repository_id, self.identity.pull_request)
        retained = self.reviewer._native_finding_receipt_records.setdefault(key, {})
        retained.update(
            {instance: receipt.to_dict() for instance, receipt in receipts.items()}
        )
        context = getattr(self.reviewer, "_native_finding_context", None)
        if isinstance(context, dict):
            context["receipts"] = (review["id"], receipts)
        state = self.reviewer._drift_states.get(
            key
        ) or self.reviewer._active_window_states.get(key)
        if state is None:
            return True  # The ordinary producer will commit the authenticated receipts.
        if isinstance(state, ActiveWindowRoundState):
            for ob in state.authoritative_decision_inventory.obligations:
                if (
                    ob.target_state != "uncertain"
                    or ob.native_target is None
                    or ob.comment is None
                ):
                    continue
                comment = replace(
                    ob.comment,
                    path=ob.path or ob.comment.path,
                    line=ob.line if ob.line is not None else ob.comment.line,
                    side=ob.side,
                    symbol=ob.symbol or ob.comment.symbol,
                    needs_human=ob.needs_human,
                    effective_blocking=ob.blocking
                    or ob.lifecycle_state
                    in {"omitted-uncertain", "ambiguous-identity"},
                )
                receipt = receipts.get(finding_instance_fingerprint(comment))
                if receipt is not None and (
                    ob.native_target.startswith("intent:")
                    or ob.native_target == receipt.target
                ):
                    state = reconcile_uncertain_slot(
                        state,
                        target=ob.native_target,
                        resolved_state="active",
                        proof_sha256=receipt.proof_sha256,
                        confirmed_target=receipt.target,
                    )
            self.reviewer._active_window_states[key] = state
        else:
            created = {}
            admitted_creates = {
                entry.obligation_id
                for entry in state.active_entries
                if entry.thread_action == "create"
            }
            for ob in state.inventory.obligations:
                binding = ob.thread_binding
                if binding is None:
                    # A process exit can precede the uncertain-state checkpoint.
                    # Only a retained admitted create plus exact native receipt
                    # may recover that missing binding, never a hidden obligation.
                    if ob.obligation_id not in admitted_creates:
                        continue
                elif binding.slot_state != "uncertain":
                    continue
                receipt = receipts.get(
                    finding_instance_fingerprint(ob.synthetic_comment_for_placement())
                )
                if (
                    receipt is not None
                    and receipt.thread_id is not None
                    and receipt.comment_id is not None
                    and (
                        binding is None
                        or binding.thread_id.startswith("intent:")
                        or binding.thread_id == receipt.thread_id
                    )
                ):
                    created[ob.obligation_id] = (receipt.thread_id, receipt.comment_id)
            if created:
                state = commit_drift_publication(state, created_threads=created)
                self.reviewer._drift_states[key] = state
        if self._planned is not None:
            self._planned = state
        return True

    def _readback_thread(self, effect, *, token):
        if effect["status"] == "confirmed" and not _confirmed_thread_response_matches(
            effect
        ):
            raise ReviewInputError(
                "finding lifecycle retained mutation receipt conflicts"
            )
        request = effect["request"]
        query = str(request.get("query", ""))
        name = (
            "unresolveReviewThread"
            if "unresolveReviewThread" in query
            else "resolveReviewThread"
        )
        variables = request.get("variables")
        if not isinstance(variables, dict):
            return None
        thread_id = variables.get("threadId") or (variables.get("input") or {}).get(
            "threadId"
        )
        records = [
            r
            for r in self.reviewer.native_finding_receipts(
                repository_id=self.identity.repository_id,
                pull_request=self.identity.pull_request,
            )
            if r["thread_id"] == thread_id
        ]
        if not records:
            return None
        self.reviewer._published_finding_suppression(
            token=token,
            repository=self.identity.repository,
            pull_request=self.identity.pull_request,
            app_slug=self.app_slug,
            repository_id=self.identity.repository_id,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
        )
        nodes = [
            n
            for n in getattr(self.reviewer, "_native_binding_nodes", ())
            if n.get("id") == thread_id
        ]
        if len(nodes) != 1 or nodes[0].get("isResolved") is not (
            name == "resolveReviewThread"
        ):
            return None
        comments = nodes[0].get("comments", {}).get("nodes")
        if not isinstance(comments, list) or len(comments) != 1:
            return None
        comment = comments[0]
        if not isinstance(comment.get("body"), str):
            return None
        digest = hashlib.sha256(comment["body"].encode()).hexdigest()
        matching = [
            r
            for r in records
            if r["comment_id"] == comment.get("databaseId")
            and r["body_sha256"] == digest
        ]
        if len(matching) != 1:
            return None
        receipt = matching[0]
        author = comment.get("author")
        if (
            comment.get("databaseId") != receipt["comment_id"]
            or not isinstance(comment.get("body"), str)
            or hashlib.sha256(comment["body"].encode()).hexdigest()
            != receipt["body_sha256"]
            or not isinstance(author, dict)
            or author.get("databaseId") != receipt["producer_id"]
            or author.get("__typename") != "Bot"
            or author.get("login") != self.app_slug
        ):
            return None
        return 200, {
            "data": {
                name: {
                    "thread": {"id": thread_id, "isResolved": nodes[0]["isResolved"]}
                }
            }
        }

    @contextmanager
    def transports(self, *, finalizer=None):
        self._readback_http = self.reviewer.http

        owners = (
            self.reviewer,
            finalizer or self.reviewer.finalizer,
            self.reviewer.checks,
        )
        previous = [(owner, owner.http) for owner in owners]
        finalizer_owner = finalizer or self.reviewer.finalizer
        previous_journal = getattr(finalizer_owner, "_finding_lifecycle_journal", None)
        try:
            finalizer_owner._finding_lifecycle_journal = self
            for owner, http in previous:
                owner.http = _JournalTransport(self, http)
            yield
        finally:
            finalizer_owner._finding_lifecycle_journal = previous_journal
            for owner, http in previous:
                owner.http = http


from .empty_original_admission import install_restore as _install_empty_restore  # noqa: E402

restore_finding_lifecycle = _install_empty_restore(restore_finding_lifecycle)
del _install_empty_restore


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
