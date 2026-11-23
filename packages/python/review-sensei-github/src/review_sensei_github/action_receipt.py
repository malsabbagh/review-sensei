"""One exact App-owned maintainer receipt inside the active action allowance."""

from __future__ import annotations

import hashlib
import inspect
import sys
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import MAX_PART_BYTES, canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.presentation import render_command_receipt
from review_sensei.session import MAX_SESSION_RECORD_BYTES
from review_sensei.source_registry import require_module_seal

from .errors import GitHubHTTPTransientError, GitHubPublicationTransientError
from .http import MAX_GITHUB_RESPONSE_BYTES, MAX_PAGINATION_PAGES

COMMAND_RECEIPT_WIRE_BOUND = 65536 + 4096
_RECEIPT_PAGE_SIZES = (100, 20, 10, 5, 1)
_RECEIPT_MARKER_PREFIX = "<!-- reviewsensei:command-receipt:v1 "


@dataclass(frozen=True, eq=False)
class _PlainReceiptPagePlan:
    caller: Any
    identity: Any
    operation_id: str
    owner: Any
    inventory_ids: tuple[int, ...]
    page_requests: tuple[tuple[int, int], ...]
    page_bounds: tuple[int, ...]
    maximum_items: int


_PLAIN_SINGLE_PAGE_RECEIPT_LEASES: WeakKeyDictionary[Any, _PlainReceiptPagePlan] = (
    WeakKeyDictionary()
)


