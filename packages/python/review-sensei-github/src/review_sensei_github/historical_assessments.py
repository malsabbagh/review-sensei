"""Authenticate a durable historical assessment independently of fresh findings."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from review_sensei.errors import ReviewInputError
from review_sensei.historical_assessments import digest, hold_view, normalize_hold
from review_sensei.session import SessionIdentity

from .api_run_history import read_previous_run


class HistoricalAssessmentReader:
    def __init__(self, *, ledger, http, repository, pull_request, token):
        self.ledger, self.http = ledger, http
        self.repository, self.pull_request, self.token = repository, pull_request, token
        self._source = None

    def read(self, *, head_sha):
        caller = self.ledger.caller
        identity = SessionIdentity(
            self.repository, self.pull_request, caller.run.repository_id
        )
        caller._require_ledger(self.ledger, identity)
        loaded = self.ledger.load(identity)
        record = loaded.record
        if (
            loaded.status != "ok"
            or record is None
            or record.record_sha256 != caller._root
            or caller._checkpoint is not None
            or record.transaction is None
            or record.transaction.head_sha != head_sha
        ):
            raise ReviewInputError("historical assessment live caller changed")
        hold = normalize_hold(record.historical_assessments)
        if hold is None:
            raise ReviewInputError("historical assessment source disappeared")
        if self._source != hold["scope_sha256"]:
            source = hold["source"]
            previous = read_previous_run(self.ledger, identity, source)
            receipt = source["completion"]
            if previous.record_sha256 != receipt["historical_root_sha256"]:
                raise ReviewInputError("historical assessment archive changed")
            status, review = self.http.request(
                "GET",
                self.http.repository_path(
                    self.repository,
                    f"/pulls/{self.pull_request}/reviews/{receipt['review_id']}",
                ),
                token=self.token,
            )
            if (
                status != 200
                or not isinstance(review, dict)
                or review.get("id") != receipt["review_id"]
                or review.get("commit_id") != receipt["historical_head_sha"]
                or not isinstance(review.get("body"), str)
                or hashlib.sha256(review["body"].encode()).hexdigest()
                != receipt["review_body_sha256"]
                or review.get("user", {}).get("id") != self.ledger._evidence_author_id
                or review.get("user", {}).get("type") != "Bot"
            ):
                raise ReviewInputError("historical assessment native source conflicts")
            from .publication import parse_finding_index

            assert identity.repository_id is not None
            index = parse_finding_index(
                review["body"],
                repository_id=identity.repository_id,
                pull_request=self.pull_request,
                head_sha=receipt["historical_head_sha"],
            )
            expected = (
                sorted(
                    (
                        dict(instance=i.instance, concern=i.lifecycle)
                        for i in index.entries
                        if i.needs_human
                    ),
                    key=lambda i: str(i["instance"]),
                )
                if index
                else None
            )
            if (
                expected != hold["assessments"]
                or digest(expected) != receipt["human_inventory_sha256"]
            ):
                raise ReviewInputError(
                    "historical assessment original inventory conflicts"
                )
            self._source = hold["scope_sha256"]
        return hold

    def attach(self, eligibility, *, head_sha):
        view = hold_view(self.read(head_sha=head_sha))
        old = eligibility.historical_assessments
        fresh = (
            bool(eligibility.human_review.pending)
            if eligibility.human_review is not None
            else old["fresh_human_pending"]
            if old is not None
            else eligibility.facts.has_human_adjudication_findings
        )
        view["fresh_human_pending"] = fresh
        return replace(
            eligibility,
            historical_assessments=view,
            facts=replace(
                eligibility.facts,
                has_human_adjudication_findings=(fresh or bool(view["pending"])),
            ),
        )

    def require(self, eligibility, *, head_sha):
        actual = self.attach(eligibility, head_sha=head_sha)
        if actual.to_dict() != eligibility.to_dict():
            raise ReviewInputError(
                "historical assessment current publication is unconfirmed"
            )
        return actual


NOTICE_PREFIX = "<!-- reviewsensei:historical-assessments:v1 -->"
NOTICE_END = "<!-- reviewsensei:historical-assessments:end -->"


def render_hold_notice(eligibility):
    hold = eligibility.historical_assessments
    if hold is None:
        return ""
    rows = [
        NOTICE_PREFIX,
        "### Historical assessments",
        "",
        f"[Earlier review](#pullrequestreview-{hold['review_id']}) at `{hold['historical_head_sha'][:7]}`. "
        "These are pending assessments from that review, not fresh findings or verified defects.",
        "",
    ]
    if hold["pending"]:
        rows.extend("- `RS-" + instance.upper() + "`" for instance in hold["pending"])
        rows.extend(
            [
                "",
                "Approval stays blocked until each assessment has an explicit authorized decision. "
                "Thread resolution or a later model's omission does not settle it.",
                "",
                "Use the full fingerprint from the list with `dismiss` for an evidence-backed rejection "
                "or `accept-risk` for explicit risk acceptance:",
                "",
                '`@reviewsensei dismiss <full fingerprint> --reason "historical:'
                + hold["scope_sha256"]
                + ' <rationale>"`',
                "",
                "This decision settles only the named historical assessment; it does not verify a fix "
                "or approve fresh findings or incomplete coverage.",
            ]
        )
    else:
        rows.append(
            "The historical assessments have durable explicit decisions. Fresh findings and coverage retain their normal gates."
        )
    return "\n".join(rows + [NOTICE_END])


def remove_hold_notice(text):
    import re

    return re.sub(
        re.escape(NOTICE_PREFIX) + r".*?" + re.escape(NOTICE_END),
        "",
        text,
        flags=re.DOTALL,
    )
