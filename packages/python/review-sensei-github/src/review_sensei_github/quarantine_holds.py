"""Current authoritative quarantine holds for every approval consumer."""

from __future__ import annotations

import re
from dataclasses import replace

from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import assessment_view
from review_sensei.session import SessionIdentity

from .quarantine_origin import QuarantineOriginReader


class QuarantineHoldReader:
    def __init__(self, *, ledger, repository, pull_request):
        self.ledger = ledger
        self.repository, self.pull_request = repository, pull_request

    def read(self, *, head_sha):
        caller = self.ledger.caller
        identity = SessionIdentity(
            self.repository, self.pull_request, caller.run.repository_id
        )
        caller._require_ledger(self.ledger, identity)
        loaded = self.ledger.load(identity)
        record = loaded.record
        reader = getattr(self.ledger, "_quarantine_origin_reader", None)
        if (
            loaded.status != "ok"
            or record is None
            or record.record_sha256 != caller._root
            or caller._checkpoint is not None
            or record.transaction is None
            or record.transaction.head_sha != head_sha
            or type(reader) is not QuarantineOriginReader
        ):
            raise ReviewInputError("quarantine assessment current authority changed")
        reader.current_scope.require()
        authenticated = getattr(self.ledger, "_quarantine_authenticated", None)
        if authenticated is None or authenticated.record_sha256 != record.record_sha256:
            raise ReviewInputError(
                "quarantine complete current origin readback is unavailable"
            )
        self._last_record = record
        return authenticated

    def record_after_read(self):
        """Reuse only the record just authenticated by this reader's full load."""
        record = getattr(self, "_last_record", None)
        caller = self.ledger.caller
        authenticated = getattr(self.ledger, "_quarantine_authenticated", None)
        if (
            record is None
            or authenticated is None
            or caller._checkpoint is not None
            or record.record_sha256 != caller._root
            or authenticated.record_sha256 != record.record_sha256
        ):
            raise ReviewInputError("quarantine just-read record changed")
        return record

    def attach(self, eligibility, *, head_sha):
        authenticated = self.read(head_sha=head_sha)
        fresh = eligibility.fresh_human_pending
        view = assessment_view(
            authenticated.archive["assessment_origins"],
            authenticated.inventories,
            authenticated.state,
            scope=authenticated.lineage["scope_sha256"],
            fresh_human_pending=fresh,
        )
        return replace(
            eligibility,
            quarantine_assessments=view,
            facts=replace(
                eligibility.facts,
                has_human_adjudication_findings=(
                    fresh
                    or bool(view["pending"])
                    or bool((eligibility.historical_assessments or {}).get("pending"))
                ),
            ),
        )

    def require(self, eligibility, *, head_sha):
        actual = self.attach(eligibility, head_sha=head_sha)
        if actual.to_dict() != eligibility.to_dict():
            raise ReviewInputError(
                "quarantine assessment current publication is unconfirmed"
            )
        return actual


NOTICE_PREFIX = "<!-- reviewsensei:quarantine-assessments:v1 -->"
NOTICE_END = "<!-- reviewsensei:quarantine-assessments:end -->"


def render_hold_notice(eligibility):
    view = eligibility.quarantine_assessments
    if view is None:
        return ""
    rows = [
        NOTICE_PREFIX,
        "### Carried historical assessments",
        "",
        "The predecessor remains unresolved. These assessments are independent of fresh findings.",
        "",
    ]
    for origin in view["origins"]:
        pending = [
            item["instance"]
            for item in view["pending"]
            if item["origin_scope_sha256"] == origin["scope_sha256"]
        ]
        rows.extend([origin["kind"] + " origin `" + origin["scope_sha256"] + "`", ""])
        rows.extend("- `RS-" + item.upper() + "`" for item in pending)
        if not pending:
            rows.append(
                "All assessments in this origin have explicit individual decisions."
            )
        rows.append("")
    if view["pending"]:
        rows.extend(
            [
                "Approval remains blocked. Each assessment requires an authorized `dismiss` or `accept-risk` decision with the full fingerprint and an evidence-backed reason.",
                "",
                '`@reviewsensei dismiss <full fingerprint> --reason "quarantine:<full origin scope> <rationale>"`',
                "",
                "One decision settles only the named origin and assessment. Fresh findings, coverage and threads retain their own gates.",
            ]
        )
    return "\n".join(rows + [NOTICE_END])


def remove_hold_notice(text):
    return re.sub(
        re.escape(NOTICE_PREFIX) + r".*?" + re.escape(NOTICE_END),
        "",
        text,
        flags=re.DOTALL,
    )
