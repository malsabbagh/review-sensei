"""Host-derived pending receipts, independent of model prose and approval."""

from __future__ import annotations

import hashlib
import json
import string
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from review_sensei.errors import ReviewInputError
from review_sensei.presentation import assign_finding_identifiers, finding_identifier

from .approval import ReviewApprovalEligibility, evaluate_approval_facts

MAX_PENDING_PAGE_BYTES = 12 * 1024
MAX_PENDING_PAGES = 16


def _display(value: str, maximum: int = 160) -> str:
    # A short reason/location is presentation only; never shorten an identity.
    # Entities render punctuation literally in Markdown, including backticks
    # and links. Never truncate inside an entity or a UTF-8 codepoint.
    pieces: list[str] = []
    used = 0
    for character in " ".join(value.split()):
        if unicodedata.category(character) in {"Cc", "Cf"}:
            # Validated rationale text can retain direction/format controls.
            # Show their codepoint instead of letting them reorder authority.
            piece = f"U{ord(character):04X}"
        else:
            piece = (
                f"&#{ord(character)};" if character in string.punctuation else character
            )
        size = len(piece.encode("utf-8"))
        if used + size > maximum - 3:
            return "".join(pieces) + "..."
        pieces.append(piece)
        used += size
    return "".join(pieces)


@dataclass(frozen=True)
class PendingReplyPage:
    body: str
    report_digest: str
    index: int
    count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.body, str)
            or not self.body
            or len(self.body.encode("utf-8")) > MAX_PENDING_PAGE_BYTES
            or "<!-- reviewsensei:" in self.body
            or not isinstance(self.report_digest, str)
            or len(self.report_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.report_digest)
            or type(self.index) is not int
            or type(self.count) is not int
            or not 0 <= self.index < self.count <= MAX_PENDING_PAGES
        ):
            raise ReviewInputError("pending reply page is invalid")


