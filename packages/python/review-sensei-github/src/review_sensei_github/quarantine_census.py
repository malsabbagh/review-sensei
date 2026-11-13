"""One closed, read-only cursor census for authenticated quarantine ledgers.

Full comment bodies retain the existing HTTP byte cap. Cursors allow a heavy
historical prefix and a lighter tail to use different page sizes without gaps.
No response, ownership or authority is cached; only safe page-size hints are.
Schema: https://docs.github.com/en/graphql/reference/issues#issuecomment
Paging: https://docs.github.com/en/graphql/guides/using-pagination-in-the-graphql-api
"""

from __future__ import annotations

import json
import re
import time
import weakref
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from review_sensei.errors import ReviewInputError

from .errors import GitHubHTTPResponseTooLargeError
from .http import (
    MAX_GITHUB_RESPONSE_BYTES,
    MAX_PAGINATION_ITEMS,
    MAX_PAGINATION_PAGES,
    _read_allowance,
)

QUERY = """query QuarantineCommentCensus($owner:String!,$name:String!,$number:Int!,$first:Int!,$after:String){
 repository(owner:$owner,name:$name){databaseId nameWithOwner pullRequest(number:$number){number
 comments(first:$first,after:$after){totalCount pageInfo{hasNextPage endCursor} nodes{
 fullDatabaseId body createdAt updatedAt author{__typename login ... on Bot{databaseId} ... on User{databaseId}}
 pullRequest{number repository{databaseId nameWithOwner}}
 }}}}}
"""


def _cursor(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 2048
        and all(33 <= ord(c) <= 126 for c in value)
    )


