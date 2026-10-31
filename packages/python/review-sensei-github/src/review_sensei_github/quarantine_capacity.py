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
        native_profile=None,
        receipt_update_bytes=0,
        old_check_head=None,
        current_check_head=None,
    ):
        for head in (old_check_head, current_check_head):
            if head is not None and (
                not isinstance(head, str) or re.fullmatch(r"[a-f0-9]{40}", head) is None
            ):
                raise ReviewInputError("quarantine check target projection is invalid")
        self.old_check_head, self.current_check_head = (
            old_check_head,
            current_check_head,
        )
        self.old_checks_outside_current_head = (
            old_check_head is not None
            and current_check_head is not None
            and old_check_head != current_check_head
        )
        self.identity, self.reserve = identity, deepcopy(reserve)
        self.native_profile = native_profile
        self.native_old_root_nodes = (
            None if native_profile is None else native_profile["targets"]
        )
        self.native_grouped = native_profile is not None and complete_tail
        self._native_prepared = None
        self.command_receipt_update_id = None
        self.future_parts, self.future_bytes = future_parts, future_bytes
        self.comment_reader: Any = None
        self.cursor = cursor
        self.closure_wire = closure_wire
        self.complete_tail = complete_tail
        self.comment_plan = comment_plan
        self.comment_read_calls = (
            comment_plan.worst_read_calls() if comment_plan is not None else None
        )
        self.issue_update_bytes = 2 * closure_wire + receipt_update_bytes
        self.comment_bounds = (
            list(comment_bounds) if comment_bounds is not None else None
        )
        self.review_bounds = [closure_wire] * 2 if cursor else []
        self.check_bounds = [closure_wire] * 2 if cursor else []
        self.inline_bounds = [6 * 16_384 + 1024] if cursor and reply else []
        self.reaction_bounds = [4096] if cursor else []
        if self.native_grouped:
            from .native_review_groups import full_inline_envelope

            envelope = full_inline_envelope()
            self.inline_bounds = [envelope["largest_selected_item_bytes"]] * envelope[
                "findings"
            ]
            self.review_bounds = [8192] * 8 + [6 * 65536 + 1024, self.closure_wire]
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

    def admit_grouped_publication(self, *, plan, review_wire=None, inline_wires=None):
        """Refine the complete before-startup promise; never add native room.

        The immutable accepted source and admission reconstruct this plan. Its
        body packets, old-body packet, metadata packets and final frame share
        the original complete native-walk certificate.
        """
        from .native_review_groups import (
            NativeReviewPlan,
            full_inline_envelope,
            wire_bytes,
        )

        if not self.native_grouped or type(plan) is not NativeReviewPlan:
            raise ReviewInputError(
                "grouped publication complete startup admission is unavailable"
            )
        if review_wire is None or inline_wires is None:
            if self._native_prepared is None or self._native_prepared[0] != plan.sha256:
                raise ReviewInputError(
                    "grouped publication was not prepared before effects"
                )
            self.require(self.stats)
            return
        requested = (plan.sha256, review_wire, tuple(inline_wires))
        if self._native_prepared is not None:
            if requested != self._native_prepared:
                raise ReviewInputError(
                    "grouped publication original preparation changed"
                )
            self.require(self.stats)
            return
        envelope = full_inline_envelope()
        if (
            len(plan.items) > 250
            or len(plan.groups) > 8
            or len(inline_wires) != len(plan.items)
            or sum(item.selected_upper_bound for item in plan.items)
            > envelope["selected_total_bytes"]
            or review_wire > 6 * 65536 + 1024
            or any(
                wire_bytes(item.payload()) > bound
                for item, bound in zip(plan.items, inline_wires)
            )
        ):
            raise ReviewInputError(
                "grouped publication exceeds complete startup admission"
            )
        old = self.review_bounds, self.inline_bounds
        self.review_bounds = [
            wire_bytes(
                {
                    "body": plan.part_body(i),
                    "event": "COMMENT",
                    "commit_id": plan.head_sha,
                }
            )
            for i in range(len(plan.groups))
        ] + [max(self.closure_wire, review_wire), self.closure_wire]
        self.inline_bounds = list(inline_wires)
        try:
            self.require(self.stats)
        except BaseException:
            self.review_bounds, self.inline_bounds = old
            raise
        self._native_prepared = requested

    def admit_publication(self, *, review_wire, inline_wires):
        """Reserve the prepared publication, approval, checks and root close."""
        if not self.cursor:
            return
        if self.native_grouped:
            raise ReviewInputError(
                "complete grouped admission cannot use a legacy publication"
            )
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
                # The current check-name reader has exactly one page. Old
                # allocations may share that head unless authenticated target
                # projection independently proves they are elsewhere.
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
            if self.native_profile is not None and surface in {
                "reviews",
                "inline_replies",
                "threads",
            }:
                from .native_review_census import (
                    MAX_REVIEWS,
                    MAX_ROOTS,
                    closing_envelope_bound,
                )
                from .native_review_groups import full_inline_envelope

                if largest > _PAGE_BYTES or count > MAX_PAGINATION_ITEMS:
                    raise ReviewInputError(f"quarantine {surface} capacity exceeded")
                if surface == "reviews":
                    if count > MAX_REVIEWS or size > MAX_PAGINATION_PAGES * _PAGE_BYTES:
                        raise ReviewInputError(
                            "quarantine native review metadata capacity exceeded"
                        )
                else:
                    roots = stats["threads"]["count"] + len(self.inline_bounds)
                    if roots > MAX_ROOTS:
                        raise ReviewInputError(
                            "quarantine complete native root capacity exceeded"
                        )
                    # Replies cannot become thread roots under the installed
                    # source/parent certificate. They stay in reserve and in
                    # the complete conversation/report-conflict domains.
                    if (
                        old["new_wire_bytes"] + old["positive_update_bytes"]
                        > MAX_PAGINATION_PAGES * _PAGE_BYTES
                    ):
                        raise ReviewInputError(
                            "quarantine complete conversation capacity exceeded"
                        )
                envelope = full_inline_envelope()
                if (
                    max(
                        envelope["largest_body_response_bytes"],
                        envelope["smallest_with_old_roots_bytes"],
                        envelope["second_with_root_opening_bytes"],
                        envelope["third_with_middle_bytes"],
                        closing_envelope_bound(),
                    )
                    > MAX_GITHUB_RESPONSE_BYTES
                ):
                    raise ReviewInputError(
                        "quarantine complete native packet capacity exceeded"
                    )
                self.certificates[surface] = {
                    "count_upper_bound": count,
                    "encoded_bytes_upper_bound": size,
                    "largest_upper_bound": largest,
                    "physical_reads_upper_bound": 10,
                    "reader": "complete-native-root-union",
                    "complete_conversation_reserve": deepcopy(
                        self.reserve["inline_replies"]
                    ),
                }
                continue
            if count > MAX_PAGINATION_ITEMS or largest > _PAGE_BYTES:
                raise ReviewInputError(
                    f"quarantine {surface} item/response capacity exceeded"
                )
            if size > MAX_PAGINATION_PAGES * _PAGE_BYTES:
                raise ReviewInputError(
                    f"quarantine {surface} encoded-byte capacity exceeded"
                )
            if surface == "checks":
                reader_count = (
                    current["count"] + len(tail)
                    if self.old_checks_outside_current_head
                    else count
                )
                reader_size = (
                    current["bytes"] + sum(tail) + len(tail) * _RESPONSE_METADATA_BYTES
                    if self.old_checks_outside_current_head
                    else size
                ) + 2 * self.closure_wire
                if reader_count > MAX_CHECK_PAGE_SIZE or reader_size > _PAGE_BYTES:
                    raise ReviewInputError(
                        "quarantine current-check transaction capacity exceeded"
                    )
                self.certificates[surface] = {
                    "count_upper_bound": reader_count,
                    "encoded_bytes_upper_bound": reader_size,
                    "physical_reads_upper_bound": 1,
                    "reader": "current-head-single-page",
                    "old_check_head_excluded": self.old_checks_outside_current_head,
                }
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

    def seed_projected_roots(self):
        """Count actual root identities with their source-enforced body bounds.

        These are capacity bounds, not fabricated native body observations.
        Full conversation/reply/report readers keep their complete raw domain.
        """
        from .native_review_groups import full_inline_envelope

        profile = self.native_profile
        if profile is None:
            raise ReviewInputError("native projected root capacity is unavailable")
        maximum = full_inline_envelope()["largest_selected_item_bytes"]
        known = profile["root_body_wires"]
        for identifier in profile["root_metadata"]:
            bound = known.get(identifier, maximum)
            for surface in ("inline_replies", "threads"):
                state = self.stats[surface]
                state["count"] += 1
                state["bytes"] += bound + _RESPONSE_METADATA_BYTES
                state["largest"] = max(
                    state["largest"], bound + _RESPONSE_METADATA_BYTES
                )
                self.objects[surface, identifier] = bound
        self.require(self.stats)

    def before_request(self, method, path, body):
        # A response may bind only its own physical create. A later GraphQL
        # POST or a read after an unknown write cannot inherit that pending ID.
        self._pending_create = None
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
        self._pending_create = (
            (surface, object_wire, deepcopy(object_body)) if new else None
        )

    def after_response(self, method, value):
        if (
            self._pending_create is not None
            and method == "POST"
            and isinstance(value, dict)
            and type(value.get("id")) is int
        ):
            surface, size, submitted = self._pending_create
            self._pending_create = None
            self.objects[surface, value["id"]] = size
            if surface == "issue_comments" and value.get("body") == submitted.get(
                "body"
            ):
                self._bind_immutable_comment_capacity(value["id"], submitted)

    def _bind_immutable_comment_capacity(self, identifier, submitted):
        """Refine packing for an own exact echo, never reuse its content.

        Every old and interrupted-current unknown slot remains in the plan.
        This intentionally double-counts this known object rather than removing
        any uncertainty liability. The refined plan is used only if its full
        interleaving proof needs no more physical calls than original admission.
        Subsequent walks still fetch every body and full opening/closing scope.
        """
        from .operation_host import OPERATION_COMMENT_MARKER
        from .packed_comment_census import CommentAllocationPlan
        from .session_ledger import SESSION_MARKER_ANY_VERSION

        plan = self.comment_plan
        text = submitted.get("body")
        if (
            plan is None
            or self.comment_read_calls is None
            or type(identifier) is not int
            or identifier <= 0
            or identifier in plan.known
            or not isinstance(text, str)
            or SESSION_MARKER_ANY_VERSION in text
            or text.startswith(OPERATION_COMMENT_MARKER + "\n")
            or "<!-- reviewsensei:command-receipt:v1 " in text
            or len(plan.known) + len(plan.unknown) >= 200
        ):
            return
        refined = CommentAllocationPlan(
            {**plan.known, identifier: _wire({"body": text})},
            plan.unknown,
            metadata_known=plan.metadata_known,
            metadata_unknown=plan.metadata_unknown,
        )
        if refined.worst_read_calls() <= self.comment_read_calls:
            self.comment_plan = refined
        self._pending_create = None


