"""Lossless fitting of known generated rows, never arbitrary published prose."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewComment
from review_sensei.presentation import (
    assign_finding_identifiers,
    build_finding_view,
    escape_markdown_label,
    render_finding_reference,
)

if TYPE_CHECKING:
    from .approval import ReviewApprovalEligibility
    from .publication import FindingIndex


def compact_command_references(
    body: str,
    *,
    eligibility: ReviewApprovalEligibility,
    index: FindingIndex,
    inline: list[Any],
    review_id: int,
    producer_id: int,
    app_slug: str,
    pull_request_url: str,
) -> str:
    """Compact only a complete, independently explained generated human list.

    Caller owns current root/head/base/ledger fences and transport accounting.
    Every unrecognized byte, including overview quotes and provenance, survives.
    Unsupported legacy renderings refuse rather than guessing their structure.
    """
    human = eligibility.human_review
    if human is None:
        raise ReviewInputError("command capacity requires a complete human inventory")
    # Summary rows precede the separately rendered full explanations. Never
    # search-and-replace a row in overview, provider narrative or detail prose.
    headings = list(re.finditer(r"(?m)^### Required decisions\n", body))
    if len(headings) != 1 or not body.startswith("## ReviewSensei — "):
        raise ReviewInputError("command capacity summary framing is ambiguous")
    start = headings[0].end()
    end = re.search(r"(?m)^#{1,3} ", body[start:])
    if end is None:
        raise ReviewInputError("command capacity summary framing is incomplete")
    stop = start + end.start()
    section = body[start:stop]
    explanations = list(
        re.finditer(r"(?m)^## Findings explained in this review body\n", body)
    )
    if len(explanations) != 1 or explanations[0].start() <= stop:
        raise ReviewInputError("command capacity explanation framing is ambiguous")
    prose_start = explanations[0].end()
    after_prose = re.search(r"(?m)^#{1,3} ", body[prose_start:])
    prose_stop = prose_start + after_prose.start() if after_prose else len(body)
    if re.search(r"(?m)^ {0,3}(?:`{3,}|~{3,})", body[:prose_stop]):
        raise ReviewInputError("command capacity fenced framing is unsupported")
    entries = {entry.instance: entry for entry in index.entries}
    if len(entries) != len(index.entries) or {
        finding.fingerprint for finding in human.findings
    } != {entry.instance for entry in index.entries if entry.needs_human}:
        raise ReviewInputError("command capacity human inventory is incomplete")
    labels = assign_finding_identifiers(entries)
    replacements: list[tuple[str, str]] = []
    original = section
    for finding in human.findings:
        entry = entries.get(finding.fingerprint)
        if entry is None or not entry.needs_human:
            raise ReviewInputError("command capacity inventory index conflicts")
        identifier = labels[finding.fingerprint]
        comment = finding.comment or ReviewComment(
            path=finding.path,
            body=finding.body,
            line=None,
            side="FILE",
            needs_human=True,
            effective_blocking=False,
        )
        view = build_finding_view(
            comment,
            fingerprint=entry.lifecycle or entry.instance,
            identifier=identifier,
            placement="body" if entry.body_placed else "inline",
        )
        reference = render_finding_reference(view)
        compact = f"- **{identifier}**"
        if section.count(reference) != 1:
            if section.count(compact + "\n") != 1:
                raise ReviewInputError("command capacity generated row is unrecognized")
            reference = compact
        detail = view.detail
        if not detail:
            raise ReviewInputError("command capacity original explanation is missing")
        # The legacy inventory lacks optional severity/title/line metadata, but
        # retains exact path and full explanation. Verify the canonical block's
        # path/detail/identifier together; never accept an arbitrary substring.
        if entry.body_placed:
            location = "`" + escape_markdown_label(finding.path) + "`"
            if location is None or not location.endswith("`"):
                raise ReviewInputError("command capacity body path is unavailable")
            block = (
                r"(?m)^\*\*Required decision[^\n]*\*\*\n\n"
                + re.escape(location[:-1])
                + r"(?::(?P<line>[1-9][0-9]*))?`\n\n"
                + re.escape(detail)
                + r"\n\n`"
                + re.escape(identifier)
                + r"`"
            )
            matches = list(re.finditer(block, body[prose_start:prose_stop]))
            if len(matches) != 1:
                raise ReviewInputError(
                    "command capacity body explanation is missing or ambiguous"
                )
            line = int(matches[0]["line"]) if matches[0]["line"] is not None else None
            sides = ("LEFT", "RIGHT") if line is not None else ("FILE", "LEFT", "RIGHT")
            if not any(
                finding_instance_fingerprint(
                    ReviewComment(
                        path=finding.path, body=finding.body, line=line, side=side
                    )
                )
                == entry.instance
                for side in sides
            ):
                raise ReviewInputError("command capacity body location conflicts")
        else:
            candidates = [
                item
                for item in inline
                if isinstance(item, dict)
                and item.get("pull_request_review_id") == review_id
                and item.get("commit_id") == eligibility.head_sha
                and item.get("pull_request_url") == pull_request_url
                and isinstance(item.get("user"), dict)
                and item["user"].get("id") == producer_id
                and type(item["user"].get("id")) is int
                and item["user"].get("type") == "Bot"
                and isinstance(item["user"].get("login"), str)
                and item["user"]["login"].casefold() == app_slug.casefold()
                and item.get("path") == finding.path
                and isinstance(item.get("body"), str)
                and type(item.get("line")) is int
                and item["line"] > 0
                and item.get("side") in {"LEFT", "RIGHT"}
                and finding_instance_fingerprint(
                    ReviewComment(
                        path=finding.path,
                        body=finding.body,
                        line=item["line"],
                        side=item["side"],
                    )
                )
                == entry.instance
                and re.search(
                    r"(?m)^\*\*Required decision[^\n]*\*\*\n\n"
                    + re.escape(detail)
                    + r"\n\n`"
                    + re.escape(identifier)
                    + r"`",
                    item["body"],
                )
                and re.search(
                    r"<!-- reviewsensei:instance:v1 instance="
                    + entry.instance
                    + " concern="
                    + re.escape(entry.lifecycle or "")
                    + " -->",
                    item["body"],
                )
            ]
            if len(candidates) != 1:
                raise ReviewInputError("command capacity inline explanation conflicts")
        replacements.append((reference, f"- **{identifier}**"))
        original = original.replace(reference, "", 1)
    if original.strip():
        raise ReviewInputError("command capacity summary contains unknown text")
    for reference, compact in replacements:
        section = section.replace(reference, compact, 1)
    return body[:start] + section + body[stop:]