def plan_plain_receipt_pages(
    ledger: Any,
    identity: Any,
    *,
    staged_parts_count: int,
    lifecycle_parts_count: int,
) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...], tuple[int, ...], int] | None:
    """Plan byte-aware aligned comment pages for plain command receipt readback."""
    if (
        not getattr(ledger, "_inventory_complete", False)
        or type(staged_parts_count) is not int
        or staged_parts_count < 0
        or type(lifecycle_parts_count) is not int
        or lifecycle_parts_count < 0
    ):
        return None
    items = ledger._cached_comments()
    if any(
        not isinstance(item, dict)
        or type(item.get("id")) is not int
        or item["id"] <= 0
        or not isinstance(item.get("body"), str)
        for item in items
    ):
        return None
    ids = tuple(item["id"] for item in items)
    if len(set(ids)) != len(ids):
        return None

    from .session_ledger import parse_session_comment, plain_session_comment_json_bound

    root_indices = [
        index
        for index, item in enumerate(items)
        if parse_session_comment(item["body"], identity=identity) is not None
    ]
    if len(root_indices) != 1:
        return None
    root_index = root_indices[0]
    root_wire = 6 * MAX_SESSION_RECORD_BYTES
    if getattr(ledger, "_controlled_restart", None) is None:
        grants = set(getattr(ledger, "_observed_consumed_grants", ()) or ())
        accepted = getattr(ledger, "_accepted_consumed_grant", None)
        if accepted is not None:
            grants.add(accepted)
        root_wire = plain_session_comment_json_bound(
            consumed_grants=grants,
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
        )

    bounds = [len(canonical_bytes(item)) for item in items]
    replaced = len(canonical_bytes(items[root_index]["body"]))
    bounds[root_index] = max(
        bounds[root_index], bounds[root_index] - replaced + root_wire + 4096
    )
    for index, item in enumerate(items):
        if _RECEIPT_MARKER_PREFIX in item["body"]:
            old_body = len(canonical_bytes(item["body"]))
            bounds[index] = max(
                bounds[index], bounds[index] - old_body + COMMAND_RECEIPT_WIRE_BOUND
            )
    bounds.extend(
        [MAX_PART_BYTES + 512 + 4096] * (staged_parts_count + lifecycle_parts_count)
    )
    bounds.append(COMMAND_RECEIPT_WIRE_BOUND)
    if len(bounds) > 200:
        return None

    single_bound = 2 + sum(bounds) + max(0, len(bounds) - 1)
    if len(bounds) < 100 and single_bound < MAX_GITHUB_RESPONSE_BYTES:
        return (ids, ((100, 1),), (single_bound,), len(bounds))

    # In multi-page layouts an earlier skipped lifecycle/staged part can shift the
    # 69,632-byte command receipt earlier into an appended slot, so bound every
    # newly appended slot by the receipt ceiling before choosing page boundaries.
    appended_start = len(items)
    for idx in range(appended_start, len(bounds)):
        bounds[idx] = max(bounds[idx], COMMAND_RECEIPT_WIRE_BOUND)

    requests: list[tuple[int, int]] = []
    response_bounds: list[int] = []
    offset = 0
    while offset <= len(bounds):
        for size in _RECEIPT_PAGE_SIZES:
            if offset % size:
                continue
            members = bounds[offset : offset + size]
            bound = 2 + sum(members) + max(0, len(members) - 1)
            if bound < MAX_GITHUB_RESPONSE_BYTES:
                requests.append((size, offset // size + 1))
                response_bounds.append(bound)
                offset += size
                break
        else:
            return None
        if len(requests) > MAX_PAGINATION_PAGES:
            return None
    return (ids, tuple(requests), tuple(response_bounds), len(bounds))


def _bind_plain_single_page_receipt(
    ledger: Any,
    caller: Any,
    identity: Any,
    *,
    operation_id: str,
    page_requests: tuple[tuple[int, int], ...] = ((100, 1),),
    page_bounds: tuple[int, ...] = (MAX_GITHUB_RESPONSE_BYTES - 1,),
    inventory_ids: tuple[int, ...] = (),
    maximum_items: int = 99,
) -> None:
    """Bind one single-page or sealed multi-page receipt window from command preflight."""
    from . import finding_lifecycle

    require_module_seal(sys.modules[__name__])
    require_module_seal(finding_lifecycle)
    expected_code = inspect.unwrap(
        finding_lifecycle.FindingLifecycleJournal.preflight_command_finalization
    ).__code__
    frame = inspect.currentframe()
    caller_frame = frame.f_back if frame is not None else None
    if (
        caller_frame is None
        or caller_frame.f_code is not expected_code
        or caller_frame.f_globals is not vars(finding_lifecycle)
        or getattr(caller_frame.f_locals.get("self"), "ledger", None) is not ledger
    ):
        raise ReviewInputError(
            "plain single-page receipt lease requires command preflight"
        )
    if (
        not isinstance(page_requests, tuple)
        or not 1 <= len(page_requests) <= MAX_PAGINATION_PAGES
        or any(
            not isinstance(req, tuple)
            or len(req) != 2
            or type(req[0]) is not int
            or req[0] not in _RECEIPT_PAGE_SIZES
            or type(req[1]) is not int
            or req[1] < 1
            for req in page_requests
        )
        or not isinstance(page_bounds, tuple)
        or len(page_bounds) != len(page_requests)
        or any(
            type(bound) is not int or not 0 < bound < MAX_GITHUB_RESPONSE_BYTES
            for bound in page_bounds
        )
        or not isinstance(inventory_ids, tuple)
        or any(type(cid) is not int or cid <= 0 for cid in inventory_ids)
        or len(set(inventory_ids)) != len(inventory_ids)
        or type(maximum_items) is not int
        or not 0 <= maximum_items <= 200
    ):
        raise ReviewInputError("plain receipt page plan is invalid")
    _PLAIN_SINGLE_PAGE_RECEIPT_LEASES[ledger] = _PlainReceiptPagePlan(
        caller=caller,
        identity=identity,
        operation_id=operation_id,
        owner=caller.accounting.owner,
        inventory_ids=inventory_ids,
        page_requests=page_requests,
        page_bounds=page_bounds,
        maximum_items=maximum_items,
    )


def publish_command_receipt(run: Any, authority: Any, body: str, result: Any) -> None:
    source = authority.document["source"]
    digest = hashlib.sha256(body.encode()).hexdigest()
    text = render_command_receipt(
        actor=authority.document["actor"]["login"],
        source_comment_id=source["id"],
        command_digest=digest,
        receipts=result.receipts,
        ledger_written=result.applied,
    )
    marker = f"<!-- reviewsensei:command-receipt:v1 source={source['id']} digest={digest} -->"
    from .quarantine_command_receipt import receipt_read_window

    with receipt_read_window(
        run, authority, text=text, marker=marker
    ) as bounded_receipt:
        token = run.broker.exchange(run.oidc, capability="issue_reply")
        path = run.http.repository_path(
            run.identity.repository, f"/issues/{run.identity.pull_request}/comments"
        )
        app_id = run.ledger._evidence_author_id
        from .publication_recovery import _publication_transport
        from .trust_effects import verification_profile

        guard = getattr(
            _publication_transport(run.http), "_quarantine_capacity_guard", None
        )
        plain_lease = (
            _PLAIN_SINGLE_PAGE_RECEIPT_LEASES.pop(run.ledger, None)
            if bounded_receipt is None and guard is None
            else None
        )
        receipt_wire_ok = (
            len(canonical_bytes({"body": text})) + 4096 <= COMMAND_RECEIPT_WIRE_BOUND
        )
        plain_plan = (
            plain_lease
            if (
                isinstance(plain_lease, _PlainReceiptPagePlan)
                and plain_lease.caller is run.caller
                and plain_lease.identity == run.identity
                and plain_lease.owner == run.caller.accounting.owner
                and verification_profile(run.caller) is None
                and not getattr(run.ledger, "_quarantine_required", False)
                and receipt_wire_ok
            )
            else None
        )

        def matching(live_readback: bool = False):
            if bounded_receipt is not None:
                values = bounded_receipt.read(path=path, token=token)
            elif plain_plan is not None:
                if plain_plan.page_requests == ((100, 1),):
                    status, values = run.http.request(
                        "GET", f"{path}?per_page=100&page=1", token=token
                    )
                    if (
                        status != 200
                        or not isinstance(values, list)
                        or len(values) >= 100
                    ):
                        raise ReviewInputError(
                            "command receipt single-page frontier changed"
                        )
                else:
                    collected: list[Any] = []
                    seen_ids: set[int] = set()
                    observed_ids: list[int] = []
                    for (page_size, page), _bound in zip(
                        plain_plan.page_requests, plain_plan.page_bounds, strict=True
                    ):
                        status, page_values = run.http.request(
                            "GET",
                            f"{path}?per_page={page_size}&page={page}",
                            token=token,
                        )
                        if (
                            status != 200
                            or not isinstance(page_values, list)
                            or len(page_values) > page_size
                        ):
                            raise ReviewInputError(
                                "command receipt paged frontier changed"
                            )
                        for item in page_values:
                            if (
                                not isinstance(item, dict)
                                or type(item.get("id")) is not int
                                or item["id"] <= 0
                                or item["id"] in seen_ids
                            ):
                                raise ReviewInputError(
                                    "command receipt paged frontier changed"
                                )
                            seen_ids.add(item["id"])
                            observed_ids.append(item["id"])
                        collected.extend(page_values)
                        if len(collected) > plain_plan.maximum_items:
                            raise ReviewInputError(
                                "command receipt paged frontier changed"
                            )
                        if len(page_values) < page_size:
                            prefix_len = len(plain_plan.inventory_ids)
                            if (
                                tuple(observed_ids[:prefix_len])
                                != plain_plan.inventory_ids
                            ):
                                raise ReviewInputError(
                                    "command receipt paged frontier changed"
                                )
                            values = collected
                            break
                    else:
                        raise ReviewInputError("command receipt paged frontier changed")
            elif (
                guard is None
                and getattr(run.ledger, "_quarantine_required", False)
                and getattr(run.ledger, "_quarantine_lifecycle_capacity", None) is None
            ):
                values = None
                if (
                    not live_readback
                    and getattr(run.ledger, "_inventory_complete", False) is True
                    and len(run.ledger._cached_comments()) >= 98
                ):
                    from .quarantine_origin import rebind_same_lineage_owner

                    authenticated = getattr(
                        run.ledger, "_quarantine_authenticated", None
                    )
                    effect_record = getattr(run.caller, "_effect_record", None)
                    if authenticated is not None and effect_record is not None:
                        try:
                            rebind_same_lineage_owner(
                                authenticated,
                                run.ledger,
                                run.caller,
                                run.identity,
                                effect_record,
                            )
                        except ReviewInputError:
                            pass
                        else:
                            values = run.ledger._cached_comments()
                if values is None:
                    from .quarantine_census import fetch_comments

                    run.caller._require_ledger(run.ledger, run.identity)
                    run.caller._quarantine_census_api_url = run.ledger.http.api_url
                    values = fetch_comments(run.ledger, run.identity)
            else:
                values = run.http.paginate(path=path, token=token)
            return [
                item
                for item in values
                if isinstance(item, dict)
                and type(item.get("id")) is int
                and isinstance(item.get("body"), str)
                and item["body"].rstrip().endswith(marker)
                and isinstance(item.get("user"), dict)
                and item["user"].get("id") == app_id
                and item["user"].get("type") == "Bot"
            ]

        previous = matching()
        update_id = getattr(guard, "command_receipt_update_id", None)
        if update_id is not None and (
            len(previous) != 1 or previous[0]["id"] != update_id
        ):
            raise ReviewInputError("original command receipt update target changed")
        if len(previous) > 1:
            raise ReviewInputError("current command receipt is duplicated")
        if previous and previous[0]["body"].strip() == text.strip():
            return
        authority.fence()
        target = (
            run.http.repository_path(
                run.identity.repository, "/issues/comments/" + str(previous[0]["id"])
            )
            if previous
            else path
        )
        from .api_run_plan import COMMAND_RECEIPT_CONFIRMATION_REQUESTS

        profile = verification_profile(run.caller)
        verification = int(profile is not None)
        if profile is not None:
            from .trust_capabilities import require_source_capability

            require_source_capability(
                run.caller,
                token,
                capability="issue_reply",
                authority=profile.session.attestation,
            )
        # This local allocation preserves confirmation, but is not whole-route
        # admission. The source effect gate still refuses an unpriced business route.
        write_tail = 1 + (
            (len(plain_plan.page_requests) + 2)
            if (bounded_receipt is None and plain_plan is not None)
            else (
                COMMAND_RECEIPT_CONFIRMATION_REQUESTS
                if bounded_receipt is None
                else bounded_receipt.confirmation_requests()
            )
        )
        capacity = write_tail + verification
        run.caller.budget_view.preflight(capacity)
        with run.caller._operation(
            "command-receipt:" + digest,
            capacity,
            write_tail=write_tail,
            prewrite_reads=verification,
        ) as allocation:
            observation_index = len(run.caller.observations)
            if bounded_receipt is not None:
                bounded_receipt.before_write(
                    method="PATCH" if previous else "POST",
                    target=target,
                    observation_index=observation_index,
                )
            unknown = False
            try:
                status, echoed = run.http.request(
                    "PATCH" if previous else "POST",
                    target,
                    token=token,
                    body={"body": text},
                )
                if status in {202, 409, 422, 429} or status >= 500:
                    unknown = True
                    echoed = None
                elif status not in {200, 201}:
                    raise ReviewInputError("current command receipt was not accepted")
            except (GitHubHTTPTransientError, GitHubPublicationTransientError):
                # The source-owned exact marker/content/producer readback is the only
                # reconciliation. Never replay this receipt's POST/PATCH.
                unknown = True
                echoed = None
            if (
                not isinstance(echoed, dict)
                or echoed.get("body") != text
                or not isinstance(echoed.get("user"), dict)
                or echoed["user"].get("id") != app_id
                or echoed["user"].get("type") != "Bot"
            ):
                matches = matching(live_readback=True)
                if len(matches) != 1 or matches[0]["body"] != text:
                    raise ReviewInputError("current command receipt is unconfirmed")
                authority.fence()
                if unknown:
                    if profile is not None:
                        import json

                        from .trust_effects import require_native_attempt

                        request_id = require_native_attempt(
                            run.caller,
                            observations_start=observation_index,
                            method="PATCH" if previous else "POST",
                            url=run.http.api_url + target,
                            body=json.dumps(
                                {"body": text}, separators=(",", ":")
                            ).encode("utf-8"),
                        )
                    else:
                        attempts = run.caller.observations[observation_index:]
                        writes = [item for item in attempts if item.get("mutation")]
                        if len(writes) != 1:
                            raise ReviewInputError(
                                "command receipt write identity is ambiguous"
                            )
                        request_id = str(writes[0]["request_id"])
                    # This settles the business outcome only. Accounting retains
                    # both the refresh and original uncertain native debit.
                    run.caller._reconciled_mutations.add(request_id)

            allocation.confirmed = True


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()
