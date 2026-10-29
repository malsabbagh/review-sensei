"""Permanent per-surface uncertainty reserves on one bounded native transport.

The current complete census and every subsequent physical mutation share the
caller's existing HTTP allowance. Known writes consume native room; elapsed
time, absent objects and old materialization never release the old reserve.
This guard grants no write authority and never retries a consumed request.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from review_sensei.bounded_evidence import MAX_PART_BYTES
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import pending_assessments

from .http import MAX_GITHUB_RESPONSE_BYTES, MAX_PAGINATION_ITEMS, MAX_PAGINATION_PAGES
from .quarantine_scan_capacity import cold_cursor_requests, cold_rest_requests

_SURFACES = (
    "issue_comments",
    "inline_replies",
    "reviews",
    "checks",
    "threads",
    "reactions",
)
_RESPONSE_METADATA_BYTES = 4096
_PAGE_BYTES = MAX_GITHUB_RESPONSE_BYTES - _RESPONSE_METADATA_BYTES


def _wire(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


def full_review_comment_tail():
    """Closed lower component of the unknown full-review transaction.

    One original accepted operation retains the full result. Closed checkpoint
    and admitted-inventory projections preserve every field, original admission
    flag and instance without duplicating prose. Their maxima are reserved
    before startup; no later full-copy fallback is admitted.
    """
    from .native_manifest import MANIFEST_WIRE_BOUND
    from .native_operation_envelope import (
        OPERATION_WIRE_BOUND,
        operation_envelope_certificate,
    )
    from .operation_comments import _MAX_RESULT_BYTES, result_part_bodies
    from .retention_projection import (
        MAX_CHECKPOINT_PROJECTION_BYTES,
        MAX_INVENTORY_PROJECTION_BYTES,
    )

    operation_envelope_certificate()
    result = tuple(
        _wire({"body": body})
        for body in result_part_bodies(
            "a" * 64 + "\t" + "b" * 64, b"x" * _MAX_RESULT_BYTES
        )
    )

    def projection(maximum):
        return tuple(
            _wire({"body": body})
            for body in result_part_bodies("a" * 64 + "\t" + "b" * 64, b"x" * maximum)
        )

    # Startup archive, operation metadata, fixed-binding inventory intent,
    # operation fallback and both independently authenticated projections.
    return (
        MAX_PART_BYTES + _RESPONSE_METADATA_BYTES,
        OPERATION_WIRE_BOUND,
        8192,
        MANIFEST_WIRE_BOUND,
        *result,
        *projection(MAX_CHECKPOINT_PROJECTION_BYTES),
        *projection(MAX_INVENTORY_PROJECTION_BYTES),
    )


def native_reserve(receipt):
    receipt = receipt.get("execution_isolation", receipt)
    if receipt.get("schema_version") not in {
        "quarantine-published-tail-isolation-v1",
        "quarantine-body-limit-isolation-v1",
    }:
        return None
    surfaces = deepcopy(receipt["qualification"]["exposure"]["surfaces"])
    # Evidence and ordinary operation comments are disjoint allocation classes
    # but share the same native issue-comment census. Count each exactly once.
    parts = surfaces.pop("evidence_parts")
    for key in parts:
        if key == "max_object_wire_bytes":
            surfaces["issue_comments"][key] = max(
                surfaces["issue_comments"][key], parts[key]
            )
        else:
            surfaces["issue_comments"][key] += parts[key]
    return surfaces


class NativeCapacityGuard:
    def __init__(
        self,
        *,
        identity,
        reserve,
        future_parts=0,
        future_bytes=0,
        comment_bounds=None,
        cursor=False,
        closure_wire=0,
        reply=False,
        complete_tail=False,
        comment_plan=None,
    ):
        self.identity, self.reserve = identity, deepcopy(reserve)
        self.future_parts, self.future_bytes = future_parts, future_bytes
        self.comment_reader: Any = None
        self.cursor = cursor
        self.closure_wire = closure_wire
        self.complete_tail = complete_tail
        self.comment_plan = comment_plan
        self.comment_read_calls = (
            comment_plan.worst_read_calls() if comment_plan is not None else None
        )
        self.issue_update_bytes = 2 * closure_wire
        self.comment_bounds = (
            list(comment_bounds) if comment_bounds is not None else None
        )
        self.review_bounds = [closure_wire] * 2 if cursor else []
        self.check_bounds = [closure_wire] * 2 if cursor else []
        self.inline_bounds = [6 * 16_384 + 1024] if cursor and reply else []
        self.reaction_bounds = [4096] if cursor else []
        self._publication_prepared = None
        self.stats = {
            name: {"count": 0, "bytes": 0, "largest": 0} for name in _SURFACES
        }
        self.objects = {}
        self.certificates = {}
        self._pending_create = None
        self.require(self.stats)

    def admit_comment_batch(self, bodies):
        """Prepare the complete immutable batch before its first POST.

        Existing tail promises remain reserved. Only uncovered prepared bodies
        add promises; successful/unknown dispatches consume them exactly once.
        """
        if self.comment_bounds is None:
            return
        available, extra = list(self.comment_bounds), []
        for body in bodies:
            size = _wire({"body": body})
            choices = [bound for bound in available if bound >= size]
            if choices:
                available.remove(min(choices))
            else:
                extra.append(size)
        if not extra:
            return
        if self.complete_tail:
            raise ReviewInputError(
                "quarantine prepared batch exceeds the admitted complete transaction"
            )
        old = self.comment_bounds, self.future_parts, self.future_bytes
        self.comment_bounds = [*self.comment_bounds, *extra]
        self.future_parts = len(self.comment_bounds)
        self.future_bytes = sum(self.comment_bounds)
        try:
            self.require(self.stats)
        except BaseException:
            self.comment_bounds, self.future_parts, self.future_bytes = old
            raise

    def admit_publication(self, *, review_wire, inline_wires):
        """Reserve the prepared publication, approval, checks and root close."""
        if not self.cursor:
            return
        requested = (review_wire, tuple(inline_wires))
        if self._publication_prepared is not None:
            # Read-only reconciliation can prepare the same frame again.
            # Dispatch, including unknown dispatch, already consumed its slot.
            if requested != self._publication_prepared:
                raise ReviewInputError("quarantine prepared publication changed")
            return
        if self.complete_tail and (
            review_wire > max(self.review_bounds, default=0)
            or len(inline_wires) > len(self.inline_bounds)
            or any(
                size > bound
                for size, bound in zip(sorted(inline_wires), sorted(self.inline_bounds))
            )
        ):
            raise ReviewInputError(
                "quarantine publication exceeds the admitted complete transaction"
            )
        old = self.review_bounds, self.inline_bounds
        self.review_bounds = [max(self.closure_wire, review_wire), self.closure_wire]
        self.inline_bounds = list(inline_wires)
        try:
            self.require(self.stats)
        except BaseException:
            self.review_bounds, self.inline_bounds = old
            raise
        self._publication_prepared = requested

    def require(self, stats):
        for surface, current in stats.items():
            old = self.reserve[surface]
            if surface == "checks":
                # Old checks carry distinct old heads and never enter the
                # current check-name reader. Its actual envelope is one page.
                from .checks import MAX_CHECK_PAGE_SIZE

                if (
                    current["count"] > MAX_CHECK_PAGE_SIZE
                    or current["bytes"] > _PAGE_BYTES
                    or current["largest"] > _PAGE_BYTES
                ):
                    raise ReviewInputError(
                        "quarantine current-check response capacity exceeded"
                    )
            count = current["count"] + old["new_objects"]
            size = (
                current["bytes"] + old["new_wire_bytes"] + old["positive_update_bytes"]
            )
            if surface == "issue_comments":
                count += self.future_parts
                size += self.future_bytes + self.future_parts * _RESPONSE_METADATA_BYTES
                # One session root and one operation metadata root can grow.
                # Their full final wire bounds remain protected until closure.
                size += self.issue_update_bytes
            tail = (
                self.review_bounds
                if surface == "reviews"
                else self.check_bounds
                if surface == "checks"
                else self.inline_bounds
                if surface in {"inline_replies", "threads"}
                else self.reaction_bounds
                if surface == "reactions"
                else []
            )
            count += len(tail)
            size += sum(tail) + len(tail) * _RESPONSE_METADATA_BYTES
            # Native response metadata is additional to emitted mutation bytes.
            size += old["new_objects"] * _RESPONSE_METADATA_BYTES
            largest = max(
                current["largest"],
                old["max_object_wire_bytes"] + _RESPONSE_METADATA_BYTES,
                max(tail, default=0) + _RESPONSE_METADATA_BYTES,
                self.closure_wire + _RESPONSE_METADATA_BYTES
                if surface == "issue_comments"
                else 0,
            )
            if surface == "issue_comments" and self.future_parts:
                largest = max(
                    largest,
                    max(self.comment_bounds, default=MAX_PART_BYTES)
                    + _RESPONSE_METADATA_BYTES
                    if self.comment_bounds is not None
                    else MAX_PART_BYTES + _RESPONSE_METADATA_BYTES,
                )
            if count > MAX_PAGINATION_ITEMS or largest > _PAGE_BYTES:
                raise ReviewInputError(
                    f"quarantine {surface} item/response capacity exceeded"
                )
            if size > MAX_PAGINATION_PAGES * _PAGE_BYTES:
                raise ReviewInputError(
                    f"quarantine {surface} encoded-byte capacity exceeded"
                )
            if surface == "checks":
                if (
                    current["count"] + len(tail) > 100
                    or current["bytes"]
                    + sum(tail)
                    + len(tail) * _RESPONSE_METADATA_BYTES
                    + 2 * self.closure_wire
                    > _PAGE_BYTES
                ):
                    raise ReviewInputError(
                        "quarantine current-check transaction capacity exceeded"
                    )
                continue
            if surface == "issue_comments" and self.comment_plan is not None:
                if count > len(self.comment_plan.known) + len(
                    self.comment_plan.unknown
                ):
                    raise ReviewInputError(
                        "quarantine comment allocation membership exceeds admission"
                    )
                pages = (
                    self.comment_read_calls
                    if self.comment_read_calls is not None
                    else MAX_PAGINATION_PAGES + 1
                )
            else:
                pages = (
                    cold_cursor_requests(count, size, largest)
                    if surface == "issue_comments" and self.cursor
                    else cold_rest_requests(count, largest)
                )
            if surface == "issue_comments" and not self.cursor:
                pages = max(
                    pages, cold_rest_requests(count, largest, (100, 50, 20, 10, 5, 1))
                )
            if pages > MAX_PAGINATION_PAGES:
                raise ReviewInputError(
                    f"quarantine {surface} page/encoded-byte capacity exceeded"
                )
            self.certificates[surface] = {
                "count_upper_bound": count,
                "encoded_bytes_upper_bound": size,
                "largest_upper_bound": largest,
                "physical_reads_upper_bound": pages,
                "reader": "packed-comments"
                if surface == "issue_comments" and self.comment_plan is not None
                else "closed-comment-cursor"
                if surface == "issue_comments" and self.cursor
                else "cold-rest",
            }

    def seed(self, surface, rows):
        seen = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or row["id"] in seen
            ):
                raise ReviewInputError(
                    f"quarantine {surface} census is incomplete or duplicated"
                )
            seen.add(row["id"])
            size = _wire(row) + _RESPONSE_METADATA_BYTES
            state = self.stats[surface]
            state["count"] += 1
            state["bytes"] += size
            state["largest"] = max(state["largest"], size)
            self.objects[surface, row["id"]] = _wire({"body": row.get("body", "")})
        self.require(self.stats)

    def before_request(self, method, path, body):
        snapshot = (
            self.comment_bounds,
            self.future_parts,
            self.future_bytes,
            self.review_bounds,
            self.check_bounds,
            self.inline_bounds,
            self.reaction_bounds,
            self.issue_update_bytes,
        )
        try:
            return self._before_request(method, path, body)
        except BaseException:
            (
                self.comment_bounds,
                self.future_parts,
                self.future_bytes,
                self.review_bounds,
                self.check_bounds,
                self.inline_bounds,
                self.reaction_bounds,
                self.issue_update_bytes,
            ) = snapshot
            raise

    def _before_request(self, method, path, body):
        if method not in {"POST", "PATCH", "PUT"} or not isinstance(body, dict):
            return
        target = urlsplit(path).path
        prefix = "/repos/" + self.identity.repository
        if not target.startswith(prefix + "/"):
            return
        suffix = target[len(prefix) :]
        surface, identifier = None, None
        if suffix == f"/issues/{self.identity.pull_request}/comments":
            surface = "issue_comments"
        elif re.fullmatch(r"/issues/comments/[1-9][0-9]*", suffix):
            surface, identifier = "issue_comments", int(suffix.rsplit("/", 1)[-1])
        elif suffix == f"/pulls/{self.identity.pull_request}/reviews":
            surface = "reviews"
        elif re.fullmatch(
            rf"/pulls/{self.identity.pull_request}/reviews/[1-9][0-9]*", suffix
        ):
            surface, identifier = "reviews", int(suffix.rsplit("/", 1)[-1])
        elif re.fullmatch(
            rf"/pulls/{self.identity.pull_request}/comments(?:/[1-9][0-9]*/replies)?",
            suffix,
        ):
            surface = "inline_replies"
        elif re.fullmatch(r"/pulls/comments/[1-9][0-9]*", suffix):
            surface, identifier = "inline_replies", int(suffix.rsplit("/", 1)[-1])
        elif suffix == "/check-runs" or re.fullmatch(
            r"/check-runs/[1-9][0-9]*", suffix
        ):
            surface = "checks"
            if method == "PATCH":
                identifier = int(suffix.rsplit("/", 1)[-1])
        elif suffix.endswith("/reactions"):
            surface = "reactions"
        if surface is None:
            return
        if surface == "issue_comments" and self.complete_tail:
            from .native_operation_envelope import require_operation_envelope
            from .operation_host import OPERATION_COMMENT_MARKER

            text = body.get("body")
            if isinstance(text, str) and text.startswith(
                OPERATION_COMMENT_MARKER + "\n"
            ):
                require_operation_envelope(text)
        candidate = deepcopy(self.stats)
        # A review POST is one physical write, but its comments are separate
        # persisted objects. The review-list response does not contain the
        # submitted comments array. Charge each resulting object exactly once,
        # including when the one outgoing request has an unknown outcome.
        object_body = (
            {key: value for key, value in body.items() if key != "comments"}
            if surface == "reviews"
            else body
        )
        object_wire = _wire(object_body)
        if (
            surface == "issue_comments"
            and self.comment_plan is not None
            and identifier in self.comment_plan.known
            and object_wire > self.comment_plan.known[identifier]
        ):
            raise ReviewInputError(
                "quarantine retained comment exceeds its admitted final size"
            )
        size = object_wire + _RESPONSE_METADATA_BYTES
        new = identifier is None and method == "POST"
        old_tail = (
            self.comment_bounds,
            self.future_parts,
            self.future_bytes,
            self.review_bounds,
            self.check_bounds,
            self.inline_bounds,
            self.reaction_bounds,
            self.issue_update_bytes,
        )
        if new and surface == "issue_comments" and self.comment_bounds is not None:
            choices = [bound for bound in self.comment_bounds if bound >= _wire(body)]
            if not choices:
                raise ReviewInputError(
                    "quarantine complete transaction tail is exhausted"
                )
            self.comment_bounds = list(self.comment_bounds)
            self.comment_bounds.remove(min(choices))
            self.future_parts, self.future_bytes = (
                len(self.comment_bounds),
                sum(self.comment_bounds),
            )
        elif new and self.cursor and surface in {"reviews", "checks"}:
            bounds = list(
                self.review_bounds if surface == "reviews" else self.check_bounds
            )
            choices = [bound for bound in bounds if bound >= object_wire]
            if not choices:
                raise ReviewInputError(
                    "quarantine native transaction tail is exhausted"
                )
            bounds.remove(min(choices))
            if surface == "reviews":
                self.review_bounds = bounds
            else:
                self.check_bounds = bounds
        elif new and self.cursor and surface in {"inline_replies", "reactions"}:
            bounds = list(
                self.inline_bounds
                if surface == "inline_replies"
                else self.reaction_bounds
            )
            choices = [bound for bound in bounds if bound >= _wire(body)]
            if not choices:
                raise ReviewInputError(
                    "quarantine reply/reaction transaction tail is exhausted"
                )
            bounds.remove(min(choices))
            if surface == "inline_replies":
                self.inline_bounds = bounds
            else:
                self.reaction_bounds = bounds
        state = candidate[surface]
        state["count"] += int(new)
        previous = (
            self.objects.get((surface, identifier), 0)
            if surface in {"issue_comments", "inline_replies", "reviews"}
            else 0
        )
        state["bytes"] += size if new else max(0, object_wire - previous)
        if self.cursor and surface == "issue_comments" and not new:
            growth = max(0, _wire(body) - previous)
            if _wire(body) > self.closure_wire or growth > self.issue_update_bytes:
                raise ReviewInputError(
                    "quarantine root update exceeds the admitted positive growth"
                )
            self.issue_update_bytes -= growth
        state["largest"] = max(state["largest"], size)
        if surface == "reviews" and isinstance(body.get("comments"), list):
            remaining = list(self.inline_bounds)
            for comment in body["comments"]:
                amount = _wire(comment) + _RESPONSE_METADATA_BYTES
                if self.cursor:
                    choices = [bound for bound in remaining if bound >= _wire(comment)]
                    if not choices:
                        raise ReviewInputError(
                            "quarantine inline publication tail is exhausted"
                        )
                    remaining.remove(min(choices))
                for name in ("inline_replies", "threads"):
                    candidate[name]["count"] += 1
                    candidate[name]["bytes"] += amount
                    candidate[name]["largest"] = max(candidate[name]["largest"], amount)
            self.inline_bounds = remaining
        elif (
            surface == "inline_replies"
            and new
            and "in_reply_to" not in body
            and not suffix.endswith("/replies")
        ):
            candidate["threads"]["count"] += 1
            candidate["threads"]["bytes"] += size
            candidate["threads"]["largest"] = max(candidate["threads"]["largest"], size)
        try:
            self.require(candidate)
        except BaseException:
            (
                self.comment_bounds,
                self.future_parts,
                self.future_bytes,
                self.review_bounds,
                self.check_bounds,
                self.inline_bounds,
                self.reaction_bounds,
                self.issue_update_bytes,
            ) = old_tail
            raise
        # Reserve before dispatch, including unknown effects. No response is
        # needed to consume this capacity, and no failure releases it.
        self.stats = candidate
        if identifier is not None:
            self.objects[surface, identifier] = max(previous, object_wire)
        self._pending_create = (surface, object_wire) if new else None

    def after_response(self, method, value):
        if (
            self._pending_create is not None
            and method == "POST"
            and isinstance(value, dict)
            and type(value.get("id")) is int
        ):
            surface, size = self._pending_create
            self.objects[surface, value["id"]] = size
        self._pending_create = None


def _comment_allocation_plan(
    ledger, receipt, comment_bounds, closure_wire, root_slot, *, identity
):
    """Issue capacity from the complete census and authenticated source bounds.

    The qualified old writers PATCH only session/operation roots. Result,
    inventory-intent and evidence carriers are append-only; their known native
    IDs retain their complete observed sizes. Old allocations remain reserved
    even when other old objects are already visible. No hint selects membership.
    """
    from .operation_host import OPERATION_COMMENT_MARKER
    from .packed_comment_census import NODE_METADATA_BYTES, CommentAllocationPlan
    from .session_ledger import SESSION_MARKER_ANY_VERSION

    if not ledger._inventory_complete:
        raise ReviewInputError(
            "quarantine native capacity needs the complete comment census"
        )
    isolation = receipt.get("execution_isolation", receipt)
    surfaces = isolation["qualification"]["exposure"]["surfaces"]
    ordinary, parts = surfaces["issue_comments"], surfaces["evidence_parts"]
    # Source allocations use this configured Bot and this exact repository/PR.
    # BigInt and numeric author IDs have at most16digits; selected DateTime
    # strings are ASCII and at most64characters. No finding prose, label or
    # mention is present in this projection. Other retained authors retain the
    # generic1024-byte envelope. Fresh walks enforce every declared envelope.
    slug = ledger.app_slug
    if not isinstance(slug, str) or not slug.isascii():
        raise ReviewInputError("quarantine comment metadata capacity is unsupported")
    metadata_bound = _wire(
        {
            "fullDatabaseId": "9" * 16,
            "createdAt": "x" * 64,
            "updatedAt": "x" * 64,
            "author": {
                "__typename": "Bot",
                "login": slug,
                "databaseId": "9" * 16,
            },
            "pullRequest": {
                "number": identity.pull_request,
                "repository": {
                    "databaseId": identity.repository_id,
                    "nameWithOwner": identity.repository,
                },
            },
        }
    )
    aliases = {slug.casefold(), slug.removesuffix("[bot]").casefold()}
    known = {}
    metadata_known = {}
    for row in ledger._cached_comments():
        identifier, body = row.get("id"), row.get("body")
        if type(identifier) is not int or identifier <= 0 or not isinstance(body, str):
            raise ReviewInputError("quarantine comment allocation census is invalid")
        if identifier in known:
            raise ReviewInputError("quarantine comment allocation census duplicates ID")
        amount = _wire({"body": body})
        if SESSION_MARKER_ANY_VERSION in body or body.startswith(
            OPERATION_COMMENT_MARKER + "\n"
        ):
            amount = max(amount, ordinary["max_object_wire_bytes"], closure_wire)
        known[identifier] = amount
        author = row.get("user")
        metadata_known[identifier] = (
            metadata_bound
            if isinstance(author, dict)
            and author.get("type") == "Bot"
            and isinstance(author.get("login"), str)
            and author["login"].isascii()
            and author["login"].casefold() in aliases
            else NODE_METADATA_BYTES
        )
    current = list(comment_bounds)
    if root_slot is not None:
        if type(root_slot) is not int or not 0 <= root_slot < len(current):
            raise ReviewInputError("quarantine mutable root allocation is invalid")
        current[root_slot] = max(current[root_slot], closure_wire)
    return CommentAllocationPlan(
        known,
        (
            *(parts["max_object_wire_bytes"],) * parts["new_objects"],
            *(ordinary["max_object_wire_bytes"],) * ordinary["new_objects"],
            *current,
        ),
        metadata_known=metadata_known,
        metadata_unknown=metadata_bound,
    )


def install_capacity(
    ledger,
    identity,
    receipt,
    *,
    token,
    future_parts,
    future_bytes,
    comment_bounds=None,
    root_slot=None,
):
    from .http import GitHubHttp
    from .publication_recovery import _publication_transport

    reserve = native_reserve(receipt)
    if reserve is None:
        return None
    if ledger._quarantine_native_comment_bounds is not None:
        from .native_operation_envelope import require_operation_envelope
        from .operation_host import OPERATION_COMMENT_MARKER

        # The complete current census is independently authenticated below;
        # this profile check supplies no ownership or source-selection proof.
        for row in ledger._cached_comments():
            body = row.get("body")
            if isinstance(body, str) and body.startswith(
                OPERATION_COMMENT_MARKER + "\n"
            ):
                require_operation_envelope(body)
    transport = _publication_transport(ledger.http)
    if type(transport) is not GitHubHttp:
        raise ReviewInputError("quarantine capacity transport is not audited")
    existing = transport._quarantine_capacity_guard
    if existing is not None:
        if existing.identity != identity or existing.reserve != reserve:
            raise ReviewInputError("quarantine native capacity scope changed")
        # Only a verified smaller remaining journey releases its own reserve.
        # A later consumer cannot replenish a consumed transaction promise.
        existing.require(existing.stats)
        return existing
    from review_sensei.session import MAX_SESSION_RECORD_BYTES

    from .quarantine_scan_capacity import CapacityCommentReader

    closure_wire = (
        2 * MAX_SESSION_RECORD_BYTES
        + 1024
        + 180 * len(ledger._observed_consumed_grants)
    )
    if comment_bounds is None:
        comment_bounds = (
            [MAX_PART_BYTES, MAX_PART_BYTES, 65_536]
            if future_parts == 3
            else [closure_wire] * future_parts
        )
    comment_plan = _comment_allocation_plan(
        ledger, receipt, comment_bounds, closure_wire, root_slot, identity=identity
    )
    guard = NativeCapacityGuard(
        identity=identity,
        reserve=reserve,
        future_parts=future_parts,
        future_bytes=sum(comment_bounds),
        comment_bounds=comment_bounds,
        cursor=True,
        closure_wire=closure_wire,
        reply=ledger._quarantine_native_tail_parts == 8,
        complete_tail=ledger._quarantine_native_comment_bounds is not None,
        comment_plan=comment_plan,
    )
    if not ledger._inventory_complete:
        raise ReviewInputError(
            "quarantine native capacity needs the complete comment census"
        )
    guard.seed("issue_comments", ledger._cached_comments())
    for surface, suffix in (("reviews", "reviews"), ("inline_replies", "comments")):
        cached = (
            getattr(ledger, "_quarantine_native_reviews", None)
            if surface == "reviews"
            else None
        )
        if cached is not None and cached[:2] == (ledger.http, identity):
            rows = cached[2]
        else:
            rows = ledger.http.paginate(
                path=ledger.http.repository_path(
                    identity.repository, f"/pulls/{identity.pull_request}/{suffix}"
                ),
                token=token,
            )
        guard.seed(surface, rows)
        if surface == "inline_replies":
            # Every thread has at least one inline root. Counting all inline
            # comments also as possible threads is a conservative superset.
            guard.seed("threads", rows)
    # The check authority reads the current head. Fixed old-head checks never
    # enter that reader; their late allocation reserve remains conservatively
    # charged here nevertheless. The same holds for fixed old reaction IDs:
    # none is enumerated by a successor authority reader.
    heads = set()
    current = getattr(ledger, "_quarantine_origin_reader", None)
    if current is not None:
        heads.add(current.current_scope.head_sha)
    checks = {}
    check_token = None
    if heads:
        provider = getattr(ledger, "_quarantine_check_token_provider", None)
        if not callable(provider):
            raise ReviewInputError("quarantine check reader capability is unavailable")
        check_token = provider()
    for head in sorted(heads):
        head_ids, expected = set(), None
        for page in range(1, MAX_PAGINATION_PAGES + 1):
            status, value = ledger.http.request(
                "GET",
                ledger.http.repository_path(
                    identity.repository,
                    f"/commits/{head}/check-runs?check_name=ReviewSensei&per_page=100&page={page}",
                ),
                token=check_token,
            )
            if (
                status != 200
                or not isinstance(value, dict)
                or not isinstance(value.get("check_runs"), list)
                or type(value.get("total_count")) is not int
            ):
                raise ReviewInputError(
                    "quarantine complete check census is unavailable"
                )
            rows = value["check_runs"]
            if value["total_count"] > MAX_PAGINATION_ITEMS:
                raise ReviewInputError("quarantine check census capacity exceeded")
            if expected is not None and value["total_count"] != expected:
                raise ReviewInputError("quarantine check census changed")
            expected = value["total_count"]
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or type(row.get("id")) is not int
                    or row["id"] in head_ids
                ):
                    raise ReviewInputError("quarantine check census is incomplete")
                head_ids.add(row["id"])
                checks[row["id"]] = row
            if len(rows) < 100:
                if len(head_ids) != expected:
                    raise ReviewInputError("quarantine check census is incomplete")
                break
        else:
            raise ReviewInputError("quarantine check census is incomplete")
    guard.seed("checks", list(checks.values()))
    transport._quarantine_capacity_guard = guard
    ledger._quarantine_capacity_cursor = True
    guard.comment_reader = CapacityCommentReader(ledger, identity, transport)
    return guard


def install_retained_capacity(ledger, identity, authenticated, *, token):
    archive, state = authenticated.archive, authenticated.state
    pending_assessments(
        archive["assessment_origins"],
        authenticated.inventories,
        state,
        scope=authenticated.lineage["scope_sha256"],
    )
    # Storage retains2parts per outstanding decision separately. This native
    # transaction reserves its startup archive, one event/result carrier and
    # mandatory root/metadata/approval/check closure before its first effect.
    future = getattr(ledger, "_quarantine_native_tail_parts", 2)
    bounds = ledger._quarantine_native_comment_bounds
    if bounds is not None:
        future = len(bounds)
    return install_capacity(
        ledger,
        identity,
        archive["target_isolation"],
        token=token,
        future_parts=future,
        future_bytes=future * MAX_PART_BYTES,
        comment_bounds=bounds,
    )