def pending_reply_pages(
    *,
    body: str,
    head_sha: str,
    eligibility: ReviewApprovalEligibility | None,
    authority_digest: str = "",
    diagnostic: str | None = None,
    decision_reasons: Mapping[str, str] | None = None,
    assessment_digest: str = "",
    layout: str = "legacy",
) -> tuple[PendingReplyPage, ...]:
    """Keep every trusted unresolved human ID in a finite, deterministic report.

    Eligibility does not retain all non-human finding IDs or coverage paths.
    Explicitly report that boundary rather than reconstructing authority from
    review prose. Model text and diagnostic codes never choose pending IDs.
    """
    if (
        not isinstance(layout, str)
        or layout not in {"legacy", "shared-guidance-v1"}
        or not isinstance(head_sha, str)
        or len(head_sha) != 40
        or any(c not in "0123456789abcdef" for c in head_sha)
        or not isinstance(body, str)
        or not body
        or len(body.encode("utf-8")) > 4096
        or "<!-- reviewsensei:" in body
        or (eligibility is not None and eligibility.head_sha != head_sha)
    ):
        raise ReviewInputError("pending reply identity is invalid")
    lines = ["## Still pending"]
    inventory = eligibility.human_review if eligibility is not None else None
    reasons = dict(decision_reasons or {})
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or len(value.encode("utf-8")) > 1024
        for key, value in reasons.items()
    ) or (
        reasons
        and (
            inventory is None
            or not set(reasons) <= {item.fingerprint for item in inventory.findings}
        )
    ):
        raise ReviewInputError("pending decision reasons are invalid")
    if inventory is not None:
        identities = tuple(item.fingerprint for item in inventory.findings)
        try:
            identifiers = assign_finding_identifiers(identities)
        except ReviewInputError:
            # A legacy display-prefix collision cannot erase a trusted item.
            identifiers = {
                item: finding_identifier(item, hex_digits=64) for item in identities
            }
        shared_guidance = (
            (
                "Current evidence is insufficient."
                if diagnostic is not None
                else "These published concerns still require human assessment."
            )
            + " Next: provide current-head code and human evidence for each exact ID in a new authorized mention."
            if layout == "shared-guidance-v1" and inventory.pending
            else ""
        )
        if shared_guidance:
            lines[0] += "\n\n" + shared_guidance
        for item in sorted(inventory.pending, key=lambda value: value.fingerprint):
            identifier = identifiers[item.fingerprint]
            reason = (
                "Current evidence is insufficient."
                if diagnostic is not None
                else "This published concern still requires human assessment."
            )
            if reasons.get(item.fingerprint):
                reason = "Validated reassessment reason: " + _display(
                    reasons[item.fingerprint], 192
                )
            if layout == "shared-guidance-v1":
                lines.append(
                    f"- **{identifier}** (`{item.fingerprint}`), {_display(item.path)}"
                    + (": " + reason if reasons.get(item.fingerprint) else "")
                )
                continue
            lines.append(
                f"- **{identifier}** (`{item.fingerprint}`), "
                f"{_display(item.path)}: {reason} "
                "Next: provide current-head code and human evidence for this ID "
                "in a new authorized mention."
            )
        if not inventory.pending:
            lines.append("- No pending human-assessment findings in this inventory.")
    elif eligibility is None or eligibility.facts.has_human_adjudication_findings:
        lines.append(
            "- The complete current-head human inventory is unavailable. "
            "Next: rerun a full review before reassessment; no IDs can be safely inferred."
        )
    else:
        lines.append("- No pending human-assessment findings were recorded.")
    lines.append(
        "\nOnly the host-derived inventory above determines pending IDs. "
        "Any finding IDs mentioned in provider discussion below are explanatory "
        "prose, not current-head authority."
    )
    lines.append("\n## Coverage and other approval requirements")
    if eligibility is None:
        lines.append(
            "- Current-head review authority is unavailable. Next: rerun a full review."
        )
    else:
        facts = eligibility.facts
        static_blockers = set(evaluate_approval_facts(facts).blockers) - {
            "human-adjudication-open",
            "review-threads-incomplete",
            "review-threads-open",
        }
        if "auto-approval-disabled" in static_blockers:
            lines.append(
                "- Automatic approval is disabled by the trusted review policy. "
                "Next: use the repository's authorized human approval process."
            )
        if "app-authored-pull-request" in static_blockers:
            lines.append(
                "- This pull request is App-authored and cannot receive automatic "
                "approval. Next: obtain an independent authorized human review."
            )
        if facts.review_status != "complete" or facts.coverage_blocker not in (
            None,
            "reviewed",
        ):
            lines.append(
                f"- Review coverage is `{facts.review_status}` "
                f"(coverage state: `{facts.coverage_blocker or 'not recorded'}`). "
                "The authority record does not retain individual coverage paths. "
                "Next: consult the full review coverage and complete a current-head review."
            )
        if "review-unverified" in static_blockers:
            lines.append(
                "- Confirmed review evidence is unverified. Next: complete "
                "current-head evidence verification before approval."
            )
        if facts.has_blocking_findings:
            lines.append(
                "- Required findings remain. Their complete ID inventory is not "
                "retained in this authority record. Next: address the required "
                "findings and open review threads in the full current-head review."
            )
        if facts.qualification in {"missing", "unverified"}:
            lines.append(
                f"- Provider qualification is `{facts.qualification}`. "
                "Next: supply trusted qualification evidence for the effective configuration."
            )
        if facts.persistence_status is not None:
            lines.append(
                "- Review persistence is incomplete. Next: complete trusted "
                "current-head publication before approval."
            )
        if not static_blockers and facts.persistence_status is None:
            lines.append(
                "- No additional blocker was recorded here. Live thread and "
                "approval checks still apply."
            )
    lines.append("\nThis receipt does not grant approval or clear incomplete coverage.")
    report_digest = hashlib.sha256(
        json.dumps(
            {
                "head": head_sha,
                "body": body,
                "eligibility": eligibility.to_dict()
                if eligibility is not None
                else None,
                "authority_digest": authority_digest,
                "diagnostic": diagnostic,
                "assessment_digest": assessment_digest,
                "decision_reasons": reasons,
                "lines": lines,
                **({"layout": layout} if layout != "legacy" else {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    header = f"Reviewed head: `{head_sha}`\n\n"
    # Reserve navigation for the largest finite part count before any write.
    reserve = len(
        (
            header
            + f"Pending receipt page {MAX_PENDING_PAGES}/{MAX_PENDING_PAGES}.\n\n"
        ).encode()
    )
    groups: list[list[str]] = [[*lines[:1]]]
    used = reserve + len("\n\n".join(groups[0]).encode("utf-8"))
    for line in lines[1:]:
        size = len(line.encode("utf-8")) + 2
        if size + reserve > MAX_PENDING_PAGE_BYTES:
            raise ReviewInputError("one pending item exceeds page capacity")
        if used + size > MAX_PENDING_PAGE_BYTES:
            groups.append(["## Still pending (continued)"])
            used = reserve + len(groups[-1][0].encode())
        groups[-1].append(line)
        used += size
    discussion = "## Provider discussion\n\n" + body
    size = len(discussion.encode("utf-8")) + 2
    if used + size > MAX_PENDING_PAGE_BYTES:
        groups.append([])
    groups[-1].append(discussion)
    if len(groups) > MAX_PENDING_PAGES:
        raise ReviewInputError("complete pending receipt exceeds page capacity")
    return tuple(
        PendingReplyPage(
            header
            + f"Pending receipt page {index + 1}/{len(groups)}.\n\n"
            + "\n\n".join(group),
            report_digest,
            index,
            len(groups),
        )
        for index, group in enumerate(groups)
    )
