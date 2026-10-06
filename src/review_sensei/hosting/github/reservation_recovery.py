"""Live, bounded Actions evidence for authenticated session recovery.

This is deliberately an adapter concern. Completion proofs come from GitHub,
not a caller-supplied status file or a lease timeout.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ...errors import ReviewInputError
from ...session import SessionIdentity, SessionRecord
from .http import GitHubHttp

_TERMINAL_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "neutral",
        "skipped",
        "startup_failure",
        "stale",
    }
)
_LEGACY_CALLER = ".github/workflows/review-sensei-review.yml"


def _stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReviewInputError("reservation recovery timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewInputError("reservation recovery timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewInputError("reservation recovery timestamp is invalid")
    return parsed


class ActionsReservationEvidence:
    """Use a separate read-only Actions token; mutation stays broker-owned."""

    def __init__(self, http: GitHubHttp, *, token: str, command_run_id: str):
        if (
            not isinstance(token, str)
            or not token.strip()
            or not isinstance(command_run_id, str)
            or re.fullmatch(r"[1-9][0-9]{0,18}", command_run_id) is None
        ):
            raise ReviewInputError(
                "reservation recovery requires authenticated Actions reads"
            )
        self.http, self.token, self.command_run_id = http, token, command_run_id

    def _get(self, identity: SessionIdentity, suffix: str) -> dict[str, Any]:
        status, payload = self.http.request(
            "GET",
            self.http.repository_path(identity.repository, suffix),
            token=self.token,
        )
        if status != 200 or not isinstance(payload, dict):
            raise ReviewInputError(
                "reservation recovery Actions evidence is unavailable"
            )
        return payload

    def _runs(self, identity: SessionIdentity, query: str) -> list[dict[str, Any]]:
        # Actions envelopes are not bare-list pagination. Verify total_count
        # and refuse truncation rather than silently accepting a partial proof.
        payload = self._get(identity, f"/actions/runs?{query}&per_page=100&page=1")
        items, total = payload.get("workflow_runs"), payload.get("total_count")
        if (
            not isinstance(items, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or total > 100
            or len(items) != total
            or any(not isinstance(item, dict) for item in items)
        ):
            raise ReviewInputError("reservation recovery run discovery is incomplete")
        return items

    def _completed_run(self, identity: SessionIdentity, run_id: str) -> dict[str, Any]:
        run = self._get(identity, f"/actions/runs/{run_id}")
        repository = run.get("repository")
        if (
            run.get("id") != int(run_id)
            or not isinstance(repository, dict)
            or repository.get("id") != identity.repository_id
            or repository.get("full_name") != identity.repository
            or run.get("status") != "completed"
            or not isinstance(run.get("conclusion"), str)
            or run.get("conclusion") not in _TERMINAL_CONCLUSIONS
            or isinstance(run.get("run_attempt"), bool)
            or not isinstance(run.get("run_attempt"), int)
            or run["run_attempt"] < 1
        ):
            raise ReviewInputError(
                "reservation owner run is active or its identity is invalid"
            )
        return run

    def verify(
        self, identity: SessionIdentity, record: SessionRecord
    ) -> tuple[str, int]:
        if record.reservation_id is None or record.reserved_slot not in {
            "initial",
            "verification",
        }:
            raise ReviewInputError("reservation recovery requires an analysis hold")
        if record.transaction is not None and record.transaction.phase != "analysis":
            raise ReviewInputError(
                "reservation recovery cannot discard publication work"
            )
        if record.reservation_owner is not None:
            run_id = str(record.reservation_owner["run_id"])
            if run_id == self.command_run_id:
                raise ReviewInputError("a command cannot reclaim its own active run")
            run = self._completed_run(identity, run_id)
        else:
            # A constrained compatibility proof for pre-owner pull_request
            # runs: exact reviewed head, PR association, approved caller path,
            # unique run interval, and one failed analysis job containing the
            # ledger's write timestamp. Unknown/manual/ambiguous origins stay
            # blocked and require investigation, never an age-based unlock.
            if record.transaction is None:
                raise ReviewInputError(
                    "legacy reservation has no provable analysis origin"
                )
            head = record.transaction.head_sha
            candidates = []
            at = _stamp(record.updated_at)
            for item in self._runs(identity, f"head_sha={head}"):
                prs = item.get("pull_requests")
                if (
                    item.get("event") == "pull_request"
                    and item.get("path") == _LEGACY_CALLER
                    and item.get("head_sha") == head
                    and isinstance(prs, list)
                    and any(
                        isinstance(pr, dict)
                        and pr.get("number") == identity.pull_request
                        for pr in prs
                    )
                    and _stamp(item.get("run_started_at"))
                    <= at
                    <= _stamp(item.get("updated_at"))
                ):
                    candidates.append(item)
            if len(candidates) != 1:
                raise ReviewInputError(
                    "legacy reservation origin is missing or ambiguous"
                )
            run_id = str(candidates[0].get("id"))
            if re.fullmatch(r"[1-9][0-9]{0,18}", run_id) is None:
                raise ReviewInputError("legacy reservation origin is invalid")
            run = self._completed_run(identity, run_id)
            if (
                run.get("head_sha") != head
                or run.get("event") != "pull_request"
                or run.get("path") != _LEGACY_CALLER
                or run.get("conclusion") not in {"failure", "cancelled", "timed_out"}
            ):
                raise ReviewInputError("legacy reservation origin no longer matches")
            jobs = self._get(
                identity,
                f"/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100&page=1",
            )
            items, count = jobs.get("jobs"), jobs.get("total_count")
            if (
                not isinstance(items, list)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count != len(items)
                or count > 100
            ):
                raise ReviewInputError("legacy reservation job discovery is incomplete")
            matching = []
            for job in items:
                if not isinstance(job, dict) or job.get("status") != "completed":
                    raise ReviewInputError(
                        "legacy reservation job is active or invalid"
                    )
                if (
                    job.get("run_id") == int(run_id)
                    and job.get("run_attempt") == run["run_attempt"]
                    and job.get("head_sha") == head
                    and isinstance(job.get("name"), str)
                    and job["name"].endswith((" / hosted", " / local"))
                    and job.get("conclusion") in {"failure", "cancelled", "timed_out"}
                    and _stamp(job.get("started_at"))
                    <= at
                    <= _stamp(job.get("completed_at"))
                ):
                    matching.append(job)
            if len(matching) != 1:
                raise ReviewInputError(
                    "legacy reservation analysis job is missing or ambiguous"
                )
        # Re-read latest attempt. A rerun that started during proof collection
        # invalidates even a previously completed attempt.
        latest = self._completed_run(identity, run_id)
        if latest["run_attempt"] != run["run_attempt"] or latest.get(
            "updated_at"
        ) != run.get("updated_at"):
            raise ReviewInputError("reservation recovery run proof is stale")
        return run_id, latest["run_attempt"]
