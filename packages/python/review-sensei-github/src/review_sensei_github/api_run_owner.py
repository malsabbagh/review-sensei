"""Finite startup claim and live Actions owner checks for the candidate path.

The local claim prevents same-slot process restarts. It is NOT a distributed
lock or an authority record. All remote writers still require the trusted
workflow's existing PR concurrency serialization. This module adds no allocator.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from review_sensei.api_work import ApiExecutionOwner, ApiRunIdentity
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

from .reservation_recovery import ActionsReservationEvidence, _stamp


def claim_hosted_startup(directory: Path, run: ApiRunIdentity, attempt: int) -> None:
    """Source-owned runner directory; claim before any ReviewSensei HTTP.

    Never remove this marker to retry. A failed process needs the one supported
    Actions rerun. A new runner under the same slot is unsupported, not recovery.
    """
    ApiExecutionOwner(attempt, "hosted", 0)
    path = (
        directory
        / f"reviewsensei-api-{run.repository_id}-{run.run_id}-{attempt}-hosted.claim"
    )
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"claimed\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ReviewInputError("API startup slot is consumed or unavailable") from exc


def _writer_name(name: object, job: str) -> bool:
    return isinstance(name, str) and (name == job or name.endswith(" / " + job))


class ApiRunActionsEvidence(ActionsReservationEvidence):
    """Read identity/cessation from authenticated GitHub, never status JSON.

    The owning adapter supplies the run ID returned by the verified broker and
    its separate read-only Actions capability. Generation remains ledger-owned.
    Only the finite hosted job is qualified by this bounded startup experiment.
    """

    def _jobs(self, identity: SessionIdentity, attempt: int) -> list[dict[str, Any]]:
        payload = self._get(
            identity,
            f"/actions/runs/{self.command_run_id}/attempts/{attempt}/jobs?per_page=100&page=1",
        )
        jobs, total = payload.get("jobs"), payload.get("total_count")
        if (
            not isinstance(jobs, list)
            or type(total) is not int
            or not 0 <= total <= 100
            or total != len(jobs)
            or any(
                not isinstance(j, dict)
                or type(j.get("id")) is not int
                or j["id"] < 1
                or type(j.get("run_id")) is not int
                or j["run_id"] != int(self.command_run_id)
                or type(j.get("run_attempt")) is not int
                or j["run_attempt"] != attempt
                for j in jobs
            )
            or len({j["id"] for j in jobs}) != len(jobs)
        ):
            raise ReviewInputError("API owner job inventory is incomplete or invalid")
        return jobs

    def current_owner(
        self, identity: SessionIdentity, *, attempt: int, generation: int
    ) -> ApiExecutionOwner:
        owner = ApiExecutionOwner(attempt, "hosted", generation)
        run = self._get(identity, f"/actions/runs/{self.command_run_id}")
        repository = run.get("repository")
        if (
            type(run.get("id")) is not int
            or run["id"] != int(self.command_run_id)
            or not isinstance(repository, dict)
            or type(repository.get("id")) is not int
            or repository["id"] != identity.repository_id
            or repository.get("full_name") != identity.repository
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] != attempt
            or run.get("status") != "in_progress"
        ):
            raise ReviewInputError("current API run identity is unavailable")
        jobs = self._jobs(identity, attempt)
        matching = [j for j in jobs if _writer_name(j.get("name"), "hosted")]
        active_writers = [
            j
            for j in jobs
            if j.get("status") == "in_progress"
            and any(
                _writer_name(j.get("name"), n) for n in ("hosted", "local", "command")
            )
        ]
        if (
            len(matching) != 1
            or matching[0].get("status") != "in_progress"
            or active_writers != matching
        ):
            raise ReviewInputError("API writer ownership is ambiguous or overlapping")
        return owner

    def ceased_owner(
        self, identity: SessionIdentity, predecessor: ApiExecutionOwner
    ) -> tuple[int, str, str]:
        if predecessor.job != "hosted":
            raise ReviewInputError("API predecessor job is unsupported")
        jobs = self._jobs(identity, predecessor.run_attempt)
        matching = [j for j in jobs if _writer_name(j.get("name"), predecessor.job)]
        if len(matching) != 1:
            raise ReviewInputError("API predecessor job is missing or ambiguous")
        job = matching[0]
        conclusion = job.get("conclusion")
        if (
            job.get("status") != "completed"
            or not isinstance(conclusion, str)
            or conclusion
            not in {
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
        ):
            raise ReviewInputError("API predecessor has not ceased")
        stamp = job.get("completed_at")
        if not isinstance(stamp, str):
            raise ReviewInputError("API predecessor completion is unavailable")
        _stamp(stamp)
        return job["id"], stamp, conclusion
