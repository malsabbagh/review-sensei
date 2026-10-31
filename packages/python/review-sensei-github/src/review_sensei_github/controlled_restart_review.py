"""Optional offline reviewer worksheet; no API, disposition, grant or approval.

The archive and published body files are untrusted evidence supplied by a human.
Exact marker/index checks prevent accidental cross-session mapping; they do not
authenticate a live GitHub inventory. Normal commands still require their own
fresh, authenticated current-head discovery and explicit authorization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.presentation import assign_finding_identifiers

from .controlled_restart import MAX_ARCHIVE_BYTES, read_registry
from .publication import (
    MAX_INDEXED_FINDINGS,
    MAX_PUBLISHED_REVIEW_BODY_BYTES,
    parse_finding_index,
)


def review_handoff(
    *,
    archive: bytes,
    prior_body: str,
    prior_head: str,
    current_body: str,
    notes: list[dict],
) -> dict:
    if len(archive) > MAX_ARCHIVE_BYTES or any(
        len(body.encode()) > MAX_PUBLISHED_REVIEW_BODY_BYTES
        for body in (prior_body, current_body)
    ):
        raise ReviewInputError("restart reviewer evidence exceeds its existing bounds")
    document = json.loads(archive)
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != "controlled-restart-archive-v1"
        or document.get("status") != "abandoned"
        or document.get("historical_cost") != "unknown"
        or document.get("historical_effects") != "unsettled"
    ):
        raise ReviewInputError("restart reviewer archive is not abandoned history")
    rows = read_registry(
        canonical_bytes(
            dict(
                schema_version="controlled-restart-registry-v1",
                registrations=[document.get("registration")],
            )
        )
    )
    row = rows[0]
    if re.fullmatch(r"[a-f0-9]{40}", prior_head) is None:
        raise ReviewInputError("historical review head is invalid")
    marker = f"<!-- reviewsensei:controlled-restart:v1 request={row['request_id']} archive={hashlib.sha256(archive).hexdigest()} -->"
    if not current_body.endswith("\n" + marker):
        raise ReviewInputError("reviewer successor marker or archive bytes differ")

    def index(body, head):
        value = parse_finding_index(
            body,
            repository_id=row["repository_id"],
            pull_request=row["pull_request"],
            head_sha=head,
        )
        if value is None:
            raise ReviewInputError(
                "reviewer finding inventory is unavailable for this head"
            )
        return value

    old, current = index(prior_body, prior_head), index(current_body, row["head_sha"])
    if current.base_sha != row["base_sha"]:
        raise ReviewInputError("reviewer successor base changed")
    if (
        not isinstance(notes, list)
        or len(notes) > MAX_INDEXED_FINDINGS
        or len(canonical_bytes(notes)) > MAX_ARCHIVE_BYTES
    ):
        raise ReviewInputError("reviewer notes exceed their bounded inventory")
    prior = {entry.instance: entry for entry in old.entries}
    decisions = {}
    for note in notes:
        if (
            not isinstance(note, dict)
            or set(note) != {"instance", "decision", "reference", "rationale"}
            or not isinstance(note["instance"], str)
            or note["instance"] not in prior
            or note["instance"] in decisions
            or not isinstance(note["decision"], str)
            or note["decision"] not in {"fixed", "not-applicable", "advisory-deferred"}
            or not isinstance(note["reference"], str)
            or not note["reference"].startswith(
                f"https://github.com/{row['repository']}/pull/{row['pull_request']}#"
            )
            or not isinstance(note["rationale"], str)
            or not note["rationale"].strip()
        ):
            raise ReviewInputError("reviewer note is foreign, duplicate or unsupported")
        decisions[note["instance"]] = dict(note)
    identifiers = assign_finding_identifiers(
        entry.instance for entry in current.entries
    )
    mappings = []
    for entry in old.entries:
        exact = [e for e in current.entries if e.instance == entry.instance]
        related = [
            e
            for e in current.entries
            if entry.lifecycle is not None and e.lifecycle == entry.lifecycle
        ]
        matches = exact or related
        selected = matches[0] if len(matches) == 1 else None
        relationship = (
            (
                "same-instance-requires-review"
                if exact
                else "same-concern-requires-review"
            )
            if selected
            else ("ambiguous" if matches else "not-observed-in-successor")
        )
        mappings.append(
            dict(
                prior_instance=entry.instance,
                prior_head=prior_head,
                prior_decision=decisions.get(entry.instance),
                relationship=relationship,
                current_instance=selected.instance if selected else None,
                current_identifier=identifiers[selected.instance] if selected else None,
                successor_disposition=None,
            )
        )
    return dict(
        schema_version="controlled-restart-review-worksheet-v1",
        request_id=row["request_id"],
        repository=row["repository"],
        pull_request=row["pull_request"],
        current_head=row["head_sha"],
        mappings=mappings,
        authenticated_live_inventory=False,
        approval_allowed=False,
        historical_cost="unknown",
        historical_effects="unsettled",
        next_action="qualified-current-head-human-review",
    )


def _read(path: Path, bound: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(bound + 1)
    if len(value) > bound:
        raise ReviewInputError("restart reviewer input exceeds its bound")
    return value


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "prior-body", "current-body"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--prior-head", required=True)
    parser.add_argument("--notes", type=Path)
    args = parser.parse_args(arguments)
    result = review_handoff(
        archive=_read(args.archive, MAX_ARCHIVE_BYTES),
        prior_body=_read(args.prior_body, MAX_PUBLISHED_REVIEW_BODY_BYTES).decode(),
        prior_head=args.prior_head,
        current_body=_read(args.current_body, MAX_PUBLISHED_REVIEW_BODY_BYTES).decode(),
        notes=json.loads(_read(args.notes, MAX_ARCHIVE_BYTES)) if args.notes else [],
    )
    print(json.dumps(result, indent=2))
    return 0
