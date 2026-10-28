"""Permanent per-surface uncertainty reserves on one bounded native transport.

The current complete census and every subsequent physical mutation share the
caller's existing HTTP allowance. Known writes consume native room; elapsed
time, absent objects and old materialization never release the old reserve.
This guard grants no write authority and never retries a consumed request.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from urllib.parse import urlsplit

from review_sensei.bounded_evidence import MAX_PART_BYTES
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import pending_assessments

from .http import MAX_GITHUB_RESPONSE_BYTES, MAX_PAGINATION_ITEMS, MAX_PAGINATION_PAGES

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
    def __init__(self, *, identity, reserve, future_parts=0, future_bytes=0):
        self.identity, self.reserve = identity, deepcopy(reserve)
        self.future_parts, self.future_bytes = future_parts, future_bytes
        self.stats = {
            name: {"count": 0, "bytes": 0, "largest": 0} for name in _SURFACES
        }
        self.objects = {}
        self._pending_create = None
        self.require(self.stats)

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
                size += self.future_bytes
            # Native response metadata is additional to emitted mutation bytes.
            size += old["new_objects"] * _RESPONSE_METADATA_BYTES
            largest = max(
                current["largest"],
                old["max_object_wire_bytes"] + _RESPONSE_METADATA_BYTES,
            )
            if surface == "issue_comments" and self.future_parts:
                largest = max(largest, MAX_PART_BYTES + _RESPONSE_METADATA_BYTES)
            if count > MAX_PAGINATION_ITEMS or largest > _PAGE_BYTES:
                raise ReviewInputError(
                    f"quarantine {surface} item/response capacity exceeded"
                )
            # A conservative sequential packing bound: a page is limited by
            # either its 100 items or by bytes. Two reads remain for adaptation
            # and final completeness. Actual readers retain their stricter caps.
            pages = (
                math.ceil(count / 100)
                + math.ceil(size / max(1, _PAGE_BYTES - largest))
                + (2 if count else 0)
            )
            if pages > MAX_PAGINATION_PAGES:
                raise ReviewInputError(
                    f"quarantine {surface} page/encoded-byte capacity exceeded"
                )

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
        candidate = deepcopy(self.stats)
        size = _wire(body) + _RESPONSE_METADATA_BYTES
        new = identifier is None and method == "POST"
        state = candidate[surface]
        state["count"] += int(new)
        previous = (
            self.objects.get((surface, identifier), 0)
            if surface in {"issue_comments", "inline_replies", "reviews"}
            else 0
        )
        state["bytes"] += size if new else max(0, _wire(body) - previous)
        state["largest"] = max(state["largest"], size)
        if surface == "reviews" and isinstance(body.get("comments"), list):
            for comment in body["comments"]:
                amount = _wire(comment) + _RESPONSE_METADATA_BYTES
                for name in ("inline_replies", "threads"):
                    candidate[name]["count"] += 1
                    candidate[name]["bytes"] += amount
                    candidate[name]["largest"] = max(candidate[name]["largest"], amount)
        elif (
            surface == "inline_replies"
            and new
            and "in_reply_to" not in body
            and not suffix.endswith("/replies")
        ):
            candidate["threads"]["count"] += 1
            candidate["threads"]["bytes"] += size
            candidate["threads"]["largest"] = max(candidate["threads"]["largest"], size)
        self.require(candidate)
        # Reserve before dispatch, including unknown effects. No response is
        # needed to consume this capacity, and no failure releases it.
        self.stats = candidate
        if identifier is not None:
            self.objects[surface, identifier] = max(previous, _wire(body))
        self._pending_create = (surface, _wire(body)) if new else None

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


def install_capacity(ledger, identity, receipt, *, token, future_parts, future_bytes):
    reserve = native_reserve(receipt)
    if reserve is None:
        return None
    existing = getattr(ledger.http, "_quarantine_capacity_guard", None)
    if existing is not None:
        if existing.identity != identity or existing.reserve != reserve:
            raise ReviewInputError("quarantine native capacity scope changed")
        # Only a verified smaller remaining journey releases its own reserve.
        existing.future_parts = min(existing.future_parts, future_parts)
        existing.future_bytes = min(existing.future_bytes, future_bytes)
        existing.require(existing.stats)
        return existing
    guard = NativeCapacityGuard(
        identity=identity,
        reserve=reserve,
        future_parts=future_parts,
        future_bytes=future_bytes,
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
    ledger.http._quarantine_capacity_guard = guard
    return guard


def install_retained_capacity(ledger, identity, authenticated, *, token):
    archive, state = authenticated.archive, authenticated.state
    pending = pending_assessments(
        archive["assessment_origins"],
        authenticated.inventories,
        state,
        scope=authenticated.lineage["scope_sha256"],
    )
    future = 2 * len(pending) + 4
    return install_capacity(
        ledger,
        identity,
        archive["target_isolation"],
        token=token,
        future_parts=future,
        future_bytes=future * MAX_PART_BYTES,
    )
