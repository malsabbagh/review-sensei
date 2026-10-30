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


def claim_review_startup(
    directory: Path, run: ApiRunIdentity, attempt: int, job: str
) -> None:
    """Source-owned runner directory; claim before any ReviewSensei HTTP.

    Never remove this marker to retry. A failed process needs the one supported
    Actions rerun. A new runner under the same slot is unsupported, not recovery.
    """
    ApiExecutionOwner(attempt, job, 0)
    path = (
        directory
        / f"reviewsensei-api-{run.repository_id}-{run.run_id}-{attempt}-{job}.claim"
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

    def __init__(self, http, *, token: str, command_run_id: str, job: str = "hosted"):
        if job not in {"hosted", "local", "command"}:
            raise ReviewInputError("review owner job is unsupported")
        super().__init__(http, token=token, command_run_id=command_run_id)
        self.job = job

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
        self,
        identity: SessionIdentity,
        *,
        attempt: int,
        generation: int,
        event: str | None = None,
    ) -> ApiExecutionOwner:
        owner = ApiExecutionOwner(attempt, self.job, generation)
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
            or event is not None
            and run.get("event") != event
        ):
            raise ReviewInputError("current API run identity is unavailable")
        jobs = self._jobs(identity, attempt)
        matching = [j for j in jobs if _writer_name(j.get("name"), self.job)]
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

    def ceased_run_owner(
        self, identity: SessionIdentity, run_id: int, predecessor: ApiExecutionOwner
    ) -> tuple[object, ...]:
        if type(run_id) is not int or run_id < 1 or str(run_id) == self.command_run_id:
            raise ReviewInputError("previous Actions run identity conflicts")
        previous = ApiRunActionsEvidence(
            self.http, token=self.token, command_run_id=str(run_id), job=predecessor.job
        )
        run = previous._get(identity, f"/actions/runs/{run_id}")
        repository = run.get("repository")
        if (
            type(run.get("id")) is not int
            or run["id"] != run_id
            or not isinstance(repository, dict)
            or repository.get("id") != identity.repository_id
            or type(repository.get("id")) is not int
            or repository.get("full_name") != identity.repository
            or run.get("status") != "completed"
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] != predecessor.run_attempt
        ):
            raise ReviewInputError(
                "previous Actions run has not ceased or its identity conflicts"
            )
        return run_id, *previous.ceased_owner(identity, predecessor)

    def qualified_publication_cessation(
        self,
        identity: SessionIdentity,
        run_id: int,
        predecessor: ApiExecutionOwner,
        *,
        head_sha: str,
    ) -> dict[str, Any]:
        """Positive exit/source contract for historical root-write non-entry.

        Completion of a job alone never settles an uncertain request. This
        narrow source-qualified contract requires the actual review step to
        succeed and excludes every other possible root writer in that run.
        """
        import hashlib

        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.historical_completion import QUALIFIED_EXECUTORS

        if (
            type(run_id) is not int
            or run_id < 1
            or predecessor.run_attempt != 1
            or predecessor.generation != 0
            or predecessor.job not in {"hosted", "local"}
        ):
            raise ReviewInputError(
                "historical owner has no qualified root-write contract"
            )
        previous = ApiRunActionsEvidence(
            self.http, token=self.token, command_run_id=str(run_id), job=predecessor.job
        )
        # Read the latest run state, not merely a successful historical attempt:
        # a later rerun could still own an unsettled write to this same root.
        run = previous._get(identity, f"/actions/runs/{run_id}")
        repository = run.get("repository")
        if (
            type(run.get("id")) is not int
            or run.get("id") != run_id
            or not isinstance(repository, dict)
            or type(repository.get("id")) is not int
            or repository.get("id") != identity.repository_id
            or repository.get("full_name") != identity.repository
            or type(run.get("run_attempt")) is not int
            or run.get("run_attempt") != predecessor.run_attempt
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("head_sha") != head_sha
            or run.get("event") != "pull_request"
        ):
            raise ReviewInputError(
                "historical executor did not prove successful review exit"
            )
        references = run.get("referenced_workflows")
        if (
            not isinstance(references, list)
            or len(references) != 1
            or not isinstance(references[0], dict)
        ):
            raise ReviewInputError("historical executor source is unrecognized")
        source = references[0]
        source_sha = source.get("sha")
        contract = (
            QUALIFIED_EXECUTORS.get(source_sha) if isinstance(source_sha, str) else None
        )
        if (
            contract is None
            or source.get("path") != contract["workflow_path"]
            or source.get("ref") != contract["workflow_ref"]
        ):
            raise ReviewInputError("historical executor source is unrecognized")
        jobs = previous._jobs(identity, predecessor.run_attempt)
        selected = [j for j in jobs if _writer_name(j.get("name"), predecessor.job)]
        if len(selected) != 1:
            raise ReviewInputError("historical owning job is missing or ambiguous")
        job = selected[0]
        for kind in ("hosted", "local", "command"):
            matching = [j for j in jobs if _writer_name(j.get("name"), kind)]
            if (
                len(matching) != 1
                or matching[0].get("status") != "completed"
                or matching[0].get("conclusion")
                != ("success" if kind == predecessor.job else "skipped")
            ):
                raise ReviewInputError(
                    "historical complete writer inventory is unconfirmed"
                )
        steps = job.get("steps")
        if (
            not isinstance(steps, list)
            or not 1 <= len(steps) <= 64
            or any(not isinstance(step, dict) for step in steps)
        ):
            raise ReviewInputError("historical review step inventory is unavailable")
        review = [
            step
            for step in steps
            if step.get("name") == f"Run {predecessor.job} review"
        ]
        reply = [
            step
            for step in steps
            if step.get("name")
            == f"Generate and publish {predecessor.job} mention reply"
        ]
        if (
            len(review) != 1
            or len(reply) != 1
            or review[0].get("status") != "completed"
            or review[0].get("conclusion") != "success"
            or reply[0].get("status") != "completed"
            or reply[0].get("conclusion") != "skipped"
            or type(review[0].get("number")) is not int
            or review[0]["number"] < 1
        ):
            raise ReviewInputError(
                "historical review step did not prove successful exit"
            )
        completed = job.get("completed_at")
        step_completed = review[0].get("completed_at")
        if (
            not isinstance(completed, str)
            or not isinstance(step_completed, str)
            or _stamp(step_completed) > _stamp(completed)
        ):
            raise ReviewInputError("historical review exit chronology conflicts")
        projection = [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "run_id",
                    "run_attempt",
                    "name",
                    "status",
                    "conclusion",
                    "completed_at",
                    "steps",
                )
            }
            for item in jobs
        ]
        return {
            "run_id": run_id,
            "attempt": predecessor.run_attempt,
            "head_sha": head_sha,
            "job_id": job["id"],
            "step_number": review[0]["number"],
            "completed_at": completed,
            "executor_sha": source["sha"],
            "executor_commit": contract["commit"],
            "workflow_blob": contract["workflow_blob"],
            "writer_inventory_sha256": hashlib.sha256(
                canonical_bytes(projection)
            ).hexdigest(),
            "contract": contract["contract"],
        }

    def ceased_owner(
        self, identity: SessionIdentity, predecessor: ApiExecutionOwner
    ) -> tuple[int, str, str]:
        if predecessor.job != self.job:
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
