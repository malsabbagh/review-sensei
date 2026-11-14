"""One exact App-owned maintainer receipt inside the active action allowance."""

from __future__ import annotations

import hashlib
from typing import Any

from review_sensei.errors import ReviewInputError
from review_sensei.presentation import render_command_receipt

from .errors import GitHubHTTPTransientError, GitHubPublicationTransientError


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

        def matching():
            values = (
                run.http.paginate(path=path, token=token)
                if bounded_receipt is None
                else bounded_receipt.read(path=path, token=token)
            )
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
        from .publication_recovery import _publication_transport

        guard = getattr(
            _publication_transport(run.http), "_quarantine_capacity_guard", None
        )
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
        from .trust_effects import verification_profile

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
            COMMAND_RECEIPT_CONFIRMATION_REQUESTS
            if bounded_receipt is None
            else bounded_receipt.confirmation_requests()
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
                matches = matching()
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