def _variables(value):
    return (
        isinstance(value, dict)
        and set(value) == {"owner", "name", "number", "first", "after"}
        and all(
            isinstance(value[key], str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", value[key])
            for key in ("owner", "name")
        )
        and type(value["number"]) is int
        and 1 <= value["number"] <= 2_147_483_647
        and type(value["first"]) is int
        and 1 <= value["first"] <= 100
        and (value["after"] is None or _cursor(value["after"]))
    )


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate census JSON key")
        result[key] = value
    return result


def is_readonly_census(request, api_url):
    """Classify only closed source reads, never arbitrary GraphQL POSTs."""
    if not isinstance(api_url, str) or request.get_method() != "POST":
        return False
    endpoint, expected = (
        urlsplit(request.full_url),
        urlsplit(api_url.rstrip("/") + "/graphql"),
    )
    if (
        endpoint != expected
        or not isinstance(request.data, bytes)
        or len(request.data) > 8192
    ):
        return False
    try:
        value = json.loads(request.data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(value, dict) or not isinstance(value.get("query"), str):
        return False
    if value["query"] == QUERY:
        return set(value) == {"query", "variables"} and _variables(value["variables"])
    from .packed_comment_census import (
        CLOSING_QUERY,
        METADATA_QUERY,
        is_closing_query,
        is_metadata_query,
    )

    if value["query"] == METADATA_QUERY:
        return (
            set(value)
            in ({"query", "variables"}, {"query", "variables", "operationName"})
            and value.get("operationName", "PackedCommentMembership")
            == "PackedCommentMembership"
            and is_metadata_query(value["query"], value.get("variables"))
        )
    if value["query"] == CLOSING_QUERY:
        return (
            set(value)
            in ({"query", "variables"}, {"query", "variables", "operationName"})
            and value.get("operationName", "PackedCommentClosing")
            == "PackedCommentClosing"
            and is_closing_query(value["query"], value.get("variables"))
        )
    from .native_review_groups import PART_QUERY

    if value["query"] == PART_QUERY:
        variables = value.get("variables")
        return (
            set(value)
            in ({"query", "variables"}, {"query", "variables", "operationName"})
            and value.get("operationName", "NativeReviewPart") == "NativeReviewPart"
            and isinstance(variables, dict)
            and set(variables) == {"id"}
            and _cursor(variables["id"])
        )
    from .native_review_census import (
        CLOSE_MIDDLE_QUERY,
        CLOSE_QUERY,
        MIDDLE_QUERY,
        OLD_QUERY,
        OPEN_QUERY,
        PART_MIDDLE_QUERY,
        PART_OLD_QUERY,
        PART_ROOTS_QUERY,
        ROOT_ID_QUERIES,
        ROOT_IDS_FORWARD_QUERY,
        ROOT_IDS_QUERY,
        ROOT_OPEN_QUERY,
        source_query_variables,
    )

    if value["query"] in {*ROOT_ID_QUERIES, ROOT_IDS_QUERY, ROOT_IDS_FORWARD_QUERY}:
        operation = value["query"].partition("(")[0].removeprefix("query ").strip()
        return (
            set(value)
            in ({"query", "variables"}, {"query", "variables", "operationName"})
            and value.get("operationName", operation) == operation
            and source_query_variables(value["query"], value.get("variables"))
        )
    operation = {
        OPEN_QUERY: "NativePublicationOpening",
        OLD_QUERY: "NativeExistingRoots",
        PART_OLD_QUERY: "NativeReviewPartWithOldRoots",
        ROOT_OPEN_QUERY: "NativeRootOpening",
        PART_ROOTS_QUERY: "NativeReviewPartWithOpening",
        MIDDLE_QUERY: "NativePublicationMiddle",
        PART_MIDDLE_QUERY: "NativeReviewPartWithMiddle",
        CLOSE_QUERY: "NativePublicationClosing",
        CLOSE_MIDDLE_QUERY: "NativePublicationClosingWithMiddle",
    }.get(value["query"])
    if operation is not None:
        return (
            set(value)
            in ({"query", "variables"}, {"query", "variables", "operationName"})
            and value.get("operationName", operation) == operation
            and source_query_variables(value["query"], value.get("variables"))
        )
    # Existing authority/suppression/cleanup readers use these exact immutable
    # query texts. POST is their transport, not a native mutation. The same
    # closed variables and endpoint/byte checks still apply; mutations and
    # unknown query texts retain the ordinary mutation accounting.
    from .publication import _REVIEW_THREADS_QUERY
    from .thread_resolution import _QUERY

    operation = {
        _REVIEW_THREADS_QUERY: "ReviewThreads",
        _QUERY: "FindingResolutionThreads",
    }.get(value["query"])
    return (
        operation is not None
        and set(value)
        in ({"query", "variables"}, {"query", "variables", "operationName"})
        and value.get("operationName", operation) == operation
        and _variables(value["variables"])
    )


def _numeric(value):
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,15}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 9_007_199_254_740_991:
        raise ReviewInputError("quarantine census numeric comment identity is invalid")
    return value


def _repository(value, identity):
    if (
        not isinstance(value, dict)
        or type(value.get("databaseId")) is not int
        or value["databaseId"] != identity.repository_id
        or value.get("nameWithOwner") != identity.repository
    ):
        raise ReviewInputError("quarantine census repository identity conflicts")


def _comment(node, ledger, identity):
    if not isinstance(node, dict) or set(node) != {
        "fullDatabaseId",
        "body",
        "createdAt",
        "updatedAt",
        "author",
        "pullRequest",
    }:
        raise ReviewInputError("quarantine census comment is incomplete")
    identifier = _numeric(node["fullDatabaseId"])
    subject = node["pullRequest"]
    if (
        not isinstance(subject, dict)
        or set(subject) != {"number", "repository"}
        or type(subject["number"]) is not int
        or subject["number"] != identity.pull_request
    ):
        raise ReviewInputError(
            "quarantine census comment is associated with another pull request"
        )
    _repository(subject["repository"], identity)
    if not isinstance(node["body"], str) or any(
        not isinstance(node[key], str) or not 1 <= len(node[key]) <= 64
        for key in ("createdAt", "updatedAt")
    ):
        raise ReviewInputError("quarantine census comment body or dates are invalid")
    author = node["author"]
    user = None
    if author is not None:
        if (
            not isinstance(author, dict)
            or not isinstance(author.get("__typename"), str)
            or not isinstance(author.get("login"), str)
        ):
            raise ReviewInputError("quarantine census author is incomplete")
        kind, login = author["__typename"], author["login"]
        numeric = author.get("databaseId")
        if kind in {"Bot", "User"}:
            numeric = _numeric(numeric)
        slug = ledger.app_slug
        if kind == "Bot" and isinstance(slug, str):
            aliases = {slug.casefold()}
            if slug.casefold().endswith("[bot]"):
                aliases.add(slug[:-5].casefold())
            if login.casefold() in aliases:
                # Representation only. All native ownership checks still
                # require the independently authenticated numeric Bot ID.
                login = slug
        user = {"type": kind, "login": login, "id": numeric}
    return {
        "id": identifier,
        "body": node["body"],
        "user": user,
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "issue_url": ledger.http.api_url.rstrip("/")
        + ledger.http.repository_path(
            identity.repository, f"/issues/{identity.pull_request}"
        ),
    }


def _page_hint(ledger, seen_ids):
    # Performance hint only: even known bodies are fetched and authenticated
    # again. Estimate the next page from previously observed unseen order.
    # New/changed bodies can still exceed the estimate and consume a failed
    # bounded attempt; they never receive an exemption from the HTTP cap.
    costs = list(
        (
            len(json.dumps(item.get("body", "")).encode()) + 1024
            for item in ledger._cached_comments()
            if isinstance(item, dict)
            and type(item.get("id")) is int
            and item["id"] not in seen_ids
        ),
    )
    size, amount = 0, 4096
    for cost in costs[:100]:
        if amount + cost > MAX_GITHUB_RESPONSE_BYTES:
            break
        size += 1
        amount += cost
    return max(1, size) if costs else 100


def fetch_comments(
    ledger,
    identity,
    *,
    max_requests=None,
    timeout_seconds=None,
    before_read=None,
    observe_request=None,
):
    """Complete bounded census; every failed adaptive attempt spends a call."""
    from .publication_recovery import _publication_transport

    lifecycle = getattr(ledger, "_quarantine_lifecycle_capacity", None)
    if lifecycle is not None:
        from .packed_comment_census import fetch_packed_comments

        if type(lifecycle) is not _LifecycleCapacity:
            raise ReviewInputError("quarantine lifecycle census scope is unissued")
        plan = _LifecycleCapacity.require(lifecycle, ledger, identity)
        return fetch_packed_comments(
            ledger,
            identity,
            plan=plan,
            max_requests=max_requests,
            timeout_seconds=timeout_seconds,
            before_read=before_read,
            observe_request=observe_request,
        )

    guard = getattr(
        _publication_transport(ledger.http), "_quarantine_capacity_guard", None
    )
    if guard is not None and guard.comment_plan is not None:
        from .packed_comment_census import fetch_packed_comments

        if guard.identity != identity:
            raise ReviewInputError("packed comment capacity scope changed")
        return fetch_packed_comments(
            ledger,
            identity,
            plan=guard.comment_plan,
            max_requests=max_requests,
            timeout_seconds=timeout_seconds,
            before_read=before_read,
            observe_request=observe_request,
        )
    owner, name = identity.repository.split("/", 1)
    profiles = getattr(ledger, "_quarantine_census_sizes", None)
    if profiles is None:
        profiles = {}
        ledger._quarantine_census_sizes = profiles
    collected = []
    seen_ids: set[int] = set()
    cursors: set[str] = set()
    after, total = None, None
    certified = getattr(ledger, "_quarantine_capacity_cursor", False)
    first = 100 if certified else _page_hint(ledger, seen_ids)
    requests = (
        MAX_PAGINATION_PAGES
        if max_requests is None
        else min(max_requests, MAX_PAGINATION_PAGES)
    )
    if type(requests) is not int or not 1 <= requests <= MAX_PAGINATION_PAGES:
        raise ReviewInputError("quarantine census request bound is invalid")
    deadline = time.monotonic() + (
        60 if timeout_seconds is None else min(timeout_seconds, 60)
    )
    for _attempt in range(requests):
        if not certified:
            first = min(first, profiles.get(after, 100))
        variables = {
            "owner": owner,
            "name": name,
            "number": identity.pull_request,
            "first": first,
            "after": after,
        }
        if not _variables(variables):
            raise ReviewInputError("quarantine census variables are invalid")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewInputError("quarantine census deadline exceeded")
        if before_read is not None:
            remaining = min(remaining, _read_allowance(before_read))
        if observe_request is not None:
            observe_request()
        try:
            status, response = ledger.http.request(
                "POST",
                "/graphql",
                token=ledger.token,
                body={"query": QUERY, "variables": variables},
                timeout_seconds=remaining,
            )
        except GitHubHTTPResponseTooLargeError as error:
            if first <= 1:
                raise ReviewInputError(
                    "quarantine census single comment exceeds response bound"
                ) from None
            completed = error.completed_json_objects
            # This fixed query has four nested object closures per ordinary
            # comment. A conservative completed-prefix count avoids repeated
            # oversized probes. No partial body or identity is admitted.
            nodes = getattr(error, "completed_comment_nodes", 0)
            hinted = (
                nodes
                if certified and type(nodes) is int and nodes > 0
                else (
                    max(1, (completed - 1) // 4 - 1)
                    if type(completed) is int and completed >= 5
                    else max(1, first // 2)
                )
            )
            first = min(first - 1, hinted)
            profiles[after] = first
            continue
        if (
            time.monotonic() >= deadline
            or status != 200
            or not isinstance(response, dict)
            or response.get("errors")
            or set(response) - {"data", "errors", "extensions"}
        ):
            raise ReviewInputError("quarantine complete census is unavailable")
        data = response.get("data")
        repository = (
            data.get("repository")
            if isinstance(data, dict) and set(data) == {"repository"}
            else None
        )
        _repository(repository, identity)
        if not isinstance(repository, dict):
            raise ReviewInputError("quarantine census repository is absent")
        subject = repository.get("pullRequest")
        if (
            not isinstance(subject, dict)
            or type(subject.get("number")) is not int
            or subject["number"] != identity.pull_request
        ):
            raise ReviewInputError("quarantine census pull request identity conflicts")
        connection = subject.get("comments")
        if not isinstance(connection, dict) or set(connection) != {
            "totalCount",
            "pageInfo",
            "nodes",
        }:
            raise ReviewInputError("quarantine census connection is incomplete")
        count, info, nodes = (
            connection["totalCount"],
            connection["pageInfo"],
            connection["nodes"],
        )
        if (
            type(count) is not int
            or not 0 <= count <= MAX_PAGINATION_ITEMS
            or total is not None
            and count != total
            or not isinstance(nodes, list)
            or len(nodes) > first
            or not isinstance(info, dict)
            or set(info) != {"hasNextPage", "endCursor"}
            or type(info["hasNextPage"]) is not bool
        ):
            raise ReviewInputError(
                "quarantine census pagination is incomplete or changed"
            )
        total = count
        if certified:
            # The cursor certificate reserves this exact closed response
            # envelope independently of per-node body/metadata bounds.
            def wire(value):
                return len(
                    json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
                )

            if wire(response) - sum(wire(node) for node in nodes) > 4096:
                raise ReviewInputError(
                    "quarantine census response envelope exceeded its bound"
                )
        for node in nodes:
            item = _comment(node, ledger, identity)
            if item["id"] in seen_ids:
                raise ReviewInputError("quarantine census comment is duplicated")
            seen_ids.add(item["id"])
            collected.append(item)
        if len(collected) > count:
            raise ReviewInputError("quarantine census item count conflicts")
        cursor = info["endCursor"]
        if not info["hasNextPage"]:
            if (
                (nodes and cursor in cursors)
                or len(collected) != count
                or (nodes and not _cursor(cursor))
                or (not nodes and cursor is not None)
            ):
                raise ReviewInputError("quarantine census terminal page is incomplete")
            return collected
        if (
            len(nodes) != first
            or not _cursor(cursor)
            or cursor in cursors
            or len(collected) >= count
        ):
            raise ReviewInputError("quarantine census cursor is missing or repeated")
        cursors.add(cursor)
        after, first = cursor, 100 if certified else _page_hint(ledger, seen_ids)
    raise ReviewInputError(
        "quarantine complete census exceeded its physical request bound"
    )


# This scope refines enumeration capacity only. It does not authorize a native
# mutation, settle an old writer, or replace the checkpoint's fresh root fences.
_LIFECYCLE_ISSUED: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_LIFECYCLE_ACTIVE: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


@dataclass(frozen=True, eq=False)
class _LifecycleCapacity:
    ledger: Any
    caller: Any
    identity: Any
    record: Any
    root: str
    record_bytes: bytes
    profile: bytes
    plan: Any

    def require(self, ledger, identity):
        from review_sensei.bounded_evidence import canonical_bytes

        issued = _LIFECYCLE_ACTIVE.get(self)
        if (
            issued is None
            or issued[0] != _LifecycleCapacity._seal(self)
            or ledger is not self.ledger
            or ledger.caller is not self.caller
            or identity != self.identity
            or self.caller._root != self.root
            or self.caller._effect_record is not self.record
            or canonical_bytes(self.record.to_dict()) != self.record_bytes
            or canonical_bytes(_lifecycle_profile(ledger)) != self.profile
        ):
            raise ReviewInputError("quarantine lifecycle census scope changed")
        operation = self.caller._checkpoint_operation
        if operation is None or self.caller._active_operation is not operation:
            raise ReviewInputError(
                "quarantine lifecycle census is outside its checkpoint"
            )
        if issued[1] is None:
            _LIFECYCLE_ACTIVE[self] = (issued[0], operation)
        elif operation is not issued[1]:
            raise ReviewInputError("quarantine lifecycle census operation changed")
        self.caller._require_ledger(ledger, identity)
        self.caller.budget_view.check()
        return self.plan

    def _seal(self):
        return (
            id(self.ledger),
            id(self.caller),
            self.identity,
            id(self.record),
            self.root,
            self.record_bytes,
            self.profile,
            id(self.plan),
            tuple(sorted(self.plan.known.items())),
            self.plan.unknown,
            tuple(sorted(self.plan.metadata_known.items())),
            self.plan.metadata_unknown,
        )


def _lifecycle_profile(ledger):
    return {
        "tail": ledger._quarantine_native_comment_bounds,
        "tail_parts": ledger._quarantine_native_tail_parts,
        "producer": ledger.evidence_producer(),
    }


def lifecycle_capacity_plan(ledger, identity, *, added_parts):
    """Pure full-frontier capacity from the already authenticated two-root scope."""
    from review_sensei.bounded_evidence import MAX_PART_BYTES, canonical_bytes

    from .api_run_requests import ApiRunSessionLedger
    from .packed_comment_census import CommentAllocationPlan, wire
    from .quarantine_capacity import full_review_comment_tail, native_reserve
    from .quarantine_origin import (
        _AUTHENTICATED,
        AuthenticatedQuarantine,
        read_predecessor,
    )
    from .review_run import ActionRunLedger, ReviewRunLedger
    from .session_ledger import parse_session_comment, plain_session_comment_json_bound

    if (
        type(ledger) not in {ApiRunSessionLedger, ActionRunLedger, ReviewRunLedger}
        or type(added_parts) is not int
        or not 0 < added_parts <= 256
    ):
        raise ReviewInputError("quarantine lifecycle capacity source is unsupported")
    caller = ledger.caller
    caller._require_ledger(ledger, identity)
    record = caller._effect_record
    authenticated = ledger._quarantine_authenticated
    if (
        not ledger._quarantine_required
        or not ledger._inventory_complete
        or record is None
        or record.record_sha256 != caller._root
        or type(authenticated) is not AuthenticatedQuarantine
        or authenticated not in _AUTHENTICATED
        or authenticated.original_caller is not caller
        or authenticated.original_identity != identity
        or canonical_bytes(record.quarantine_lineage) != authenticated.lineage_bytes
    ):
        raise ReviewInputError(
            "quarantine lifecycle capacity lacks authenticated current lineage"
        )
    receipt = None
    if authenticated.record_sha256 != record.record_sha256:
        from .quarantine_checkpoint import confirmed_checkpoint_capacity

        receipt = confirmed_checkpoint_capacity(caller, ledger, identity)
    predecessor = read_predecessor(authenticated.archive)
    lineage = authenticated.lineage
    expected = {lineage["predecessor_comment_id"]: predecessor.record_sha256}
    roots, known = {}, {}
    replaced = False
    for row in ledger._cached_comments():
        identifier, body = row.get("id"), row.get("body")
        if (
            type(identifier) is not int
            or identifier <= 0
            or identifier in known
            or not isinstance(body, str)
        ):
            raise ReviewInputError(
                "quarantine lifecycle capacity frontier is incomplete"
            )
        if receipt is not None and identifier == receipt.comment_id:
            previous = parse_session_comment(body, identity=identity)
            if (
                previous is None
                or previous.record_sha256 != authenticated.record_sha256
                or canonical_bytes(previous.quarantine_lineage)
                != authenticated.lineage_bytes
            ):
                raise ReviewInputError(
                    "quarantine lifecycle receipt predecessor changed"
                )
            ledger._part_owner(row, identity)
            body = receipt.body
            replaced = True
        known[identifier] = wire({"body": body})
        from .native_operation_envelope import OPERATION_WIRE_BOUND
        from .operation_host import OPERATION_COMMENT_MARKER

        if body.startswith(OPERATION_COMMENT_MARKER + "\n"):
            known[identifier] = max(known[identifier], OPERATION_WIRE_BOUND)
        parsed = parse_session_comment(body, identity=identity)
        if parsed is not None:
            ledger._part_owner(row, identity)
            roots[identifier] = parsed.record_sha256
            if parsed.record_sha256 == record.record_sha256:
                if canonical_bytes(parsed.to_dict()) != canonical_bytes(
                    record.to_dict()
                ):
                    raise ReviewInputError(
                        "quarantine lifecycle selected record changed"
                    )
                if identifier in expected:
                    raise ReviewInputError(
                        "quarantine lifecycle root collides with predecessor"
                    )
                expected[identifier] = record.record_sha256
            # Preserve both roots' maximum legal renderer width. The old root
            # can never be silently removed from the capacity calculation.
            known[identifier] = max(
                known[identifier],
                9 + plain_session_comment_json_bound(),
            )
    if (
        (receipt is not None and not replaced)
        or len(expected) != 2
        or roots != expected
        or predecessor.record_sha256 != lineage["predecessor_record_sha256"]
    ):
        raise ReviewInputError("quarantine lifecycle predecessor/current roots changed")
    tail = full_review_comment_tail()
    if ledger._quarantine_native_comment_bounds not in (None, tail):
        raise ReviewInputError(
            "quarantine lifecycle original tail profile is unsupported"
        )
    isolation = authenticated.archive["target_isolation"]
    from .publication_recovery import _publication_transport

    guard = getattr(
        _publication_transport(ledger.http), "_quarantine_capacity_guard", None
    )
    if (
        isolation.get("schema_version") != "quarantine-target-isolation-v1"
        or native_reserve(isolation) is not None
        or guard is not None
    ):
        # Published-tail and body-limit guards include mutable object maxima,
        # closure widths and receipt updates beyond this legacy capacity-only
        # contract. Never replace or reinterpret their source-issued plan.
        if guard is not None:
            from .quarantine_capacity import NativeCapacityGuard

            if type(guard) is not NativeCapacityGuard:
                raise ReviewInputError(
                    "quarantine lifecycle capacity guard source changed"
                )
            NativeCapacityGuard.require(guard, guard.stats)
        raise ReviewInputError(
            "quarantine lifecycle guarded capacity requires its original plan"
        )
    # Preserve the original per-obligation event/archive and ordinary
    # verification storage promises, independently of this review's tail.
    promised_parts = 2 * len(authenticated.pending) + 5
    plan = CommentAllocationPlan(
        known,
        (*tail, *((MAX_PART_BYTES + 512,) * (added_parts + promised_parts))),
    )
    if plan.worst_read_calls() > MAX_PAGINATION_PAGES:
        raise ReviewInputError(
            "quarantine lifecycle complete census exceeds original request bound"
        )
    scope = _LifecycleCapacity(
        ledger,
        caller,
        identity,
        record,
        record.record_sha256,
        canonical_bytes(record.to_dict()),
        canonical_bytes(_lifecycle_profile(ledger)),
        plan,
    )
    _LIFECYCLE_ISSUED[scope] = _LifecycleCapacity._seal(scope)
    return scope


def activate_lifecycle_capacity(scope):
    from contextlib import contextmanager

    @contextmanager
    def active():
        if type(scope) is not _LifecycleCapacity or _LIFECYCLE_ISSUED.pop(
            scope, None
        ) != _LifecycleCapacity._seal(scope):
            raise ReviewInputError(
                "quarantine lifecycle capacity was not source-issued"
            )
        ledger = scope.ledger
        if getattr(ledger, "_quarantine_lifecycle_capacity", None) is not None:
            raise ReviewInputError("quarantine lifecycle census cannot nest")
        _LIFECYCLE_ACTIVE[scope] = (_LifecycleCapacity._seal(scope), None)
        ledger._quarantine_lifecycle_capacity = scope
        try:
            yield
        finally:
            ledger._quarantine_lifecycle_capacity = None
            _LIFECYCLE_ACTIVE.pop(scope, None)

    return active()