def _comment_allocation_plan(
    ledger,
    receipt,
    comment_bounds,
    closure_wire,
    root_slot,
    *,
    identity,
    receipt_update=None,
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
        if receipt_update is not None and identifier == receipt_update[0]:
            amount = max(amount, receipt_update[1])
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
    current = getattr(ledger, "_quarantine_origin_reader", None)
    current_check_head = None if current is None else current.current_scope.head_sha
    isolation = receipt.get("execution_isolation", receipt)
    old_check_head = isolation.get("old_head_sha")
    existing = transport._quarantine_capacity_guard
    if existing is not None:
        if (
            existing.identity != identity
            or existing.reserve != reserve
            or (existing.old_check_head, existing.current_check_head)
            != (old_check_head, current_check_head)
        ):
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
    receipt_update = None
    receipt_update_bytes = 0
    receipt_marker = getattr(ledger, "_quarantine_command_receipt_marker", None)
    if receipt_marker is not None:
        matches = [
            row
            for row in ledger._cached_comments()
            if isinstance(row.get("body"), str)
            and row["body"].rstrip().endswith(receipt_marker)
            and isinstance(row.get("user"), dict)
            and row["user"].get("type") == "Bot"
            and row["user"].get("id") == ledger._evidence_author_id
        ]
        if len(matches) > 1:
            raise ReviewInputError("current command receipt is duplicated")
        if matches:
            # Transfer the existing command's receipt promise to the selected
            # existing ID before effects. This adds no slot or byte allowance;
            # the old thirteen allocations and their uncertainty stay intact.
            choices = [bound for bound in comment_bounds if bound >= 65_536]
            if not choices:
                raise ReviewInputError("current command receipt update is unfunded")
            bound = min(choices)
            comment_bounds = list(comment_bounds)
            comment_bounds.remove(bound)
            future_parts -= 1
            receipt_update = (matches[0]["id"], bound)
            receipt_update_bytes = max(0, bound - _wire({"body": matches[0]["body"]}))
    comment_plan = _comment_allocation_plan(
        ledger,
        receipt,
        comment_bounds,
        closure_wire,
        root_slot,
        identity=identity,
        receipt_update=receipt_update,
    )
    native_profile = None
    isolation = receipt.get("execution_isolation", receipt)
    qualification = isolation.get("qualification", {})
    if qualification.get("schema_version") == "quarantine-published-tail-provenance-v2":
        from .native_reader_projection import (
            read_projection_roots,
            validate_reader_projection,
        )
        from .native_review_groups import NativeReviewPlan
        from .quarantine_activation import registered_activation

        registration = registered_activation(
            identity, isolation["predecessor_comment_id"]
        )
        contract = registration.get(
            "published_tail", registration.get("prepublication")
        )
        if not isinstance(contract, dict):
            raise ReviewInputError(
                "native reader projection has no installed source contract"
            )
        projection = validate_reader_projection(
            qualification["reader_projection"],
            contract,
            source_contract_sha256=qualification["exposure"]["source_contract_sha256"],
            surface_exposure=qualification["exposure"]["surfaces"],
        )
        scope = ledger._quarantine_origin_reader.current_scope
        skeleton = NativeReviewPlan.prepare(
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=scope.head_sha,
            base_sha=scope.base_sha,
            transaction_id="0" * 64,
            result_sha256="0" * 64,
            items=(),
        )
        targets, root_count, old_bytes, reads, root_metadata, root_body_wires = (
            read_projection_roots(
                ledger.http,
                token=token,
                repository=identity.repository,
                plan=skeleton,
                projection=projection,
                ceased_census_sha256=isolation["census_sha256"],
                current_census_sha256=isolation["census_sha256"],
            )
        )
        if old_bytes + reserve["threads"]["positive_update_bytes"] > 96 * 1024:
            raise ReviewInputError(
                "quarantine historical root positive-growth capacity exceeded"
            )
        native_profile = {
            "projection": projection,
            "targets": targets,
            "root_count": root_count,
            "reads": reads,
            "root_metadata": root_metadata,
            "root_body_wires": root_body_wires,
        }
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
        native_profile=native_profile,
        receipt_update_bytes=receipt_update_bytes,
        old_check_head=old_check_head,
        current_check_head=current_check_head,
    )
    guard.command_receipt_update_id = (
        None if receipt_update is None else receipt_update[0]
    )
    if not ledger._inventory_complete:
        raise ReviewInputError(
            "quarantine native capacity needs the complete comment census"
        )
    guard.seed("issue_comments", ledger._cached_comments())
    for surface, suffix in (("reviews", "reviews"), ("inline_replies", "comments")):
        if (
            surface == "inline_replies"
            and native_profile is not None
            and ledger._quarantine_native_tail_parts != 8
        ):
            guard.seed_projected_roots()
            continue
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
            guard.seed(
                "threads",
                [row for row in rows if row.get("in_reply_to_id") is None]
                if native_profile is not None
                else rows,
            )
    # The check authority reads the current head. Excluding old allocations
    # requires the authenticated isolation receipt's distinct immutable head;
    # same/unknown-head checks retain the complete old reserve in this reader.
    # The same holds for fixed old reaction IDs:
    # none is enumerated by a successor authority reader.
    heads = set()
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
        from .api_run_host import review_startup_plan
        from .caller_operation import caller_for_http

        caller = caller_for_http(ledger.http)
        all_checks = caller is not None and caller.plan == review_startup_plan(
            caller.job, quarantine_v3=True
        )
        for page in range(1, MAX_PAGINATION_PAGES + 1):
            status, value = ledger.http.request(
                "GET",
                ledger.http.repository_path(
                    identity.repository,
                    f"/commits/{head}/check-runs?check_name=ReviewSensei&per_page=100&page={page}"
                    + ("&filter=all" if all_checks else ""),
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
