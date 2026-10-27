"""A distinct, current-authorized decision about a published historical hold.

Generic earlier-head findings remain ineligible for ordinary commands. This
target exists only when the current App review explicitly publishes the hold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from review_sensei.disposition import (
    MaintainerCommandResult,
    OverrideReceipt,
    authorized_override,
)
from review_sensei.errors import ReviewInputError
from review_sensei.historical_assessments import digest, normalize_hold

from .approval import ReviewApprovalEligibility
from .default_human_reader import transaction_publication_digest
from .publication import approval_eligibility_from_body, parse_finding_index


@dataclass(frozen=True)
class HistoricalAssessmentCommand:
    action: str
    scope: str
    reference: str
    reason: str
    submitted_reason: str
    finding_reference: None = None
    finding_references: tuple = ()


def parse_historical_command(body):
    from review_sensei.disposition import parse_maintainer_command

    parsed = parse_maintainer_command(body, actor="untrusted")
    if (
        parsed is None
        or re.match(r"historical[:/@_-]", parsed.reason or "", re.IGNORECASE | re.ASCII)
        is None
    ):
        return None
    match = re.fullmatch(
        r"historical:([a-f0-9]{64}) (.+)", parsed.reason or "", re.ASCII
    )
    if (
        match is None
        or parsed.finding_fingerprint is None
        or parsed.action not in {"dismiss", "accept-risk"}
    ):
        raise ReviewInputError(
            "historical assessment requires an exact scope and full instance fingerprint"
        )
    scope, reason = match.groups()
    if len(reason.encode()) < 16 or not reason.isprintable():
        raise ReviewInputError("historical assessment requires a substantive reason")
    return HistoricalAssessmentCommand(
        parsed.action,
        scope.lower(),
        "RS-" + parsed.finding_fingerprint.upper(),
        reason,
        parsed.reason or "",
    )


class HistoricalTargetRefusal(ReviewInputError):
    """A definite read-only precondition refusal before any decision write."""


@dataclass(frozen=True)
class HistoricalTarget:
    reader: object
    state_sha256: str
    instance: str
    hold: dict
    permission: str


def prepare_historical_target(run, authority, command, app, permission):
    if not authorized_override(
        association=authority.document["actor"]["association"], permission=permission
    ):
        raise HistoricalTargetRefusal(
            "historical assessment decision requires current maintain or admin permission"
        )
    reader = app.reviewer.finalizer.historical_reader(
        token=run.ledger.token,
        repository=run.identity.repository,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
    )
    if reader is None:
        raise ReviewInputError(
            "current review publishes no historical assessment target"
        )
    hold = reader.read(head_sha=run.head_sha)
    if hold["scope_sha256"] != command.scope:
        raise HistoricalTargetRefusal(
            "historical assessment scope does not match current publication"
        )
    prefix = command.reference[3:].lower()
    matches = [a for a in hold["assessments"] if a["instance"].startswith(prefix)]
    if len(matches) != 1:
        raise HistoricalTargetRefusal(
            "historical assessment target is missing or ambiguous"
        )
    instance = matches[0]["instance"]
    rows = run.http.paginate(
        path=run.http.repository_path(
            run.identity.repository, f"/pulls/{run.identity.pull_request}/reviews"
        ),
        token=run.ledger.token,
    )
    candidates = [
        r
        for r in rows
        if isinstance(r, dict)
        and r.get("commit_id") == run.head_sha
        and r.get("user", {}).get("id") == run.ledger._evidence_author_id
        and r.get("user", {}).get("type") == "Bot"
        and type(r.get("id")) is int
        and isinstance(r.get("body"), str)
        and "<!-- reviewsensei:eligibility:v1 " in r["body"]
    ]
    if not candidates:
        raise ReviewInputError(
            "current historical assessment publication is unavailable"
        )
    selected = max(candidates, key=lambda r: r["id"])
    # The source is the current review's explicit hold marker, never fallback
    # selection of an older review when the current index lacks an ID.
    eligibility = approval_eligibility_from_body(
        selected["body"],
        human_inventory_reader=app._human_inventory_reader(
            token=run.ledger.token,
            repository=run.identity.repository,
            pull_request=run.identity.pull_request,
            head_sha=run.head_sha,
            app_slug="reviewsensei[bot]",
            budget=run.caller.budget_view,
        ),
    )
    if (
        not isinstance(eligibility, ReviewApprovalEligibility)
        or eligibility.historical_assessments is None
    ):
        raise ReviewInputError(
            "current review has no explicit historical assessment marker"
        )
    reader.require(eligibility, head_sha=run.head_sha)
    loaded = run.ledger.load(run.identity)
    if (
        loaded.record is None
        or loaded.record.transaction is None
        or eligibility.result_digest
        != transaction_publication_digest(loaded.record.transaction)
    ):
        raise ReviewInputError("current historical assessment transaction conflicts")
    index = parse_finding_index(
        selected["body"],
        repository_id=run.identity.repository_id,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
    )
    if index is None:
        raise ReviewInputError(
            "current historical assessment finding index is unavailable"
        )
    if any(
        i.instance == instance
        or (i.lifecycle is not None and i.lifecycle == matches[0]["concern"])
        for i in index.entries
    ):
        raise HistoricalTargetRefusal(
            "historical assessment collides with a fresh finding; no decision applied"
        )
    authority.fence()
    return HistoricalTarget(reader, digest(hold), instance, hold, permission)


def apply_historical_target(run, authority, command, target, app):
    source, actor = authority.document["source"], authority.document["actor"]
    existing = next(
        (d for d in target.hold["settlements"] if d["instance"] == target.instance),
        None,
    )
    reference = "historical:" + command.scope + "/RS-" + target.instance.upper()
    if existing is None:
        decision = dict(
            instance=target.instance,
            action=command.action,
            actor=actor["login"],
            role=target.permission,
            head_sha=run.head_sha,
            source_id=source["id"],
            source_updated_at=source["updated_at"],
            source_body_sha256=source["body_sha256"],
            reason=command.submitted_reason,
            policy_sha256=run.caller.accounting.configuration.digest,
        )

        def mutate(record):
            hold = normalize_hold(record.historical_assessments)
            if hold is None or digest(hold) != target.state_sha256:
                raise ReviewInputError(
                    "historical assessment state changed before decision"
                )
            hold["settlements"].append(decision)
            return record.evolve(
                generation=record.generation + 1, historical_assessments=hold
            )

        authority.fence()
        confirmed = run.ledger.replace(run.identity, mutate)
        if confirmed.historical_assessments["settlements"][-1] != decision:
            raise ReviewInputError("historical assessment decision was not confirmed")
    else:
        confirmed = run.ledger.load(run.identity).record
    result = MaintainerCommandResult(
        action=command.action,
        applied=existing is None,
        operator_paused=confirmed.operator_paused,
        summary=(
            (
                "Historical assessment dismissed"
                if command.action == "dismiss"
                else "Historical assessment risk accepted"
            )
            + "; no independently verified fix is claimed"
            if existing is None
            else "Historical assessment already has a durable decision"
        ),
        receipts=(
            OverrideReceipt(
                reference,
                target.instance,
                "recorded" if existing is None else "already-recorded",
                command.action,
            ),
        ),
    )
    authority.fence()
    return app._refinalize_command(
        result,
        oidc_token=run.oidc,
        repository=run.identity.repository,
        repository_id=run.identity.repository_id,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
        app_slug="reviewsensei[bot]",
        actor=actor["login"],
        record=confirmed,
        ledger=run.ledger,
        evidence_budget=run.caller.budget_view,
        policy_digest=run.caller.accounting.configuration.digest,
    )
