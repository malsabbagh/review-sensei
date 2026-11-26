"""Ordinary finding lifecycle checkpoints under selected session authority.

The transport journal records intent before dispatch. It never retries an unknown
write: only exact, owned native readback can discharge that intent. Tokens are
neither part of the document nor included in request identities.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import sys
import zlib
from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache
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

from .errors import GitHubHTTPError, GitHubPublicationError
from .thread_resolution import graphql_app_author

VERSION = "finding-lifecycle-journal-v1"
CURRENT_VERSION = "finding-lifecycle-journal-v2"
MAX_EFFECTS = 32
MAX_RECEIPT_BYTES = 512 * 1024


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


_ATOM_FIELDS = {"native_target", "target", "thread_id", "node_id"}
_NUMERIC_FIELDS = {"id", "review_id", "comment_id", "producer_id", "app_id"}
_CHECK_RUN_PATH_RE = re.compile(r"/repos/[^/]+/[^/]+/check-runs/[0-9]+")
_HEX64_RE = re.compile(r"[a-f0-9]{64}")


def record_has_pending_lifecycle_effects(ledger: Any, record: Any) -> bool:
    """Return True if ``record`` has any pending or unreadable lifecycle effects."""
    if record is None or getattr(record, "finding_lifecycle", None) is None:
        return False
    from review_sensei.finding_lifecycle_store import read_finding_lifecycle

    from .errors import (
        GitHubBrokerClientError,
        GitHubHTTPError,
        GitHubPublicationError,
    )
    from .finding_lifecycle_dictionary import resolve_stored

    try:
        stored = read_finding_lifecycle(ledger, record)
        if stored is None:
            return True
        resolved = resolve_stored(ledger, record, stored)
        journal_doc = _validate(_unpack_journal(resolved))
    except (
        GitHubHTTPError,
        GitHubPublicationError,
        GitHubBrokerClientError,
        ReviewInputError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return True
    return bool(journal_doc.get("unknown_trust_effect")) or any(
        isinstance(effect, dict) and effect.get("status") == "pending"
        for effect in journal_doc.get("effects", ())
    )


def _append_unique_state(target: list[Any], candidate: Any) -> None:
    for existing in target:
        if candidate is existing or (
            type(candidate) is type(existing) and candidate == existing
        ):
            return
    target.append(candidate)


def _pack_journal_with_skeleton(document):
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
    paired = document.get("schema_version") == "finding-lifecycle-journal-v3" or (
        document.get("schema_version") == "finding-lifecycle-journal-v4"
        and document.get("base_schema_version") == "finding-lifecycle-journal-v3"
    )

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
                    and _CHECK_RUN_PATH_RE.fullmatch(value) is not None
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
            and len(value) == 64
            and _HEX64_RE.fullmatch(value) is not None
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
    return (
        {
            "schema_version": "finding-lifecycle-packed-v3",
            "journal": encode_evidence(skeleton, max_decoded_bytes=1_048_576),
            "native_atoms": atoms,
        },
        skeleton,
    )


def _pack_journal(document):
    """Separate unknown native scalars from the deterministically packed state.

    Independent scalar allocation makes the future wire bound insensitive to
    how well an as-yet-unobserved GitHub ID happens to compress. These are
    representation indexes, never native identities or authority.
    """
    return _pack_journal_with_skeleton(document)[0]


def _pack_current_journal(document, *, _legacy=None):
    """Select a self-contained representation without changing legacy v3 bytes.

    This is serialization only. Unknown future values must still be sized with
    the generic v3 bound unless their exact retained prefix has separate custody.
    """
    legacy = _pack_journal(document) if _legacy is None else _legacy
    if type(document) is not dict:
        return legacy
    schema = document.get("schema_version")
    if (
        schema in {VERSION, CURRENT_VERSION}
        and type(document.get("guarded_publication_pair")) is not dict
    ):
        lifecycle = document.get("lifecycle")
        effects = document.get("effects")
        if (
            isinstance(lifecycle, dict)
            and lifecycle.get("mode") == "drift"
            and isinstance(effects, list)
        ):
            obligations = (
                lifecycle.get("state", {}).get("inventory", {}).get("obligations", ())
            )
            if isinstance(obligations, list) and len(obligations) >= 24:
                resolved_count = sum(
                    1
                    for ob in obligations
                    if isinstance(ob, dict)
                    and ob.get("resolution_proof_sha256") is not None
                )
                has_pending = any(
                    isinstance(e, dict) and e.get("status") == "pending"
                    for e in effects
                )
                if resolved_count == 0 or (resolved_count == 1 and not has_pending):
                    return legacy
                try:
                    compact_v6 = _pack_compact_journal(document)
                except ReviewInputError:
                    return legacy
                return (
                    compact_v6
                    if len(canonical_bytes(compact_v6)) < len(canonical_bytes(legacy))
                    else legacy
                )
        return legacy
    if schema != "finding-lifecycle-journal-v3":
        return legacy
    effects = document.get("effects")
    if (
        type(effects) is not list
        or not 4 <= len(effects) <= MAX_EFFECTS
        or any(
            type(e) is not dict or e.get("status") != "confirmed" for e in effects[:-1]
        )
        or type(document.get("guarded_publication_pair")) is not dict
    ):
        return legacy
    from review_sensei.source_registry import require_module_seal

    from . import finding_lifecycle_prefix

    require_module_seal(finding_lifecycle_prefix)
    finding_lifecycle_prefix._sources()
    try:
        plain = finding_lifecycle_prefix._plain(document)
    except ReviewInputError:
        # The optional format's plain finite graph is narrower than historical
        # JSON. Keep the original representation for unsupported data, not a
        # weakened prefix. Source authentication happened outside this fallback.
        return legacy
    compact = finding_lifecycle_prefix.pack(plain, prefix_count=len(effects) - 1)
    return (
        compact
        if len(canonical_bytes(compact)) < len(canonical_bytes(legacy))
        else legacy
    )


def _envelope_for_manifest(document, manifest):
    """Match one committed full representation; never replace its hash.

    Both candidates are finite, self-contained encodings of the same complete
    logical journal. A match is evidence bytes only, not selected-root authority.
    """
    if type(manifest) is not dict or type(manifest.get("sha256")) is not str:
        return None
    for encode in (_pack_journal, _pack_current_journal):
        packed = encode(document)
        envelope = {
            "schema_version": "finding-lifecycle-evidence-v2",
            "lifecycle": packed,
        }
        if _digest(envelope) == manifest["sha256"]:
            return envelope
    return None


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


_COMPACT_VERSION = "finding-lifecycle-packed-v6"
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_OVERRIDE_ACTORS: list[str] = [
    "alice",
    "bob",
    "maintainer",
    "operator",
    "reviewer",
    "authenticated-resolution",
]


def register_compact_actor(actor: object) -> None:
    if isinstance(actor, str):
        cleaned = actor.strip()
        if (
            0 < len(cleaned) <= 128
            and cleaned not in _KNOWN_OVERRIDE_ACTORS
            and len(_KNOWN_OVERRIDE_ACTORS) < 512
        ):
            _KNOWN_OVERRIDE_ACTORS.append(cleaned)


_COMPACT_STATE_LIST = (
    "active-unresolved",
    "new",
    "still-present",
    "authorized-override",
    "verified-fixed",
    "reopened",
)
_COMPACT_CRIT_FIELDS = (
    "origin_head_sha",
    "origin_path",
    "origin_line",
    "origin_side",
    "origin_symbol",
    "defect_kind",
    "blocking",
    "needs_human",
    "impact",
    "evidence",
)
_COMPACT_ANCHOR_FIELDS = (
    "head_sha",
    "path",
    "line",
    "side",
    "symbol",
    "anchor_location",
    "drift_kind",
    "candidate_paths",
)
_COMPACT_BINDING_FIELDS = (
    "thread_id",
    "comment_id",
    "placement",
    "body_reason",
    "slot_state",
    "proof_sha256",
    "host_marked_outdated",
    "host_marked_resolved",
)
_COMPACT_COMMENT_FIELDS = (
    "path",
    "line",
    "body",
    "blocking",
    "severity",
    "category",
    "fix_effort",
    "symbol",
    "defect_kind",
    "evidence_id",
    "side",
    "effective_blocking",
    "needs_human",
)
_COMPACT_RESERVED = {"$h", "$rb", "$ib", "$icl", "$sh", "$tid"}
_COMPACT_ZDICT = (
    b'{"ac":["alice"],"ech":{},"effects":['
    b'{"method":"POST","path":"$rp/check-runs","request":{"external_id":"reviewsensei-review-v1","head_sha":{"$sh":0},"name":"ReviewSensei","output":{"summary":"A new ReviewSensei review is running for this head; any earlier conclusion on this head does not stand for it.","title":"ReviewSensei review in progress"},"status":"in_progress"},"response":[201,{"app_id":999,"id":502,"mutation":null,"producer_id":null}],"status":"confirmed"},'
    b'{"check_update_precondition":{"app_id":999,"app_slug":"reviewsensei","check_run_id":502,"head_sha":{"$sh":0}},"method":"PATCH","path":"$rp/check-runs/502","request":{"conclusion":"action_required","output":{"summary":"The completed review reported findings that require a human decision before approval.","title":"Human decision required"},"status":"completed"},"response":[200,{"app_id":999,"id":502,"mutation":null,"producer_id":null}],"status":"confirmed"},'
    b'{"conclusion":"success","output":{"summary":"The completed review reported no required fixes for this head.","title":"No required fixes"},"status":"completed"},'
    b'{"method":"POST","path":"$rp/pulls/7/reviews","request":{"body":{"$body_sha256":{"$h":0},"utf8_bytes":2569},"comments":{"$icl":"new"},"commit_id":{"$sh":0},"event":"COMMENT"},"response":[200,{"app_id":null,"id":102,"mutation":null,"producer_id":999}],"status":"confirmed"},'
    b'{"method":"POST","path":"$rp/pulls/7/reviews","request":{"body":{"$body_sha256":{"$h":1},"utf8_bytes":21488},"comments":[],"commit_id":{"$sh":0},"event":"APPROVE"},"response":[200,{"app_id":null,"id":103,"mutation":null,"producer_id":999}],"status":"confirmed"},'
    b'{"method":"PUT","path":"$rp/pulls/7/reviews/103","request":{"body":{"$body_sha256":{"$h":2},"utf8_bytes":21668}},"review_update_precondition":{"body_sha256":{"$h":1},"head_sha":{"$sh":0},"producer_id":999,"review_id":103},"response":[200,{"app_id":null,"id":103,"mutation":null,"producer_id":999}],"status":"confirmed"},'
    b'{"method":"POST","path":"/graphql","request":{"operationName":"ResolveReviewThread","query":"mutation ResolveReviewThread($input: ResolveReviewThreadInput!) {\\n resolveReviewThread(input: $input) { thread { id isResolved } } }","variables":{"input":{"threadId":{"$tid":1}}}},"response":[200,{"app_id":null,"id":null,"mutation":{"name":"resolveReviewThread","resolved":true,"thread_id":{"$tid":1}},"producer_id":null}],"status":"confirmed"},'
    b'"status":"pending","response":null,'
    b'"ii":[{"$sh":0},{"$sh":1},{"$h":3}],"p":"complete-finding-lifecycle","receipts":"$id","rp":"/repos/owner/repo/","rt":[123,7,0,1,999,102,["$inl","new"],"$tb","$tb",0],'
    b'"st":{"active":["$def","update"],"ad":[0,"src/app.py",3,"RIGHT",null,"right","unchanged",["src/app.py"]],"body_placement_note":null,"cd":[0,"src/app.py",3,"RIGHT",null,"unknown",false,true,"high","supported-source"],'
    b'"changed_file_coverage":{"acceptance_proof_sha256":null,"approval_state":"reviewed","coverage_mode":"full","coverage_uncertainty_accepted":false,"enumerated_paths":["src/app.py"],"enumeration_complete":true,"fully_reviewed":true,"reviewed_paths":["src/app.py"]},'
    b'"max_active_findings":250,"obs":{"a":0,"c":0,"cids":[209,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"co":null,"g":0,"n":28,"rc":1,"rc_b":[],"rc_e":[],"rk":50,"rp":[["$co",0,1],null],"st":[3,0],"tb":[4,2],"tids":"$prrt"},'
    b'"rd":["src/app.py",3,"",false,"high",null,null,null,null,"","RIGHT",false,true],"review_complete":true,"round_number":1,"so":null},"ute":false,"v":"finding-lifecycle-payload-v1"}'
)


def _compact_unresolved_order(obs_list: list[dict[str, Any]]) -> list[int]:
    return sorted(
        (i for i, ob in enumerate(obs_list) if ob["resolution_proof_sha256"] is None),
        key=lambda i: (
            obs_list[i]["proposal_rank"]
            if type(obs_list[i]["proposal_rank"]) is int
            else 10**9,
            obs_list[i]["present_anchor"]["path"] or "",
            obs_list[i]["present_anchor"]["line"]
            if type(obs_list[i]["present_anchor"]["line"]) is int
            else 0,
            obs_list[i]["criterion"]["obligation_id"],
        ),
    )


@lru_cache(maxsize=512)
def _compact_derived_inline_body(
    comment: Any,
    *,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    base_sha: str,
    state: str,
) -> tuple[str, int]:
    from review_sensei.context import finding_lifecycle_for_comment
    from review_sensei.human_assessment import finding_instance_fingerprint
    from review_sensei.models import ReviewResult
    from review_sensei.presentation import build_finding_view

    from .publication import render_native_inline

    fl = finding_lifecycle_for_comment(comment)
    inst = finding_instance_fingerprint(comment)
    ident = f"RS-{inst[:6].upper()}"
    view = replace(
        build_finding_view(
            comment,
            fingerprint=fl.fingerprint,
            identifier=ident,
            lifecycle_state=state,
            placement="inline",
        ),
        repeat=True,
    )
    dummy = ReviewResult(provider="p", model="m", summary="s", comments=())
    body = render_native_inline(
        comment,
        view=view,
        result=dummy,
        repository_id=repository_id,
        pull_request=pull_request,
        head_sha=head_sha,
        base_sha=base_sha,
        fingerprint=fl.fingerprint,
        state=state,
    )
    raw = body.encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


@lru_cache(maxsize=512)
def _compact_criterion_hashes(
    comment: Any,
    *,
    origin_head_sha: str,
    defect_kind: str,
    path: str | None,
    symbol: str | None,
) -> tuple[str, str, str | None, str]:
    from review_sensei.baseline import resolution_criterion_digest
    from review_sensei.context import finding_lifecycle_for_comment
    from review_sensei.human_assessment import finding_instance_fingerprint

    fl = finding_lifecycle_for_comment(comment)
    inst = finding_instance_fingerprint(comment)
    oid = hashlib.sha256(
        b"reviewsensei:drift-obligation:v1:"
        + canonical_bytes(
            {
                "fingerprint": fl.fingerprint,
                "instance": inst,
                "origin_head_sha": origin_head_sha,
            }
        )
    ).hexdigest()
    rc_dig = resolution_criterion_digest(
        fingerprint=fl.fingerprint,
        evidence_id=comment.evidence_id,
        defect_kind=defect_kind,
        path=path,
        symbol=symbol,
    )
    return oid, fl.fingerprint, fl.concern, rc_dig


def _pack_compact_journal(document: object) -> dict[str, Any]:
    """Lossless compact representation for large v2 drift journals."""
    from review_sensei.human_assessment import finding_instance_fingerprint
    from review_sensei.models import ReviewComment

    from .publication import NativeFindingReceipt

    if not isinstance(document, dict) or document.get("schema_version") not in {
        VERSION,
        CURRENT_VERSION,
    }:
        raise ReviewInputError(
            "finding lifecycle compact carrier schema is unsupported"
        )
    lc = document.get("lifecycle")
    if not isinstance(lc, dict) or lc.get("mode") != "drift":
        raise ReviewInputError("finding lifecycle compact carrier mode is unsupported")
    st = lc.get("state")
    if not isinstance(st, dict):
        raise ReviewInputError("finding lifecycle compact carrier state is invalid")
    h_sha = st.get("head_sha")
    b_sha = st.get("base_sha")
    if (
        not isinstance(h_sha, str)
        or not isinstance(b_sha, str)
        or _HEX40_RE.fullmatch(h_sha) is None
        or _HEX40_RE.fullmatch(b_sha) is None
    ):
        raise ReviewInputError("finding lifecycle compact carrier snapshot is invalid")

    def ref_sha(value):
        if value == h_sha:
            return 0
        if value == b_sha:
            return 1
        return value

    digests = bytearray()
    digest_idx: dict[str, int] = {}

    def intern_hex(hex_str: str) -> int:
        idx = digest_idx.get(hex_str)
        if idx is None:
            idx = len(digest_idx)
            digest_idx[hex_str] = idx
            digests.extend(bytes.fromhex(hex_str))
        return idx

    obs = st["inventory"]["obligations"]
    inst_to_ob_idx: dict[str, int] = {}
    ob_comments: list[Any] = []
    for ob_i, ob in enumerate(obs):
        c_dict = ob["latest_comment"]
        comment = None if c_dict is None else ReviewComment(**c_dict)
        ob_comments.append(comment)
        if comment is not None:
            inst_to_ob_idx[finding_instance_fingerprint(comment)] = ob_i

    receipts = document["native_receipts"]
    body_sha_by_thread: dict[str, str] = {}
    receipt_by_thread: dict[str, dict[str, Any]] = {}
    inline_body_by_digest: dict[str, tuple[int, str, int]] = {}
    inline_cache: dict[tuple[int, int, int, str, str, str], tuple[str, int]] = {}

    def derived_info(
        ob_i: int, repo_id: int, pr: int, rh: str, rb: str, st_cand: str
    ) -> tuple[str, int]:
        cache_key = (ob_i, repo_id, pr, rh, rb, st_cand)
        hit = inline_cache.get(cache_key)
        if hit is None:
            hit = _compact_derived_inline_body(
                ob_comments[ob_i],
                repository_id=repo_id,
                pull_request=pr,
                head_sha=rh,
                base_sha=rb,
                state=st_cand,
            )
            inline_cache[cache_key] = hit
        return hit

    rt: list[Any] | None = None
    encoded_receipts: list[Any] = []
    for receipt in receipts:
        if receipt["thread_id"] is not None:
            body_sha_by_thread[receipt["thread_id"]] = receipt["body_sha256"]
            receipt_by_thread[receipt["thread_id"]] = receipt
        matched_ob_i = inst_to_ob_idx.get(receipt["instance"])
        body_ref: Any = None
        if matched_ob_i is not None:
            for st_cand in ("new", "still-present"):
                dig, utf8_len = derived_info(
                    matched_ob_i,
                    receipt["repository_id"],
                    receipt["pull_request"],
                    receipt["head_sha"],
                    receipt["base_sha"],
                    st_cand,
                )
                inline_body_by_digest.setdefault(dig, (matched_ob_i, st_cand, utf8_len))
                if dig == receipt["body_sha256"]:
                    body_ref = ["$inl", st_cand]
                    break
        if body_ref is None:
            body_ref = ["$h", intern_hex(receipt["body_sha256"])]
        inst_ref: Any = (
            ["$ob", matched_ob_i]
            if matched_ob_i is not None
            else ["$h", intern_hex(receipt["instance"])]
        )
        # Ensure proof property matches NativeFindingReceipt validation cleanly.
        NativeFindingReceipt(
            receipt["repository_id"],
            receipt["pull_request"],
            receipt["head_sha"],
            receipt["base_sha"],
            receipt["producer_id"],
            receipt["review_id"],
            receipt["instance"],
            receipt["body_sha256"],
            receipt["thread_id"],
            receipt["comment_id"],
            receipt["native_head_sha"],
        )
        if receipt["thread_id"] == "$tb" or receipt["comment_id"] == "$tb":
            raise ReviewInputError("finding lifecycle compact reserved receipt binding")
        tb_match = (
            obs[matched_ob_i]["thread_binding"] if matched_ob_i is not None else None
        )
        tid_out = (
            "$tb"
            if tb_match is not None
            and tb_match["thread_id"] is not None
            and receipt["thread_id"] == tb_match["thread_id"]
            else receipt["thread_id"]
        )
        cid_out = (
            "$tb"
            if tb_match is not None
            and tb_match["comment_id"] is not None
            and receipt["comment_id"] == tb_match["comment_id"]
            else receipt["comment_id"]
        )
        row = [
            receipt["repository_id"],
            receipt["pull_request"],
            ref_sha(receipt["head_sha"]),
            ref_sha(receipt["base_sha"]),
            receipt["producer_id"],
            receipt["review_id"],
            inst_ref,
            body_ref,
            tid_out,
            cid_out,
            ref_sha(receipt["native_head_sha"]),
        ]
        if matched_ob_i is not None:
            cand_rt = [*row[:6], *row[7:]]
            if rt is None:
                rt = cand_rt
            if cand_rt == rt:
                encoded_receipts.append(matched_ob_i)
                continue
        encoded_receipts.append(row)

    receipts_out: Any = (
        "$id"
        if rt is not None and len(obs) > 0 and encoded_receipts == list(range(len(obs)))
        else encoded_receipts
    )

    body_sha_to_ridx = {r["body_sha256"]: idx for idx, r in enumerate(receipts)}
    ch = lc["concern_history"]
    extra_ch: dict[str, Any] = {}
    tid_to_ob_idx: dict[str, int] = {}
    expected_slots = []
    oid_to_ob_idx: dict[str, int] = {}
    crit_def: list[Any] | None = None
    anc_def: list[Any] | None = None
    rc_def: list[Any] | None = None
    state_idx_map = {name: i for i, name in enumerate(_COMPACT_STATE_LIST)}
    used_actors: list[str] = []
    used_actor_idx: dict[str, int] = {}

    c_col: list[Any] = []
    a_col: list[Any] = []
    st_col: list[Any] = []
    g_col: list[Any] = []
    rk_col: list[Any] = []
    tb_col: list[Any] = []
    tids: list[str] = []
    cids: list[int] = []
    raw_cids: list[int] = []
    prev_cid = 0
    rp_col: list[Any] = []
    rc_col: list[Any] = []
    rc_b: list[str] = []
    rc_e: list[str | None] = []
    co_col: list[Any] = []

    for idx, ob in enumerate(obs):
        crit = ob["criterion"]
        oid = crit["obligation_id"]
        oid_to_ob_idx[oid] = idx
        comment = ob_comments[idx]
        crit_override: list[int | None] | None = None
        if comment is not None:
            derived = _compact_criterion_hashes(
                comment,
                origin_head_sha=crit["origin_head_sha"],
                defect_kind=crit["defect_kind"],
                path=crit["origin_path"],
                symbol=crit["origin_symbol"],
            )
            actual = (
                crit["obligation_id"],
                crit["original_fingerprint"],
                crit["concern"],
                crit["resolution_criterion"],
            )
            if derived != actual:
                crit_override = [
                    intern_hex(crit["obligation_id"]),
                    intern_hex(crit["original_fingerprint"]),
                    None if crit["concern"] is None else intern_hex(crit["concern"]),
                    intern_hex(crit["resolution_criterion"]),
                ]
        else:
            crit_override = [
                intern_hex(crit["obligation_id"]),
                intern_hex(crit["original_fingerprint"]),
                None if crit["concern"] is None else intern_hex(crit["concern"]),
                intern_hex(crit["resolution_criterion"]),
            ]
        if ch.get(oid) != [crit["original_fingerprint"]]:
            extra_ch[oid] = None if oid not in ch else [intern_hex(h) for h in ch[oid]]
        crit_row = [
            ref_sha(crit[k]) if k == "origin_head_sha" else crit[k]
            for k in _COMPACT_CRIT_FIELDS
        ]
        if crit_def is None:
            crit_def = list(crit_row)
        c_col.append(0 if crit_row == crit_def else crit_row)
        anc = ob["present_anchor"]
        anc_row = [
            ref_sha(anc[k]) if k == "head_sha" else anc[k]
            for k in _COMPACT_ANCHOR_FIELDS
        ]
        if anc_def is None:
            anc_def = list(anc_row)
        a_col.append(0 if anc_row == anc_def else anc_row)
        l_state = ob["lifecycle_state"]
        st_col.append(state_idx_map[l_state] if l_state in state_idx_map else l_state)
        g_col.append(ob["generation"])
        rk_col.append(ob["proposal_rank"])
        tb = ob["thread_binding"]
        if tb is not None:
            tid, cid = tb["thread_id"], tb["comment_id"]
            if tid == "$tb":
                raise ReviewInputError(
                    "finding lifecycle compact reserved thread binding"
                )
            if isinstance(tid, str):
                tid_to_ob_idx[tid] = idx
            expected_slots.append(
                {
                    "proof_sha256": tb["proof_sha256"],
                    "state": tb["slot_state"],
                    "target": tid,
                }
            )
            p = tb["proof_sha256"]
            p_code: Any = None
            if (
                ob["resolution_proof_sha256"] is not None
                and p == ob["resolution_proof_sha256"]
            ):
                p_code = "$res"
            elif (
                tid in body_sha_by_thread
                and p
                == hashlib.sha256(
                    canonical_bytes(
                        {
                            "thread_id": tid,
                            "comment_id": cid,
                            "head_sha": h_sha,
                            "obligation_id": oid,
                            "placement": tb["placement"],
                            "body_sha256": body_sha_by_thread[tid],
                        }
                    )
                ).hexdigest()
            ):
                p_code = "$d1"
            elif (
                p
                == hashlib.sha256(
                    canonical_bytes(
                        {
                            "thread_id": tid,
                            "comment_id": cid,
                            "head_sha": h_sha,
                            "obligation_id": oid,
                        }
                    )
                ).hexdigest()
            ):
                p_code = "$d2"
            elif (
                ob["resolution_proof_sha256"] is not None
                and tid in receipt_by_thread
                and p
                == hashlib.sha256(
                    canonical_bytes(
                        {
                            "kind": "confirmed-finding-slot-release-v1",
                            "resolution": ob["resolution_proof_sha256"],
                            "receipt": receipt_by_thread[tid],
                            "head_sha": h_sha,
                        }
                    )
                ).hexdigest()
            ):
                p_code = "$rel"
            tb_mapped = {
                **tb,
                "proof_sha256": p_code if p_code is not None else ["$h", intern_hex(p)],
            }
            tb_row: list[Any] = [tb_mapped[k] for k in _COMPACT_BINDING_FIELDS]
            tag_num: int | None = None
            if isinstance(tid, str) and type(cid) is int:
                if tb_row == [tid, cid, "inline", None, "active", "$d1", False, False]:
                    tag_num = 1
                elif tb_row == [
                    tid,
                    cid,
                    "inline",
                    None,
                    "active",
                    "$d2",
                    False,
                    False,
                ]:
                    tag_num = 2
                elif tb_row == [
                    tid,
                    cid,
                    "inline",
                    None,
                    "released",
                    "$res",
                    False,
                    False,
                ]:
                    tag_num = 3
                elif tb_row == [
                    tid,
                    cid,
                    "inline",
                    None,
                    "released",
                    "$rel",
                    False,
                    True,
                ]:
                    tag_num = 4
            if tag_num is not None:
                tb_col.append(tag_num)
                tids.append(tid)
                cids.append(cid - prev_cid)
                raw_cids.append(cid)
                prev_cid = cid
            else:
                tb_col.append(tb_row)
        else:
            tb_col.append(None)

        res_hex = ob["resolution_proof_sha256"]
        res_p: Any = None
        if res_hex is not None:
            matched_co: tuple[int, int] | None = None
            for cand_actor in (
                *used_actors,
                *(a for a in _KNOWN_OVERRIDE_ACTORS if a not in used_actor_idx),
            ):
                for sr in (True, False):
                    cand_proof = hashlib.sha256(
                        b"reviewsensei:drift-command-override:v1:"
                        + canonical_bytes(
                            {
                                "obligation_id": oid,
                                "actor": cand_actor,
                                "head_sha": h_sha,
                                "slot_released": sr,
                            }
                        )
                    ).hexdigest()
                    if cand_proof == res_hex:
                        a_idx = used_actor_idx.get(cand_actor)
                        if a_idx is None:
                            a_idx = len(used_actors)
                            used_actor_idx[cand_actor] = a_idx
                            used_actors.append(cand_actor)
                        matched_co = (a_idx, 1 if sr else 0)
                        break
                if matched_co is not None:
                    break
            res_p = (
                ["$co", matched_co[0], matched_co[1]]
                if matched_co is not None
                else intern_hex(res_hex)
            )
        rp_col.append(res_p)

        c_dict = ob["latest_comment"]
        rc_row = (
            None if c_dict is None else [c_dict[k] for k in _COMPACT_COMMENT_FIELDS]
        )
        if rc_row is None:
            rc_col.append(None)
        else:
            rc_template = [*rc_row[:2], "", *rc_row[3:9], "", *rc_row[10:]]
            if rc_def is None:
                rc_def = rc_template
            if rc_template == rc_def:
                rc_col.append(1)
                rc_b.append(rc_row[2])
                rc_e.append(rc_row[9])
            else:
                rc_col.append(rc_row)
        co_col.append(crit_override)

    slots_order: list[int] | None = None
    if st["slots"] != expected_slots:
        slots_order = [tid_to_ob_idx[s["target"]] for s in st["slots"]]

    compact_active = [
        [
            oid_to_ob_idx[ae["obligation_id"]],
            ae["placement"],
            ae["body_reason"],
            ae["thread_action"],
        ]
        for ae in st["active_entries"]
    ]
    unres_order = _compact_unresolved_order(obs)
    if not unres_order and compact_active == []:
        active_out: Any = "$def"
    elif (
        unres_order
        and len(compact_active) == len(unres_order)
        and isinstance(compact_active[0][3], str)
        and compact_active
        == [[i, "inline", None, compact_active[0][3]] for i in unres_order]
    ):
        active_out = ["$def", compact_active[0][3]]
    else:
        active_out = compact_active

    n_obs = len(obs)
    obs_col: dict[str, Any] = {
        "n": n_obs,
        "c": 0 if n_obs > 0 and all(x == 0 for x in c_col) else c_col,
        "a": 0 if n_obs > 0 and all(x == 0 for x in a_col) else a_col,
        "st": (
            st_col[0] if n_obs > 0 and all(x == st_col[0] for x in st_col) else st_col
        ),
        "g": (
            g_col[0]
            if n_obs > 0 and type(g_col[0]) is int and all(x == g_col[0] for x in g_col)
            else g_col
        ),
        "rk": (
            "$id"
            if n_obs > 0 and rk_col == list(range(n_obs))
            else None
            if n_obs > 0 and all(x is None for x in rk_col)
            else rk_col[0]
            if n_obs > 0
            and type(rk_col[0]) is int
            and all(x == rk_col[0] for x in rk_col)
            else rk_col
        ),
        "tb": (
            None
            if n_obs > 0 and all(x is None for x in tb_col)
            else tb_col[0]
            if n_obs > 0
            and type(tb_col[0]) is int
            and 1 <= tb_col[0] <= 4
            and all(x == tb_col[0] for x in tb_col)
            else tb_col
        ),
        "tids": (
            "$prrt"
            if len(tids) > 0 and tids == [f"PRRT_{c}" for c in raw_cids]
            else tids
        ),
        "cids": cids,
        "rp": (
            None
            if all(x is None for x in rp_col)
            else rp_col[0]
            if n_obs > 0
            and isinstance(rp_col[0], list)
            and len(rp_col[0]) == 3
            and rp_col[0][0] == "$co"
            and all(x == rp_col[0] for x in rp_col)
            else rp_col
        ),
        "rc": (
            None
            if n_obs > 0 and all(x is None for x in rc_col)
            else 1
            if n_obs > 0 and all(x == 1 for x in rc_col)
            else rc_col
        ),
        "rc_b": rc_b,
        "rc_e": rc_e,
        "co": None if all(x is None for x in co_col) else co_col,
    }

    st_compact = {
        k: v
        for k, v in st.items()
        if k
        not in {
            "inventory",
            "active_entries",
            "slots",
            "approved",
            "withheld_reasons",
            "recovery_steps",
            "base_sha",
            "head_sha",
        }
    }
    st_compact["obs"] = obs_col
    st_compact["active"] = active_out
    st_compact["so"] = slots_order
    st_compact["cd"] = crit_def
    st_compact["ad"] = anc_def
    st_compact["rd"] = rc_def

    def intern_tree(value: Any) -> Any:
        if isinstance(value, str):
            if value == h_sha:
                return {"$sh": 0}
            if value == b_sha:
                return {"$sh": 1}
            if _HEX64_RE.match(value):
                if value in body_sha_to_ridx:
                    return {"$rb": body_sha_to_ridx[value]}
                return {"$h": intern_hex(value)}
            if value in tid_to_ob_idx:
                return {"$tid": tid_to_ob_idx[value]}
        if isinstance(value, dict):
            if set(value) & _COMPACT_RESERVED:
                raise ReviewInputError("finding lifecycle compact reserved key")
            if set(value) == {"$body_sha256", "utf8_bytes"}:
                hit = inline_body_by_digest.get(value["$body_sha256"])
                if hit is not None and hit[2] == value["utf8_bytes"]:
                    return {"$ib": [hit[0], hit[1]]}
            return {key: intern_tree(item) for key, item in value.items()}
        if isinstance(value, list):
            mapped = [intern_tree(item) for item in value]
            if (
                receipts
                and len(mapped) == len(obs)
                and len(obs) > 0
                and all(c is not None for c in ob_comments)
                and isinstance(mapped[0], dict)
                and isinstance(mapped[0].get("body"), dict)
                and isinstance(mapped[0]["body"].get("$ib"), list)
                and len(mapped[0]["body"]["$ib"]) == 2
            ):
                st_cand = mapped[0]["body"]["$ib"][1]
                if st_cand in {"new", "still-present"} and mapped == [
                    {
                        "body": {"$ib": [i, st_cand]},
                        "line": ob_comments[i].line,
                        "path": ob_comments[i].path,
                        "side": ob_comments[i].side,
                    }
                    for i in range(len(obs))
                ]:
                    return {"$icl": st_cand}
            return mapped
        return value

    repo_prefix: str | None = None
    if not any(
        isinstance(e.get("path"), str) and e["path"].startswith("$rp/")
        for e in document["effects"]
    ):
        for e in document["effects"]:
            p_str = e.get("path")
            if isinstance(p_str, str):
                match = re.match(r"^(/repos/[^/]+/[^/]+/)", p_str)
                if match is not None:
                    repo_prefix = match.group(1)
                    break

    effects_compact = []
    for effect in document["effects"]:
        copied = dict(effect)
        if copied.get("request_sha256") == _digest(
            {
                "method": copied["method"],
                "path": copied["path"],
                "request": _request_identity(copied["request"]),
            }
        ):
            del copied["request_sha256"]
        if (
            repo_prefix is not None
            and isinstance(copied.get("path"), str)
            and copied["path"].startswith(repo_prefix)
        ):
            copied["path"] = "$rp/" + copied["path"][len(repo_prefix) :]
        effects_compact.append(intern_tree(copied))

    if len(digest_idx) > 4096:
        raise ReviewInputError("finding lifecycle compact digest capacity exceeded")

    payload = {
        "v": lc["schema_version"],
        "p": lc["purpose"],
        "ii": intern_tree(lc["input_identity"]),
        "ac": used_actors,
        "ech": extra_ch,
        "st": st_compact,
        "rt": rt,
        "receipts": receipts_out,
        "rp": repo_prefix,
        "effects": effects_compact,
        "ute": document["unknown_trust_effect"],
        **(
            {"ash": intern_tree(document["accepted_semantic_history"])}
            if document["schema_version"] == CURRENT_VERSION
            else {}
        ),
    }
    compressor = zlib.compressobj(level=9, wbits=-15, zdict=_COMPACT_ZDICT)
    compressed = compressor.compress(canonical_bytes(payload)) + compressor.flush()
    stream = (
        bytes.fromhex(b_sha)
        + bytes.fromhex(h_sha)
        + struct.pack(">H", len(digest_idx))
        + bytes(digests)
        + compressed
    )
    if len(stream) > 1_048_576:
        raise ReviewInputError("finding lifecycle compact stream exceeds bounds")
    packed = {
        "schema_version": _COMPACT_VERSION,
        "d": base64.b85encode(stream).decode("ascii"),
    }
    if canonical_bytes(_unpack_compact_payload(packed)) != canonical_bytes(document):
        raise ReviewInputError(
            "finding lifecycle compact round-trip verification failed"
        )
    return packed


def _unpack_compact_payload(stored: object) -> dict[str, Any]:
    from review_sensei.active_finding_window import ChangedFileCoverageState
    from review_sensei.finding_drift import (
        DriftActiveEntry,
        DriftInventory,
        DriftTrackedObligation,
        _recompute_drift_verdict,
    )
    from review_sensei.finding_lifecycle import _decode, _encode
    from review_sensei.human_assessment import finding_instance_fingerprint
    from review_sensei.models import ReviewComment
    from review_sensei.publication_selection import PublishedSlot

    if (
        type(stored) is not dict
        or set(stored) != {"schema_version", "d"}
        or stored.get("schema_version") != _COMPACT_VERSION
        or not isinstance(stored.get("d"), str)
        or len(stored["d"]) > 1_400_000
    ):
        raise ReviewInputError("finding lifecycle compact carrier is invalid")
    try:
        stream = base64.b85decode(stored["d"])
    except (ValueError, TypeError) as exc:
        raise ReviewInputError("finding lifecycle compact encoding is invalid") from exc
    if base64.b85encode(stream).decode("ascii") != stored["d"] or not (
        43 <= len(stream) <= 1_048_576
    ):
        raise ReviewInputError("finding lifecycle compact stream is invalid")
    b_sha = stream[:20].hex()
    h_sha = stream[20:40].hex()
    (digest_count,) = struct.unpack(">H", stream[40:42])
    if not 0 <= digest_count <= 4096 or 42 + 32 * digest_count >= len(stream):
        raise ReviewInputError("finding lifecycle compact digest header is invalid")
    digest_bytes = stream[42 : 42 + 32 * digest_count]
    compressed = stream[42 + 32 * digest_count :]
    try:
        decoder = zlib.decompressobj(wbits=-15, zdict=_COMPACT_ZDICT)
        raw = decoder.decompress(compressed, 1_048_576 + 1)
        if not decoder.eof:
            raise ReviewInputError("finding lifecycle compact stream exceeds bounds")
        tail = decoder.flush()
        if (
            len(raw) + len(tail) > 1_048_576
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise ReviewInputError("finding lifecycle compact stream exceeds bounds")
        raw = raw + tail
    except zlib.error as exc:
        raise ReviewInputError("finding lifecycle compact stream is invalid") from exc
    if len(raw) > 1_048_576:
        raise ReviewInputError("finding lifecycle compact stream exceeds bounds")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ReviewInputError("finding lifecycle compact JSON is invalid") from exc
    if not isinstance(payload, dict) or canonical_bytes(payload) != raw:
        raise ReviewInputError("finding lifecycle compact JSON is noncanonical")

    def hex_at(index: object) -> str:
        if type(index) is not int or not 0 <= index < digest_count:
            raise ReviewInputError("finding lifecycle compact digest index is invalid")
        return digest_bytes[index * 32 : (index + 1) * 32].hex()

    used_actors = payload.get("ac")
    if not isinstance(used_actors, list) or any(
        not isinstance(a, str) or not 0 < len(a.strip()) <= 128 or a != a.strip()
        for a in used_actors
    ):
        raise ReviewInputError("finding lifecycle compact actor table is invalid")
    for actor_name in used_actors:
        register_compact_actor(actor_name)

    st = payload.get("st")
    if not isinstance(st, dict):
        raise ReviewInputError("finding lifecycle compact state is invalid")

    def resolve_sha(value: object) -> object:
        if value == 0:
            return h_sha
        if value == 1:
            return b_sha
        return value

    obs_col = st.get("obs")
    if not isinstance(obs_col, dict):
        raise ReviewInputError("finding lifecycle compact obs is invalid")
    n_obs = obs_col.get("n")
    if type(n_obs) is not int or not 0 <= n_obs <= 4096:
        raise ReviewInputError("finding lifecycle compact obs count is invalid")

    def expand_col(val: object, *, allow_zero: bool = False) -> list[Any]:
        if isinstance(val, list):
            if len(val) != n_obs:
                raise ReviewInputError("finding lifecycle compact column width invalid")
            return val
        if n_obs > 0 and (allow_zero and val == 0):
            return [0] * n_obs
        raise ReviewInputError("finding lifecycle compact column type is invalid")

    c_col = expand_col(obs_col.get("c"), allow_zero=True)
    a_col = expand_col(obs_col.get("a"), allow_zero=True)

    st_raw = obs_col.get("st")
    if isinstance(st_raw, list):
        if len(st_raw) != n_obs:
            raise ReviewInputError("finding lifecycle compact state column invalid")
        st_col = st_raw
    elif n_obs > 0 and (type(st_raw) is int or isinstance(st_raw, str)):
        st_col = [st_raw] * n_obs
    else:
        raise ReviewInputError("finding lifecycle compact state column invalid")

    g_raw = obs_col.get("g")
    if isinstance(g_raw, list):
        if len(g_raw) != n_obs:
            raise ReviewInputError("finding lifecycle compact gen column invalid")
        g_col = g_raw
    elif n_obs > 0 and type(g_raw) is int:
        g_col = [g_raw] * n_obs
    else:
        raise ReviewInputError("finding lifecycle compact gen column invalid")

    rk_raw = obs_col.get("rk")
    if rk_raw == "$id" and n_obs > 0:
        rk_col: list[Any] = list(range(n_obs))
    elif rk_raw is None and n_obs > 0:
        rk_col = [None] * n_obs
    elif type(rk_raw) is int and n_obs > 0:
        rk_col = [rk_raw] * n_obs
    elif isinstance(rk_raw, list) and len(rk_raw) == n_obs:
        rk_col = rk_raw
    else:
        raise ReviewInputError("finding lifecycle compact rank column invalid")

    tb_raw = obs_col.get("tb")
    if tb_raw is None and n_obs > 0:
        tb_col: list[Any] = [None] * n_obs
    elif type(tb_raw) is int and 1 <= tb_raw <= 4 and n_obs > 0:
        tb_col = [tb_raw] * n_obs
    elif isinstance(tb_raw, list) and len(tb_raw) == n_obs:
        tb_col = tb_raw
    else:
        raise ReviewInputError("finding lifecycle compact binding column invalid")

    raw_tids = obs_col.get("tids")
    cids = obs_col.get("cids")
    if not isinstance(cids, list) or any(type(c) is not int for c in cids):
        raise ReviewInputError("finding lifecycle compact binding IDs invalid")
    if raw_tids == "$prrt":
        if not cids:
            raise ReviewInputError("finding lifecycle compact binding IDs invalid")
        tids: list[str] = []
        acc_cid = 0
        for dc_val in cids:
            acc_cid += dc_val
            tids.append(f"PRRT_{acc_cid}")
    elif (
        isinstance(raw_tids, list)
        and len(raw_tids) == len(cids)
        and all(isinstance(t, str) for t in raw_tids)
    ):
        tids = raw_tids
    else:
        raise ReviewInputError("finding lifecycle compact binding IDs invalid")

    rp_raw = obs_col.get("rp")
    if rp_raw is None:
        rp_col: list[Any] = [None] * n_obs
    elif (
        n_obs > 0
        and isinstance(rp_raw, list)
        and len(rp_raw) == 3
        and rp_raw[0] == "$co"
        and type(rp_raw[1]) is int
        and type(rp_raw[2]) is int
    ):
        rp_col = [rp_raw] * n_obs
    elif isinstance(rp_raw, list) and len(rp_raw) == n_obs:
        rp_col = rp_raw
    else:
        raise ReviewInputError("finding lifecycle compact proofs invalid")

    rc_raw = obs_col.get("rc")
    if rc_raw is None and n_obs > 0:
        rc_col: list[Any] = [None] * n_obs
    elif rc_raw == 1 and n_obs > 0:
        rc_col = [1] * n_obs
    elif isinstance(rc_raw, list) and len(rc_raw) == n_obs:
        rc_col = rc_raw
    else:
        raise ReviewInputError("finding lifecycle compact comments invalid")

    rc_b = obs_col.get("rc_b")
    rc_e = obs_col.get("rc_e")
    if (
        not isinstance(rc_b, list)
        or not isinstance(rc_e, list)
        or len(rc_b) != len(rc_e)
        or any(not isinstance(b, str) for b in rc_b)
        or any(e is not None and not isinstance(e, str) for e in rc_e)
    ):
        raise ReviewInputError("finding lifecycle compact comment bodies invalid")

    co_raw = obs_col.get("co")
    if co_raw is None:
        co_col: list[Any] = [None] * n_obs
    elif isinstance(co_raw, list) and len(co_raw) == n_obs:
        co_col = co_raw
    else:
        raise ReviewInputError("finding lifecycle compact overrides invalid")

    crit_def = st.get("cd")
    anc_def = st.get("ad")
    rc_def = st.get("rd")

    ob_comments: list[Any] = []
    obs_partials: list[tuple[Any, ...]] = []
    tid_cursor = 0
    prev_cid = 0
    rc_cursor = 0
    for idx in range(n_obs):
        crit_row = crit_def if c_col[idx] == 0 else c_col[idx]
        anc_row = anc_def if a_col[idx] == 0 else a_col[idx]
        if not isinstance(crit_row, list) or len(crit_row) != len(_COMPACT_CRIT_FIELDS):
            raise ReviewInputError("finding lifecycle compact criterion is invalid")
        if not isinstance(anc_row, list) or len(anc_row) != len(_COMPACT_ANCHOR_FIELDS):
            raise ReviewInputError("finding lifecycle compact anchor is invalid")
        l_state_raw = st_col[idx]
        if type(l_state_raw) is int:
            if not 0 <= l_state_raw < len(_COMPACT_STATE_LIST):
                raise ReviewInputError("finding lifecycle compact state code invalid")
            l_state: Any = _COMPACT_STATE_LIST[l_state_raw]
        else:
            l_state = l_state_raw
        gen = g_col[idx]
        rank = rk_col[idx]
        tb_item = tb_col[idx]
        tb_row: list[Any] | None = None
        if type(tb_item) is int and 1 <= tb_item <= 4:
            if tid_cursor >= len(tids):
                raise ReviewInputError("finding lifecycle compact tids underflow")
            t_id = tids[tid_cursor]
            c_id = prev_cid + cids[tid_cursor]
            prev_cid = c_id
            tid_cursor += 1
            if tb_item == 1:
                tb_row = [t_id, c_id, "inline", None, "active", "$d1", False, False]
            elif tb_item == 2:
                tb_row = [t_id, c_id, "inline", None, "active", "$d2", False, False]
            elif tb_item == 3:
                tb_row = [
                    t_id,
                    c_id,
                    "inline",
                    None,
                    "released",
                    "$res",
                    False,
                    False,
                ]
            else:
                tb_row = [
                    t_id,
                    c_id,
                    "inline",
                    None,
                    "released",
                    "$rel",
                    False,
                    True,
                ]
        elif tb_item is not None:
            if not isinstance(tb_item, list) or len(tb_item) != len(
                _COMPACT_BINDING_FIELDS
            ):
                raise ReviewInputError("finding lifecycle compact binding row invalid")
            tb_row = tb_item

        rc_item = rc_col[idx]
        rc_row: list[Any] | None = None
        if rc_item == 1:
            if not isinstance(rc_def, list) or len(rc_def) != len(
                _COMPACT_COMMENT_FIELDS
            ):
                raise ReviewInputError("finding lifecycle compact comment is invalid")
            if rc_cursor >= len(rc_b):
                raise ReviewInputError("finding lifecycle compact rc_b underflow")
            rc_row = [
                *rc_def[:2],
                rc_b[rc_cursor],
                *rc_def[3:9],
                rc_e[rc_cursor],
                *rc_def[10:],
            ]
            rc_cursor += 1
        elif rc_item is not None:
            if not isinstance(rc_item, list) or len(rc_item) != len(
                _COMPACT_COMMENT_FIELDS
            ):
                raise ReviewInputError("finding lifecycle compact comment is invalid")
            rc_row = rc_item

        crit_dict = dict(zip(_COMPACT_CRIT_FIELDS, crit_row))
        crit_dict["origin_head_sha"] = resolve_sha(crit_dict["origin_head_sha"])
        anc_dict = dict(zip(_COMPACT_ANCHOR_FIELDS, anc_row))
        anc_dict["head_sha"] = resolve_sha(anc_dict["head_sha"])
        if rc_row is None:
            c_dict = None
            comment = None
        else:
            c_dict = dict(zip(_COMPACT_COMMENT_FIELDS, rc_row))
            comment = ReviewComment(**c_dict)
        ob_comments.append(comment)
        crit_override = co_col[idx]
        if crit_override is None:
            if comment is None:
                raise ReviewInputError("finding lifecycle compact criterion missing")
            oid, orig_fp, concern, rc_dig = _compact_criterion_hashes(
                comment,
                origin_head_sha=crit_dict["origin_head_sha"],
                defect_kind=crit_dict["defect_kind"],
                path=crit_dict["origin_path"],
                symbol=crit_dict["origin_symbol"],
            )
        else:
            if not isinstance(crit_override, list) or len(crit_override) != 4:
                raise ReviewInputError(
                    "finding lifecycle compact criterion override is invalid"
                )
            oid = hex_at(crit_override[0])
            orig_fp = hex_at(crit_override[1])
            concern = None if crit_override[2] is None else hex_at(crit_override[2])
            rc_dig = hex_at(crit_override[3])
        crit_dict.update(
            obligation_id=oid,
            original_fingerprint=orig_fp,
            concern=concern,
            resolution_criterion=rc_dig,
        )
        res_p = rp_col[idx]
        if res_p is None:
            res_sha: str | None = None
        elif isinstance(res_p, list) and len(res_p) == 3 and res_p[0] == "$co":
            a_i, sr_i = res_p[1], res_p[2]
            if (
                type(a_i) is not int
                or not 0 <= a_i < len(used_actors)
                or sr_i not in (0, 1)
                or type(sr_i) is not int
            ):
                raise ReviewInputError(
                    "finding lifecycle compact override proof invalid"
                )
            res_sha = hashlib.sha256(
                b"reviewsensei:drift-command-override:v1:"
                + canonical_bytes(
                    {
                        "obligation_id": oid,
                        "actor": used_actors[a_i],
                        "head_sha": h_sha,
                        "slot_released": bool(sr_i),
                    }
                )
            ).hexdigest()
        else:
            res_sha = hex_at(res_p)
        obs_partials.append(
            (crit_dict, anc_dict, l_state, gen, tb_row, res_sha, rank, c_dict)
        )

    if tid_cursor != len(tids) or rc_cursor != len(rc_b):
        raise ReviewInputError("finding lifecycle compact column count mismatch")

    rt = payload.get("rt")
    raw_receipts = payload.get("receipts")
    if raw_receipts == "$id":
        if not isinstance(rt, list) or len(rt) != 10 or n_obs == 0:
            raise ReviewInputError("finding lifecycle compact receipt template invalid")
        compact_receipts: list[Any] = list(range(n_obs))
    elif isinstance(raw_receipts, list):
        compact_receipts = raw_receipts
    else:
        raise ReviewInputError("finding lifecycle compact receipts invalid")

    inline_cache: dict[tuple[int, int, int, str, str, str], tuple[str, int]] = {}

    def derived_info(
        ob_i: int, repo_id: int, pr: int, rh: str, rb: str, st_cand: str
    ) -> tuple[str, int]:
        if not 0 <= ob_i < len(ob_comments) or ob_comments[ob_i] is None:
            raise ReviewInputError("finding lifecycle compact comment index is invalid")
        cache_key = (ob_i, repo_id, pr, rh, rb, st_cand)
        hit = inline_cache.get(cache_key)
        if hit is None:
            hit = _compact_derived_inline_body(
                ob_comments[ob_i],
                repository_id=repo_id,
                pull_request=pr,
                head_sha=rh,
                base_sha=rb,
                state=st_cand,
            )
            inline_cache[cache_key] = hit
        return hit

    receipts = []
    body_sha_by_thread: dict[str, str] = {}
    receipt_by_thread: dict[str, dict[str, Any]] = {}
    for entry in compact_receipts:
        if type(entry) is int:
            if not isinstance(rt, list) or len(rt) != 10:
                raise ReviewInputError(
                    "finding lifecycle compact receipt template invalid"
                )
            row = [*rt[:6], ["$ob", entry], *rt[6:]]
        else:
            row = entry
        if not isinstance(row, list) or len(row) != 11:
            raise ReviewInputError("finding lifecycle compact receipt is invalid")
        (
            repo_id,
            pr,
            r_head_raw,
            r_base_raw,
            prod_id,
            rev_id,
            inst_ref,
            body_ref,
            tid,
            cid,
            n_head_raw,
        ) = row
        r_head = resolve_sha(r_head_raw)
        r_base = resolve_sha(r_base_raw)
        n_head = resolve_sha(n_head_raw)
        if (
            not isinstance(r_head, str)
            or not isinstance(r_base, str)
            or type(repo_id) is not int
            or type(pr) is not int
        ):
            raise ReviewInputError("finding lifecycle compact receipt scope is invalid")
        ob_i_for_body: int | None = None
        if isinstance(inst_ref, list) and len(inst_ref) == 2 and inst_ref[0] == "$ob":
            idx_val = inst_ref[1]
            if (
                type(idx_val) is not int
                or not 0 <= idx_val < len(ob_comments)
                or ob_comments[idx_val] is None
            ):
                raise ReviewInputError(
                    "finding lifecycle compact receipt obligation is invalid"
                )
            inst_hex = finding_instance_fingerprint(ob_comments[idx_val])
            ob_i_for_body = idx_val
        elif isinstance(inst_ref, list) and len(inst_ref) == 2 and inst_ref[0] == "$h":
            inst_hex = hex_at(inst_ref[1])
        else:
            raise ReviewInputError(
                "finding lifecycle compact receipt instance is invalid"
            )
        if (
            isinstance(body_ref, list)
            and len(body_ref) == 2
            and body_ref[0] == "$inl"
            and ob_i_for_body is not None
            and body_ref[1] in {"new", "still-present"}
        ):
            body_hex = derived_info(
                ob_i_for_body, repo_id, pr, r_head, r_base, body_ref[1]
            )[0]
        elif isinstance(body_ref, list) and len(body_ref) == 2 and body_ref[0] == "$h":
            body_hex = hex_at(body_ref[1])
        else:
            raise ReviewInputError("finding lifecycle compact receipt body is invalid")
        if tid == "$tb" or cid == "$tb":
            if ob_i_for_body is None:
                raise ReviewInputError(
                    "finding lifecycle compact receipt binding is invalid"
                )
            tb_ref = obs_partials[ob_i_for_body][4]
            if not isinstance(tb_ref, list) or len(tb_ref) != len(
                _COMPACT_BINDING_FIELDS
            ):
                raise ReviewInputError(
                    "finding lifecycle compact receipt binding is invalid"
                )
            if tid == "$tb":
                tid = tb_ref[0]
            if cid == "$tb":
                cid = tb_ref[1]
        receipt = {
            "schema_version": "native-finding-receipt-v1",
            "repository_id": repo_id,
            "pull_request": pr,
            "head_sha": r_head,
            "base_sha": r_base,
            "producer_id": prod_id,
            "review_id": rev_id,
            "instance": inst_hex,
            "body_sha256": body_hex,
            "thread_id": tid,
            "comment_id": cid,
            "native_head_sha": n_head,
        }
        receipts.append(receipt)
        if isinstance(tid, str):
            body_sha_by_thread[tid] = body_hex
            receipt_by_thread[tid] = receipt

    obligations = []
    default_slots = []
    slot_by_ob_idx: dict[int, dict[str, Any]] = {}
    ch: dict[str, list[str]] = {}
    extra_ch = payload.get("ech")
    if not isinstance(extra_ch, dict):
        raise ReviewInputError("finding lifecycle compact concern history is invalid")

    for idx, (
        crit_dict,
        anc_dict,
        l_state,
        gen,
        tb_row,
        res_sha,
        rank,
        c_dict,
    ) in enumerate(obs_partials):
        oid = crit_dict["obligation_id"]
        if oid in extra_ch:
            val = extra_ch[oid]
            if val is not None:
                if not isinstance(val, list):
                    raise ReviewInputError("finding lifecycle compact history invalid")
                ch[oid] = [hex_at(i) for i in val]
        else:
            ch[oid] = [crit_dict["original_fingerprint"]]
        if tb_row is None:
            tb_dict = None
        else:
            tb_dict = dict(zip(_COMPACT_BINDING_FIELDS, tb_row))
            tid, cid = tb_dict["thread_id"], tb_dict["comment_id"]
            p_raw = tb_dict["proof_sha256"]
            if p_raw == "$res":
                if res_sha is None:
                    raise ReviewInputError("finding lifecycle compact proof invalid")
                tb_dict["proof_sha256"] = res_sha
            elif p_raw == "$d1":
                if not isinstance(tid, str) or tid not in body_sha_by_thread:
                    raise ReviewInputError("finding lifecycle compact proof invalid")
                tb_dict["proof_sha256"] = hashlib.sha256(
                    canonical_bytes(
                        {
                            "thread_id": tid,
                            "comment_id": cid,
                            "head_sha": h_sha,
                            "obligation_id": oid,
                            "placement": tb_dict["placement"],
                            "body_sha256": body_sha_by_thread[tid],
                        }
                    )
                ).hexdigest()
            elif p_raw == "$d2":
                tb_dict["proof_sha256"] = hashlib.sha256(
                    canonical_bytes(
                        {
                            "thread_id": tid,
                            "comment_id": cid,
                            "head_sha": h_sha,
                            "obligation_id": oid,
                        }
                    )
                ).hexdigest()
            elif p_raw == "$rel":
                if (
                    res_sha is None
                    or not isinstance(tid, str)
                    or tid not in receipt_by_thread
                ):
                    raise ReviewInputError("finding lifecycle compact proof invalid")
                tb_dict["proof_sha256"] = hashlib.sha256(
                    canonical_bytes(
                        {
                            "kind": "confirmed-finding-slot-release-v1",
                            "resolution": res_sha,
                            "receipt": receipt_by_thread[tid],
                            "head_sha": h_sha,
                        }
                    )
                ).hexdigest()
            elif isinstance(p_raw, list) and len(p_raw) == 2 and p_raw[0] == "$h":
                tb_dict["proof_sha256"] = hex_at(p_raw[1])
            else:
                raise ReviewInputError("finding lifecycle compact proof invalid")
            slot_dict = {
                "proof_sha256": tb_dict["proof_sha256"],
                "state": tb_dict["slot_state"],
                "target": tid,
            }
            default_slots.append(slot_dict)
            slot_by_ob_idx[idx] = slot_dict
        obligations.append(
            {
                "criterion": crit_dict,
                "generation": gen,
                "latest_comment": c_dict,
                "lifecycle_state": l_state,
                "present_anchor": anc_dict,
                "proposal_rank": rank,
                "resolution_proof_sha256": res_sha,
                "thread_binding": tb_dict,
            }
        )

    slots_order = st.get("so")
    if slots_order is None:
        slot_dicts = default_slots
    else:
        if not isinstance(slots_order, list) or any(
            type(i) is not int or i not in slot_by_ob_idx for i in slots_order
        ):
            raise ReviewInputError("finding lifecycle compact slot order is invalid")
        slot_dicts = [slot_by_ob_idx[i] for i in slots_order]

    compact_active = st.get("active")
    if compact_active == "$def":
        if _compact_unresolved_order(obligations):
            raise ReviewInputError("finding lifecycle compact default active mismatch")
        compact_active = []
    elif (
        isinstance(compact_active, list)
        and len(compact_active) == 2
        and compact_active[0] == "$def"
        and isinstance(compact_active[1], str)
    ):
        unres_order = _compact_unresolved_order(obligations)
        if not unres_order:
            raise ReviewInputError("finding lifecycle compact default active mismatch")
        compact_active = [[i, "inline", None, compact_active[1]] for i in unres_order]
    elif not isinstance(compact_active, list):
        raise ReviewInputError("finding lifecycle compact active list is invalid")
    active_entry_dicts = []
    for ae_row in compact_active:
        if not isinstance(ae_row, list) or len(ae_row) != 4:
            raise ReviewInputError("finding lifecycle compact active entry is invalid")
        ob_idx, placement, body_reason, thread_action = ae_row
        if type(ob_idx) is not int or not 0 <= ob_idx < len(obligations):
            raise ReviewInputError("finding lifecycle compact active index is invalid")
        ob_d = obligations[ob_idx]
        tb_d = ob_d["thread_binding"]
        active_entry_dicts.append(
            {
                "obligation_id": ob_d["criterion"]["obligation_id"],
                "original_fingerprint": ob_d["criterion"]["original_fingerprint"],
                "origin_path": ob_d["criterion"]["origin_path"],
                "present_path": ob_d["present_anchor"]["path"],
                "present_line": ob_d["present_anchor"]["line"],
                "present_side": ob_d["present_anchor"]["side"],
                "drift_kind": ob_d["present_anchor"]["drift_kind"],
                "lifecycle_state": ob_d["lifecycle_state"],
                "placement": placement,
                "body_reason": body_reason,
                "thread_id": None if tb_d is None else tb_d["thread_id"],
                "thread_action": thread_action,
            }
        )

    coverage_obj = _decode(st.get("changed_file_coverage"), ChangedFileCoverageState)
    inv_obj = DriftInventory(
        obligations=tuple(_decode(ob_d, DriftTrackedObligation) for ob_d in obligations)
    )
    entries_obj = tuple(_decode(ae_d, DriftActiveEntry) for ae_d in active_entry_dicts)
    slots_obj = tuple(_decode(s_d, PublishedSlot) for s_d in slot_dicts)
    review_complete = st.get("review_complete")
    if not isinstance(review_complete, bool):
        raise ReviewInputError("finding lifecycle compact review_complete is invalid")
    approved, reasons, steps = _recompute_drift_verdict(
        coverage_state=coverage_obj,
        review_complete=review_complete,
        inventory=inv_obj,
        active_entries=entries_obj,
        slots=slots_obj,
    )
    reconstructed_state = {
        "head_sha": h_sha,
        "base_sha": b_sha,
        "round_number": st.get("round_number"),
        "review_complete": review_complete,
        "max_active_findings": st.get("max_active_findings"),
        "changed_file_coverage": st.get("changed_file_coverage"),
        "inventory": {"obligations": obligations},
        "active_entries": active_entry_dicts,
        "slots": slot_dicts,
        "approved": approved,
        "withheld_reasons": list(reasons),
        "recovery_steps": [_encode(step) for step in steps],
        "body_placement_note": st.get("body_placement_note"),
    }

    def expand_tree(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$sh"}:
                sh_idx = value["$sh"]
                if sh_idx == 0:
                    return h_sha
                if sh_idx == 1:
                    return b_sha
                raise ReviewInputError("finding lifecycle compact sha index invalid")
            if set(value) == {"$h"}:
                return hex_at(value["$h"])
            if set(value) == {"$rb"}:
                r_idx = value["$rb"]
                if type(r_idx) is not int or not 0 <= r_idx < len(receipts):
                    raise ReviewInputError(
                        "finding lifecycle compact body index invalid"
                    )
                return receipts[r_idx]["body_sha256"]
            if set(value) == {"$tid"}:
                t_idx = value["$tid"]
                if type(t_idx) is not int or not 0 <= t_idx < len(obs_partials):
                    raise ReviewInputError(
                        "finding lifecycle compact tid index invalid"
                    )
                t_ref = obs_partials[t_idx][4]
                if not isinstance(t_ref, list) or not isinstance(t_ref[0], str):
                    raise ReviewInputError(
                        "finding lifecycle compact tid binding invalid"
                    )
                return t_ref[0]
            if set(value) == {"$ib"}:
                spec = value["$ib"]
                if (
                    not isinstance(spec, list)
                    or len(spec) != 2
                    or type(spec[0]) is not int
                    or spec[1] not in {"new", "still-present"}
                    or not receipts
                ):
                    raise ReviewInputError(
                        "finding lifecycle compact inline body invalid"
                    )
                sample = receipts[0]
                dig, utf8_len = derived_info(
                    spec[0],
                    sample["repository_id"],
                    sample["pull_request"],
                    sample["head_sha"],
                    sample["base_sha"],
                    spec[1],
                )
                return {"$body_sha256": dig, "utf8_bytes": utf8_len}
            if set(value) == {"$icl"}:
                st_cand = value["$icl"]
                if (
                    st_cand not in {"new", "still-present"}
                    or not receipts
                    or not ob_comments
                    or any(c is None for c in ob_comments)
                ):
                    raise ReviewInputError(
                        "finding lifecycle compact comment list invalid"
                    )
                return [
                    {
                        "body": expand_tree({"$ib": [i, st_cand]}),
                        "line": ob_comments[i].line,
                        "path": ob_comments[i].path,
                        "side": ob_comments[i].side,
                    }
                    for i in range(len(ob_comments))
                ]
            return {key: expand_tree(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand_tree(item) for item in value]
        return value

    rp = payload.get("rp")
    if rp is not None and (
        not isinstance(rp, str) or re.fullmatch(r"/repos/[^/]+/[^/]+/", rp) is None
    ):
        raise ReviewInputError("finding lifecycle compact repo prefix is invalid")

    raw_effects = payload.get("effects")
    if not isinstance(raw_effects, list):
        raise ReviewInputError("finding lifecycle compact effects invalid")
    effects = []
    for raw_ef in raw_effects:
        ef = expand_tree(raw_ef)
        if not isinstance(ef, dict):
            raise ReviewInputError("finding lifecycle compact effect invalid")
        if (
            rp is not None
            and isinstance(ef.get("path"), str)
            and ef["path"].startswith("$rp/")
        ):
            ef["path"] = rp + ef["path"][4:]
        if "request_sha256" not in ef:
            ef["request_sha256"] = _digest(
                {
                    "method": ef.get("method"),
                    "path": ef.get("path"),
                    "request": _request_identity(ef.get("request")),
                }
            )
        effects.append(ef)

    has_ash = "ash" in payload
    reconstructed = {
        "schema_version": CURRENT_VERSION if has_ash else VERSION,
        "lifecycle": {
            "schema_version": payload.get("v"),
            "purpose": payload.get("p"),
            "mode": "drift",
            "state": reconstructed_state,
            "input_identity": expand_tree(payload.get("ii")),
            "concern_history": ch,
        },
        "effects": effects,
        "unknown_trust_effect": payload.get("ute"),
        "native_receipts": receipts,
        **(
            {"accepted_semantic_history": expand_tree(payload.get("ash"))}
            if has_ash
            else {}
        ),
    }
    if len(canonical_bytes(reconstructed)) > 1_048_576:
        raise ReviewInputError("finding lifecycle reconstructed byte capacity exceeded")
    return reconstructed


def _unpack_compact_journal(stored: object) -> dict[str, Any]:
    reconstructed = _validate(_unpack_compact_payload(stored))
    if _pack_compact_journal(reconstructed) != stored:
        raise ReviewInputError(
            "finding lifecycle compact representation is noncanonical"
        )
    return reconstructed


def _compact_historical_sizing_active(journal: object) -> bool:
    from review_sensei.source_registry import require_module_seal

    preflight = sys._getframe(2)
    outer = preflight.f_back
    if (
        preflight.f_code is not FindingLifecycleJournal._preflight_effect.__code__
        or preflight.f_globals is not vars(sys.modules[__name__])
        or preflight.f_locals.get("self") is not journal
        or outer is None
        or outer.f_globals is not vars(sys.modules[__name__])
    ):
        return False
    if (
        preflight.f_locals.get("capacity_only") is True
        and outer.f_code is FindingLifecycleJournal._command_branch_documents.__code__
        and outer.f_locals.get("shadow") is journal
    ):
        from . import quarantine_command_finalization

        require_module_seal(quarantine_command_finalization)
        measurement = outer.f_back
        return bool(
            measurement is not None
            and measurement.f_code
            is quarantine_command_finalization._measure_inline_branches.__code__
            and measurement.f_globals is vars(quarantine_command_finalization)
            and measurement.f_locals.get("journal") is outer.f_locals.get("self")
        )
    if (
        preflight.f_locals.get("capacity_only") is False
        and outer.f_code is FindingLifecycleJournal.request.__code__
        and outer.f_locals.get("self") is journal
    ):
        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        ledger = getattr(journal, "ledger", None)
        caller = None if ledger is None else getattr(ledger, "caller", None)
        return bool(
            caller is not None
            and ledger is not None
            and quarantine_historical_execution.available(caller, ledger)
        )
    return False


def _maximum_native_allocation(
    packed,
    *,
    preserve_observed=False,
    _pair_source=None,
    _compact_pair_source=None,
    _source_document=None,
):
    """A sizing-only value, never passed to stage or installed as authority."""
    import copy

    document = None
    if packed.get("schema_version") in {
        "finding-lifecycle-packed-v5",
        _COMPACT_VERSION,
    }:
        packed = _pack_journal(_unpack_journal(packed))
    if preserve_observed and _pair_source is not None:
        from review_sensei.source_registry import require_module_seal

        from . import finding_lifecycle_prefix, historical_prefix_sizing

        require_module_seal(historical_prefix_sizing)
        require_module_seal(finding_lifecycle_prefix)
        finding_lifecycle_prefix._sources()
        document = _unpack_journal(packed)
        if historical_prefix_sizing.retained_for_candidate(_pair_source, document):
            count = len(document["effects"]) - 1
            prefix = finding_lifecycle_prefix.prepare_prefix(
                document, prefix_count=count
            )
            remainder = finding_lifecycle_prefix.remainder(document, prefix_count=count)
            # Only unknown suffix atoms receive maxima. The separately proven
            # old effects and source-fixed updates retain their exact bytes;
            # synthetic confirmations are never treated as observations.
            maximum = _maximum_native_allocation(remainder, preserve_observed=True)
            return finding_lifecycle_prefix.assemble_data(prefix, remainder=maximum)
    path_bounds = (
        {} if _pair_source is None else _closed_pair_path_bound(packed, _pair_source)
    )
    result = (
        {
            **packed,
            "native_atoms": [
                [atom[0], atom[1]] if isinstance(atom, list) else copy.deepcopy(atom)
                for atom in packed["native_atoms"]
            ],
        }
        if isinstance(packed, dict) and type(packed.get("native_atoms")) is list
        else copy.deepcopy(packed)
    )
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
    compact_source = (
        _compact_pair_source if _compact_pair_source is not None else _pair_source
    )
    if (
        preserve_observed
        and compact_source is not None
        and _compact_historical_sizing_active(compact_source)
        and not any(
            atom[0] == "native" and str(atom[1]).startswith("capacity_")
            for atom in result["native_atoms"]
        )
    ):
        if (
            _source_document is not None
            and not path_bounds
            and _source_document.get("schema_version") in {VERSION, CURRENT_VERSION}
        ):
            return _pack_current_journal(_source_document, _legacy=result)
        reconstructed = _unpack_journal(result)
        if reconstructed.get("schema_version") in {
            VERSION,
            CURRENT_VERSION,
        }:
            return _pack_current_journal(reconstructed)
    return result


def _unpack_journal(stored):
    if not isinstance(stored, dict):
        raise ReviewInputError("finding lifecycle carrier schema is unsupported")
    if stored.get("schema_version") == _COMPACT_VERSION:
        return _unpack_compact_journal(stored)
    if stored.get("schema_version") == "finding-lifecycle-packed-v5":
        from review_sensei.source_registry import require_module_seal

        from . import finding_lifecycle_prefix

        require_module_seal(finding_lifecycle_prefix)
        return finding_lifecycle_prefix.unpack(stored)
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


def _check_request_matches(observed, expected, *, recovering=False):
    """Match the sent check projection, not GitHub's larger output envelope."""
    if not isinstance(observed, dict):
        return False
    # An acknowledged rescan update can reuse a completed check object. A
    # later observation retaining its previous outcome cannot alone prove
    # that an uncertain in-progress write was this operation's exact effect.
    if (
        recovering
        and expected.get("status") == "in_progress"
        and expected.get("conclusion") is None
        and observed.get("conclusion") is not None
    ):
        return False
    if _request_matches(observed, expected):
        return True
    output, sent = observed.get("output"), expected.get("output")
    # The current check producer sends only title and summary. GitHub adds
    # nullable text and annotation metadata. Do not ignore another rendered
    # field, annotations, extra output keys or differences in sent fields.
    if (
        not isinstance(sent, dict)
        or set(sent) != {"title", "summary"}
        or not all(isinstance(item, str) for item in sent.values())
        or not isinstance(output, dict)
        or not set(output)
        <= {"title", "summary", "text", "annotations_count", "annotations_url"}
        or output.get("text") is not None
    ):
        return False
    count = output.get("annotations_count", 0)
    url = output.get("annotations_url")
    if (
        type(count) is not int
        or count != 0
        or ("annotations_url" in output and (not isinstance(url, str) or not url))
    ):
        return False
    return _request_matches(
        {**observed, "output": {key: output.get(key) for key in sent}}, expected
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
    from .quarantine_lifecycle_transfer import clear_namespace_restore

    try:
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
            source_record = record
            document = _validate(document)
        else:
            if loaded is None:
                selected, stored = select_finding_lifecycle(ledger, identity)
            else:
                selected, stored = loaded, None
            if selected.status not in {"ok", "migrated", "missing"}:
                raise ReviewInputError(
                    "finding lifecycle selected session is unavailable: "
                    + selected.status
                )
            record = selected.record
            if record is not None:
                for item in record.dispositions:
                    if isinstance(item, dict):
                        register_compact_actor(item.get("actor"))
            caller = getattr(ledger, "caller", None)
            reconciled = getattr(caller, "_reconciled_prior_lifecycle", None)
            source_record = record
            if (
                loaded is None
                and record is not None
                and record.finding_lifecycle is not None
                and isinstance(reconciled, tuple)
                and len(reconciled) == 2
                and reconciled[0] == record.finding_lifecycle.get("operation_id")
            ):
                prior_record = reconciled[1]
                if prior_record is None or prior_record.finding_lifecycle is None:
                    # A reconciled failed Round 1 with no prior completed rounds or
                    # prior lifecycle intentionally restores an empty Round-0 baseline.
                    # Never silently drop retained state if any review state exists.
                    if (
                        record.completed_initial_reviews != 0
                        or record.completed_verification_rounds != 0
                        or record.transaction is not None
                        or bool(record.dispositions)
                        or record.disposition_snapshot is not None
                        or record.convergence_history is not None
                    ):
                        raise ReviewInputError(
                            "reconciled prior finding lifecycle is missing for a session with retained review state"
                        )
                    return record, None
                source_record = prior_record
            if source_record is None or source_record.finding_lifecycle is None:
                return record, None
            if loaded is not None or source_record is not record:
                stored = read_finding_lifecycle(
                    ledger,
                    source_record,
                    _selected_load=selected if source_record is record else None,
                )
            from .finding_lifecycle_dictionary import resolve_stored

            stored = resolve_stored(ledger, source_record, stored)
            document = _validate(_unpack_journal(stored))
        restored = decode_lifecycle_state(document["lifecycle"])
        state = restored.state
        binding = source_record.finding_lifecycle["manifest"]["binding"]
        if (
            state.head_sha != binding["head_sha"]
            or state.base_sha != binding["base_sha"]
        ):
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
        history = source_record.finding_lifecycle.get(
            "accepted_semantic_history", document.get("accepted_semantic_history")
        )
        if history is not None and history.get("qualification") != "unavailable":
            from .accepted_semantic_history import validate_history

            validate_history(history, identity)
        from .quarantine_namespace import inherited_namespace_is_historical

        if inherited_namespace_is_historical(ledger, source_record, state, document):
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
    finally:
        clear_namespace_restore(ledger)


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
        self._initial_effect_keys = frozenset(
            e["request_sha256"]
            for e in self.effects
            if isinstance(e, dict) and isinstance(e.get("request_sha256"), str)
        )
        self._initial_effect_count = len(self.effects)
        self._initial_had_pending = any(
            isinstance(e, dict) and e.get("status") != "confirmed" for e in self.effects
        )
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
        return _pack_current_journal(document) if packed is None else packed

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
        prefix_states: list[Any] = []
        for state in getattr(shadow, "_finalizer_capacity_states", ()):
            _append_unique_state(prefix_states, state)
        lifecycle_key = (self.identity.repository_id, self.identity.pull_request)
        current_state = (
            shadow._planned
            or self.reviewer._drift_states.get(lifecycle_key)
            or self.reviewer._active_window_states.get(lifecycle_key)
        )
        projection_sources_list: list[Any] = []
        for state in (current_state, *prefix_states):
            if state is not None:
                _append_unique_state(projection_sources_list, state)
        projection_sources = tuple(projection_sources_list)
        receipts_by_instance = {receipt["instance"]: receipt for receipt in receipts}
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
                for state in projection_sources:
                    _append_unique_state(
                        prefix_states,
                        project_confirmed_slot_release(
                            state,
                            key=lifecycle_key,
                            receipts=receipts_by_instance,
                            instances=confirmed_instances,
                            head_sha=self.head_sha,
                        ),
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
            dictionary_base = _envelope_for_manifest(
                self._document(), dictionary_manifest
            )
            if dictionary_base is None:
                raise ReviewInputError("command dictionary base representation changed")
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
        states_list: list[Any] = []
        for state in (initial_state, *cleanup_capacity_states(plan)):
            if state is not None:
                _append_unique_state(states_list, state)
        states = tuple(states_list)
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

                packed_dictionary = pack_dictionary(
                    _unpack_journal(pending),
                    base=dictionary_base,
                    manifest=dictionary_manifest,
                    current_binding=dictionary_manifest["binding"],
                )
                if len(canonical_bytes(packed_dictionary)) < len(
                    canonical_bytes(pending)
                ):
                    pending = packed_dictionary
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
                receipts_by_instance = {r["instance"]: r for r in receipts}
                released_states: list[Any] = []
                for state in states:
                    _append_unique_state(
                        released_states,
                        project_confirmed_slot_release(
                            state,
                            key=key,
                            receipts=receipts_by_instance,
                            instances=confirmed_instances,
                            head_sha=self.head_sha,
                        ),
                    )
                shadow._finalizer_capacity_states = tuple(released_states)
            elif "check_update_precondition" not in effect:
                # New check/approval identities are not predictable source IDs.
                predictable = False
        if not documents:
            # Still price the final retained checkpoint and source restore.
            fallback_doc = shadow._document()
            documents = [
                _pack_current_journal(fallback_doc)
                if fallback_doc.get("schema_version") in {VERSION, CURRENT_VERSION}
                else _pack_journal(fallback_doc)
            ]
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

        self.last_capacity_breakdown = None

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
        from review_sensei.finding_lifecycle_store import (
            estimate_lifecycle_branch_parts,
            preflight_finding_lifecycle,
        )

        from .api_run_requests import ApiRunSessionLedger
        from .review_run import ActionRunLedger

        payload = json.loads(plan.payload)
        from .current_quarantine_finalization import CONTRACT, require_plan

        current_contract = payload.get("read_contract") == CONTRACT
        if current_contract:
            require_plan(plan, self, preview_record, capacity_details)
        reads = payload.get("execution_read_tail")
        capabilities = payload.get("execution_capability_tail")
        if (
            (
                not current_contract
                and payload.get("read_contract")
                != "closed-plain-single-complete-page-v1"
            )
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
            from review_sensei.bounded_evidence import read_inline_evidence
            from review_sensei.disposition_snapshot import (
                normalize_snapshot,
                validate_page_index,
            )

            snapshot = normalize_snapshot(snapshot)
            if snapshot is None or snapshot["schema_version"] not in {"1.0", "2.0"}:
                raise ReviewInputError(
                    "command finalization snapshot reads are unqualified"
                )
            manifest = snapshot["state_manifest"]
            if snapshot["schema_version"] == "1.0":
                if manifest["encoding"] == "partitioned-json-v1":
                    snapshot_reads = len(manifest["parts"])
                elif manifest["encoding"] != "inline-json-v1":
                    raise ReviewInputError(
                        "command finalization snapshot reads are unqualified"
                    )
            else:
                if manifest["encoding"] != "inline-json-v1":
                    raise ReviewInputError(
                        "command finalization snapshot reads are unqualified"
                    )
                index_doc = read_inline_evidence(
                    manifest, expected_binding=manifest["binding"]
                )
                pages = validate_page_index(index_doc, root=snapshot)
                snapshot_reads = sum(
                    len(page["parts"])
                    for page in pages
                    if page.get("encoding") == "partitioned-json-v1"
                )
        from review_sensei.api_work import LEGACY_ACCOUNTING_VERSION

        from . import action_receipt
        from .api_run_plan import (
            COMMAND_RECEIPT_CONFIRMATION_REQUESTS,
            READ_OPERATION_REQUESTS,
            owner_allocations,
        )
        from .trust_effects import verification_profile

        body_receipt_calls = int(
            bool(
                payload.get("review_update")
                and self._body_receipts(
                    int(payload["review_update"][1].rsplit("/", 1)[-1])
                )
            )
        )
        cleanup_threads_calls = payload.get("cleanup_native_calls", 0)
        review_update_calls = (
            2 if payload.get("review_update") is not None else 0
        ) + body_receipt_calls
        check_run_calls = 2 if payload.get("check_request") is not None else 0
        approval_calls = 2 if payload.get("conditional_approval") is not None else 0
        known_non_cleanup = (
            int(payload.get("review_update") is not None)
            + int(payload.get("check_request") is not None)
            + int(payload.get("conditional_approval") is not None)
        )
        other_non_cleanup = max(
            0,
            sum(path != "/graphql" for _, path, _ in requests) - known_non_cleanup,
        )
        native_calls = sum(reads.values()) + sum(capabilities.values())
        native_calls += cleanup_threads_calls
        # Exact native write and one possible readback per non-cleanup effect.
        # Cleanup already carries its own complete mutation/readback tail.
        native_calls += 2 * sum(path != "/graphql" for _, path, _ in requests)
        native_calls += body_receipt_calls
        native_calls += restore_calls + snapshot_reads
        is_plain_action_command = (
            not current_contract
            and not getattr(self.ledger, "_quarantine_required", False)
            and type(self.ledger) is ActionRunLedger
            and verification_profile(caller) is None
            and payload.get("read_contract") == "closed-plain-single-complete-page-v1"
        )
        plain_receipt_plan = None
        if is_plain_action_command and getattr(
            self.ledger, "_inventory_complete", False
        ):
            lifecycle_parts = estimate_lifecycle_branch_parts(
                self.ledger,
                self._record,
                alternatives=alternatives,
                operation_id=self.operation_id,
                base_sha=self.base_sha,
                head_sha=self.head_sha,
                policy_digest=self.policy_digest,
                configuration_digest=self.configuration_digest,
                item_count=item_count,
                projected_record=preview_record,
            )
            plain_receipt_plan = action_receipt.plan_plain_receipt_pages(
                self.ledger,
                self.identity,
                staged_parts_count=len(capacity_details["staged_parts"]),
                lifecycle_parts_count=lifecycle_parts,
            )
        if plain_receipt_plan is not None:
            receipt_calls = 6 + 2 * len(plain_receipt_plan[1])
        elif is_plain_action_command:
            receipt_calls = (
                4 + READ_OPERATION_REQUESTS + COMMAND_RECEIPT_CONFIRMATION_REQUESTS
            )
        else:
            receipt_calls = 6
        acct = caller.accounting
        alloc = owner_allocations(
            acct.configuration.limit,
            startup=acct.startup.liability,
            legacy=getattr(acct, "version", None) == LEGACY_ACCOUNTING_VERSION,
        )
        caller_allocation = {
            "limit": acct.configuration.limit,
            "startup": acct.startup.liability,
            "first_work": alloc.first_work,
            "terminal": alloc.first_terminal,
            "successor": alloc.successor_work + alloc.successor_terminal,
        }
        tail_breakdown = {
            "native_tail_calls": {
                "reads": dict(reads),
                "capability_mints": sum(capabilities.values()),
                "review_update": review_update_calls,
                "cleanup_threads": cleanup_threads_calls,
                "check_run": check_run_calls,
                "approval": approval_calls,
                "other_writes": 2 * other_non_cleanup,
                "restore_and_snapshot_reads": restore_calls + snapshot_reads,
                "total": native_calls,
            },
            "receipt_calls": receipt_calls,
        }
        try:
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
                reserved_tail_calls=native_calls + receipt_calls,
                projected_root=projected,
                branch_documents=branch_proof,
                tail_breakdown=tail_breakdown,
                caller_allocation=caller_allocation,
            )
        except Exception as error:
            self.last_capacity_breakdown = getattr(
                error, "reserved_cost_breakdown", None
            )
            raise
        self.last_capacity_breakdown = result.get("reserved_cost_breakdown")
        self.preflight_native_tail(calls=native_calls)
        if plain_receipt_plan is not None:
            inv_ids, page_requests, page_bounds, max_items = plain_receipt_plan
            action_receipt._bind_plain_single_page_receipt(
                self.ledger,
                caller,
                self.identity,
                operation_id=self.operation_id,
                page_requests=page_requests,
                page_bounds=page_bounds,
                inventory_ids=inv_ids,
                maximum_items=max_items,
            )
        plan.bind_preview_capacity(
            preview_record, capacity_details, pending_documents=pending_commitments
        )
        if current_contract:
            from .current_quarantine_finalization import capture_completed_preview

            capture_completed_preview(
                plan,
                self,
                preview_record=preview_record,
                details=capacity_details,
                alternatives=alternatives,
                item_count=item_count,
                quote=result,
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
            variants = [commit_window_publication(state, created_targets=targets)]
            _append_unique_state(
                variants, commit_window_publication(state, uncertain_targets=targets)
            )
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
            variants = [commit_drift_publication(state, created_threads=targets)]
            _append_unique_state(
                variants,
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
            )
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
            _append_unique_state(variants, preview)
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
                    effects_prefix = document["effects"][:-1]
                    last_effect_base = document["effects"][-1]
                    schema = document.get("schema_version")
                    is_paired_schema = schema == "finding-lifecycle-journal-v3" or (
                        schema == "finding-lifecycle-journal-v4"
                        and document.get("base_schema_version")
                        == "finding-lifecycle-journal-v3"
                    )
                    nonconfirmed_base: tuple[dict[str, Any], list[list[Any]]] | None = (
                        None
                    )
                    for effect_status, effect_response in outcomes:
                        for unknown_hold in {document["unknown_trust_effect"], True}:
                            outcome = {
                                **document,
                                "unknown_trust_effect": unknown_hold,
                                "effects": [
                                    *effects_prefix,
                                    {
                                        **last_effect_base,
                                        "status": effect_status,
                                        "response": effect_response,
                                    },
                                ],
                            }
                            if not is_paired_schema and effect_status != "confirmed":
                                if nonconfirmed_base is None:
                                    packed_outcome, base_skeleton = (
                                        _pack_journal_with_skeleton(outcome)
                                    )
                                    nonconfirmed_base = (
                                        base_skeleton,
                                        packed_outcome["native_atoms"],
                                    )
                                else:
                                    base_skeleton, base_atoms = nonconfirmed_base
                                    if len(canonical_bytes(outcome)) > 1_048_576:
                                        raise ReviewInputError(
                                            "finding lifecycle complete byte capacity exceeded"
                                        )
                                    outcome_skeleton = {
                                        **base_skeleton,
                                        "effects": [
                                            *base_skeleton["effects"][:-1],
                                            {
                                                **base_skeleton["effects"][-1],
                                                "response": effect_response,
                                                "status": effect_status,
                                            },
                                        ],
                                        "unknown_trust_effect": unknown_hold,
                                    }
                                    packed_outcome = {
                                        "schema_version": "finding-lifecycle-packed-v3",
                                        "journal": encode_evidence(
                                            outcome_skeleton,
                                            max_decoded_bytes=1_048_576,
                                        ),
                                        "native_atoms": base_atoms,
                                    }
                            else:
                                packed_outcome = _pack_journal(outcome)
                            candidates.append(
                                _maximum_native_allocation(
                                    packed_outcome,
                                    _pair_source=self
                                    if schema == "finding-lifecycle-journal-v3"
                                    else None,
                                    _compact_pair_source=self
                                    if schema in {VERSION, CURRENT_VERSION}
                                    else None,
                                    _source_document=outcome,
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
            self._pending_wire(document)
            if pending_intent
            else _pack_current_journal(document)
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

    def _compact_finished_command_effects(self, current_state) -> None:
        caller = getattr(self.ledger, "caller", None)
        if caller is None or getattr(caller, "job", None) != "command":
            return
        if any(effect.get("status") != "confirmed" for effect in self.effects):
            return
        key = (self.identity.repository_id, self.identity.pull_request)
        if key in self.reviewer._unknown_effect_holds:
            return
        from review_sensei.source_registry import require_module_seal

        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        if quarantine_historical_execution.available(caller, self.ledger):
            return
        released_threads: set[str] = set()
        if (
            current_state is not None
            and current_state.head_sha == self.head_sha
            and current_state.base_sha == self.base_sha
        ):
            from review_sensei.active_finding_window import ActiveWindowRoundState

            if isinstance(current_state, ActiveWindowRoundState):
                released_threads.update(
                    slot.target
                    for slot in current_state.display_active_findings.slots
                    if slot.state == "released" and isinstance(slot.target, str)
                )
                released_threads.update(
                    obligation.native_target
                    for obligation in current_state.authoritative_decision_inventory.obligations
                    if obligation.target_state == "released"
                    and isinstance(obligation.native_target, str)
                )
            else:
                released_threads.update(
                    slot.target
                    for slot in current_state.slots
                    if slot.state == "released" and isinstance(slot.target, str)
                )
        protected = (
            3 if getattr(self, "_guarded_publication_pair", None) is not None else 0
        )
        prefix = self.effects[:protected]
        tail = self.effects[protected:]
        review_prefix = (
            f"/repos/{self.identity.repository}/pulls/"
            f"{self.identity.pull_request}/reviews/"
        )
        check_prefix = f"/repos/{self.identity.repository}/check-runs/"
        last_put_by_path: dict[str, int] = {}
        last_check_patch_by_path: dict[str, int] = {}
        for index, effect in enumerate(tail):
            method = effect.get("method")
            path = effect.get("path")
            if not isinstance(path, str):
                continue
            if method == "PUT" and path.startswith(review_prefix):
                last_put_by_path[path] = index
            elif method == "PATCH" and path.startswith(check_prefix):
                last_check_patch_by_path[path] = index
        initial_keys: frozenset[object] = getattr(
            self, "_initial_effect_keys", frozenset()
        )
        if (
            not getattr(self, "_initial_had_pending", False)
            and len(self.effects) == getattr(self, "_initial_effect_count", -1)
            and all(
                effect.get("request_sha256") in initial_keys for effect in self.effects
            )
        ):
            return
        kept_tail = []
        for index, effect in enumerate(tail):
            method = effect.get("method")
            path = effect.get("path")
            if (
                method == "PUT"
                and isinstance(path, str)
                and path.startswith(review_prefix)
                and last_put_by_path.get(path) != index
            ):
                continue
            if (
                method == "PATCH"
                and isinstance(path, str)
                and path.startswith(check_prefix)
                and last_check_patch_by_path.get(path) != index
            ):
                continue
            target = _thread_effect_target(effect)
            if (
                target is not None
                and target[2]
                and effect.get("request_sha256") in initial_keys
                and target[1] in released_threads
            ):
                continue
            kept_tail.append(effect)
        self.effects = prefix + kept_tail

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
        self._compact_finished_command_effects(current)
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
        request = _request_identity(raw_request)
        key = _digest({"method": method, "path": path, "request": request})
        legacy_key = _digest({"method": method, "path": path, "request": raw_request})
        prepared_thread = getattr(self, "_prepared_thread_effect", None)
        if not _prepare_only and prepared_thread is not None:
            effect, record, root = prepared_thread
            caller = getattr(self.ledger, "caller", None)
            if (
                effect not in self.effects
                or method != "POST"
                or path != "/graphql"
                or effect["status"] != "pending"
                or effect["method"] != method
                or effect["path"] != path
                or effect["request"] != request
                or effect["request_sha256"] != key
                or self._record is not record
                or record.record_sha256 != root
                or (caller is not None and caller._root != root)
            ):
                raise ReviewInputError(
                    "finding lifecycle prepared thread resolution authority changed"
                )
            self._prepared_thread_effect = None
            return self._dispatch_effect(
                http, effect, method, path, token=token, body=body, **kwargs
            )
        if (
            _prepare_only
            and path == "/graphql"
            and any(e["request_sha256"] in {key, legacy_key} for e in self.effects)
        ):
            return 0, None
        ordered = getattr(self, "_ordered_native_batch", None)
        if ordered is not None:
            ordered.before_request(method, path, raw_request)
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
                _pack_current_journal(self._document(effects=[*self.effects, effect]))
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
            if path == "/graphql":
                self._prepared_thread_effect = (
                    effect,
                    self._record,
                    self._record.record_sha256,
                )
                return 0, None
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
        from review_sensei.source_registry import require_module_seal

        from . import quarantine_historical_execution

        require_module_seal(quarantine_historical_execution)
        quarantine_historical_execution.before_native_attempt(
            self, effect, method, path, body
        )
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

    def _check_update_matches(self, effect, payload, *, recovered=False):
        precondition = effect.get("check_update_precondition")
        if not (
            isinstance(precondition, dict)
            and self._check_commitment(payload) == precondition
            and payload.get("errors") in (None, [])
            and _check_request_matches(payload, effect["request"], recovering=recovered)
        ):
            return False
        source_app_id = getattr(self.ledger, "authenticated_app_id", None)
        if (
            recovered
            and source_app_id is not None
            and precondition["app_id"] != source_app_id
        ):
            return False
        # If this journal created the same check, keep its request's external
        # identity bound even on an exact-target update. Independent legacy
        # update preconditions do not contain this optional provenance.
        for prior in self.effects:
            if prior is effect:
                break
            if (
                prior["method"] == "POST"
                and prior["path"] + f"/{precondition['check_run_id']}" == effect["path"]
                and prior["status"] == "confirmed"
                and isinstance(prior["response"][1], dict)
                and prior["response"][1].get("id") == precondition["check_run_id"]
                and prior["response"][1].get("app_id") == precondition["app_id"]
                and "external_id" in prior["request"]
                and payload.get("external_id") != prior["request"]["external_id"]
            ):
                return False
        return True

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
    def prepare_thread_resolution(self, *, token, body):
        """Stage thread cleanup intent before opening caller readback correlation."""
        if not self._armed:
            yield None
            return
        if getattr(self, "_prepared_thread_effect", None) is not None:
            raise ReviewInputError(
                "finding lifecycle thread resolution preparation is nested"
            )
        caller = getattr(self.ledger, "caller", None)
        self.request(
            self._readback_http,
            "POST",
            "/graphql",
            token=token,
            body=body,
            _prepare_only=True,
        )
        reconciled_before = (
            frozenset(caller._reconciled_mutations)
            if caller is not None and hasattr(caller, "_reconciled_mutations")
            else frozenset()
        )
        prepared = getattr(self, "_prepared_thread_effect", None)
        effect = prepared[0] if prepared is not None else None

        def confirm_reconciled(thread_id: str, *, caller_confirmed: bool) -> None:
            if effect is None or effect["status"] != "pending":
                return
            if caller is not None and hasattr(caller, "_reconciled_mutations"):
                if not caller_confirmed:
                    return
                newly_reconciled = caller._reconciled_mutations - reconciled_before
                if len(newly_reconciled) != 1 or not any(
                    str(obs.get("request_id")) in newly_reconciled
                    and obs.get("mutation")
                    for obs in caller.observations
                ):
                    return
            target = _thread_effect_target(effect)
            if target != ("resolveReviewThread", thread_id, True):
                return
            payload = {
                "data": {
                    "resolveReviewThread": {
                        "thread": {"id": thread_id, "isResolved": True}
                    }
                }
            }
            receipt = self._compact_response(payload, effect=effect)
            effect["status"], effect["response"] = "confirmed", [200, receipt]

        try:
            yield confirm_reconciled
        finally:
            self._prepared_thread_effect = None

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
        expected_slug = (self.app_slug or "").removesuffix("[bot]")
        if (
            "/check-runs/" in path
            and effect["method"] == "PATCH"
            and response is None
            and "check_update_precondition" not in effect
        ):
            return None
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
            external_id = request.get("external_id")
            if (
                path != f"/repos/{self.identity.repository}/check-runs"
                or request.get("name") != "ReviewSensei"
                or request.get("head_sha") != self.head_sha
                or not isinstance(external_id, str)
                or not external_id
            ):
                return None
            status, payload = http.request(
                "GET",
                path.rsplit("/check-runs", 1)[0]
                + "/commits/"
                + self.head_sha
                + "/check-runs?check_name=ReviewSensei&per_page=100&filter=all",
                token=token,
            )
            runs = payload.get("check_runs") if isinstance(payload, dict) else None
            if (
                status == 200
                and isinstance(runs, list)
                and type(payload.get("total_count")) is int
                and payload["total_count"] == len(runs)
                and len(runs) < 100
            ):
                # The default latest filter can hide an older run with this
                # stable identity. Count all owned matches before inspecting
                # their mutable output, and never page beyond this bounded GET.
                candidates = []
                seen: set[int] = set()
                source_app_id = getattr(self.ledger, "authenticated_app_id", None)
                for run in runs:
                    if not isinstance(run, dict):
                        return None
                    run_id, name, head = (
                        run.get("id"),
                        run.get("name"),
                        run.get("head_sha"),
                    )
                    if (
                        type(run_id) is not int
                        or run_id <= 0
                        or run_id in seen
                        or not isinstance(name, str)
                        or not name
                        or not isinstance(head, str)
                        or _HEX40_RE.fullmatch(head) is None
                    ):
                        return None
                    seen.add(run_id)
                    if name != request["name"] or head != request["head_sha"]:
                        continue
                    app = run.get("app")
                    if (
                        not isinstance(app, dict)
                        or type(app.get("id")) is not int
                        or app["id"] <= 0
                        or not isinstance(app.get("slug"), str)
                        or not app["slug"]
                    ):
                        return None
                    if source_app_id is not None and (app["id"] == source_app_id) != (
                        app["slug"] == expected_slug
                    ):
                        return None
                    if app["slug"] != expected_slug:
                        continue
                    observed_external_id = run.get("external_id")
                    if (
                        not isinstance(observed_external_id, str)
                        or not observed_external_id
                    ):
                        return None
                    if observed_external_id == external_id:
                        candidates.append(run)
                if len(candidates) == 1 and not _check_request_matches(
                    candidates[0], request, recovering=True
                ):
                    return None
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
                matches = _request_matches
                if "/check-runs" in path:
                    matches = _check_request_matches
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
                            and self._check_update_matches(later, value, recovered=True)
                        ):
                            # A response-lost final update may supersede this
                            # earlier confirmed creation. The same fresh GET
                            # proves its exact source-bound object/request; do
                            # not infer success from a generic failure state.
                            expected_request = {**expected_request, **later["request"]}
                            confirmed_check_successor = later
                candidates = [value] if matches(value, expected_request) else []
        if candidates is None or len(candidates) != 1:
            return None
        value = candidates[0]
        if "check_update_precondition" in effect and not self._check_update_matches(
            effect, value, recovered=response is None
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
            commitment = self._check_commitment(value)
            if (
                commitment is None
                or "user" in value
                or value["app"]["slug"] != expected_slug
                or value.get("errors") not in (None, [])
                or (
                    effect["method"] == "PATCH"
                    and path
                    != f"/repos/{self.identity.repository}/check-runs/{commitment['check_run_id']}"
                )
            ):
                return None
            if response is not None and isinstance(response[1], dict):
                old_app_id = response[1].get("app_id")
                if old_app_id is not None and commitment["app_id"] != old_app_id:
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
            and (not isinstance(user, dict) or user.get("id") != expected_producer)
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
            # Match compact-receipt identity bounds before the native value
            # can become a follow-up request path, not only after readback.
            if type(rid) is not int or not 0 < rid <= 9_223_372_036_854_775_807:
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
                        if assembled.offloaded_body_instances and self.app_slug:
                            try:
                                companions = (
                                    self.reviewer._verified_companion_prose_bodies(
                                        token=token,
                                        repository=self.identity.repository,
                                        repository_id=self.identity.repository_id,
                                        pull_request=self.identity.pull_request,
                                        head_sha=self.head_sha,
                                        app_slug=self.app_slug,
                                        main_body=body,
                                        producer_id=producer_id,
                                    )
                                )
                            except (
                                GitHubHTTPError,
                                GitHubPublicationError,
                                ReviewInputError,
                            ):
                                companions = {}
                            for instance, item in explanations.items():
                                if (
                                    instance in indexed
                                    and instance not in receipts
                                    and instance in assembled.offloaded_body_instances
                                    and (companion := companions.get(instance))
                                    is not None
                                    and render_finding(item.view) in companion[2]
                                ):
                                    receipts[instance] = NativeFindingReceipt(
                                        self.identity.repository_id,
                                        self.identity.pull_request,
                                        self.head_sha,
                                        self.base_sha,
                                        producer_id,
                                        companion[0],
                                        instance,
                                        companion[1],
                                    )
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
            or comment.get("edited_by_human") is True
            or comment.get("pullRequestReview") != {"databaseId": receipt["review_id"]}
            or comment.get("commit") != {"oid": receipt["native_head_sha"]}
            or not isinstance(author, dict)
            or author.get("databaseId") != receipt["producer_id"]
            or author.get("__typename") != "Bot"
            or not graphql_app_author(author, self.app_slug)
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


from review_sensei.publication_intent_fusion import (  # noqa: E402
    install_journal as _install_fusion_checkpoint_source,
)

_install_fusion_checkpoint_source(FindingLifecycleJournal)
del _install_fusion_checkpoint_source


from .materialized_publication_phase import (  # noqa: E402
    install_journal as _install_materialized_phase,
)

_install_materialized_phase(FindingLifecycleJournal)
del _install_materialized_phase


from .empty_original_admission import install_restore as _install_empty_restore  # noqa: E402

restore_finding_lifecycle = _install_empty_restore(restore_finding_lifecycle)
del _install_empty_restore


from .ordinary_drift_execution import _install_checkpoint_source  # noqa: E402

_install_checkpoint_source(FindingLifecycleJournal)
del _install_checkpoint_source


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
