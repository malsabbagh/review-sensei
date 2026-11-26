"""Finite startup claim and live Actions owner checks for the candidate path.

The local claim prevents same-slot process restarts. It is NOT a distributed
lock or an authority record. All remote writers still require the trusted
workflow's existing PR concurrency serialization. This module adds no allocator.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from review_sensei.api_work import ApiExecutionOwner, ApiRunIdentity
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

from .reservation_recovery import ActionsReservationEvidence, _stamp

BUSINESS_WRITER_JOBS: Final[tuple[str, ...]] = ("hosted", "local", "command")
MAX_LATER_ATTEMPT_INSPECTION: Final[int] = 4
_MAX_LATER_ATTEMPT_INSPECTION: Final[int] = MAX_LATER_ATTEMPT_INSPECTION
_BUSINESS_EXECUTION_STEPS_BY_JOB: Final[dict[str, frozenset[str]]] = {
    "hosted": frozenset(
        {
            "Run hosted review",
            "Generate and publish hosted mention reply",
        }
    ),
    "local": frozenset(
        {
            "Run local review",
            "Generate and publish local mention reply",
        }
    ),
    "command": frozenset(
        {
            "Apply broker-attested maintainer command",
            "Run command",
        }
    ),
}
_STARTUP_CRASH_RUN_CONCLUSIONS: Final[frozenset[str]] = frozenset(
    {"failure", "cancelled", "timed_out", "startup_failure"}
)
_HEX40_RE = re.compile(r"[a-f0-9]{40}")
_TERMINAL_JOB_CONCLUSIONS: Final[frozenset[str]] = frozenset(
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


def is_terminal_job(row: dict[str, Any]) -> bool:
    """Return True when an Actions run or job row has completed with a terminal conclusion."""
    conclusion = row.get("conclusion")
    return (
        row.get("status") == "completed"
        and isinstance(conclusion, str)
        and conclusion in _TERMINAL_JOB_CONCLUSIONS
    )


_terminal_job = is_terminal_job


def later_attempt_proved_no_business_entry(later_jobs: list[dict[str, Any]]) -> bool:
    """Positively prove a later Actions attempt exited before Python business entry.

    ``later_jobs`` comes from the complete ``total_count``-validated job inventory
    for that attempt, so every ``hosted``/``local``/``command`` job that ran is
    present. Workflows that declare only a subset of ``BUSINESS_WRITER_JOBS``
    legitimately omit undeclared jobs, so validating every matched writer job
    with ``matched_writer_jobs > 0`` proves no writer entered Python business code.
    """
    matched_writer_jobs = 0
    seen_kinds: set[str] = set()
    for job in later_jobs:
        matched_kinds = [
            kind for kind in BUSINESS_WRITER_JOBS if _writer_name(job.get("name"), kind)
        ]
        if not matched_kinds:
            continue
        if len(matched_kinds) != 1:
            return False
        kind = matched_kinds[0]
        if kind in seen_kinds:
            return False
        seen_kinds.add(kind)
        matched_writer_jobs += 1
        if not is_terminal_job(job):
            return False
        conclusion = job.get("conclusion")
        steps = job.get("steps")
        recognized = _BUSINESS_EXECUTION_STEPS_BY_JOB[kind]
        if conclusion == "skipped":
            if steps is not None and not isinstance(steps, list):
                return False
            if isinstance(steps, list) and any(
                isinstance(step, dict)
                and step.get("name") in recognized
                and (
                    step.get("status") != "completed"
                    or step.get("conclusion") != "skipped"
                )
                for step in steps
            ):
                return False
            continue
        if conclusion == "success":
            return False
        if (
            not isinstance(steps, list)
            or not steps
            or any(not isinstance(step, dict) for step in steps)
        ):
            return False
        business_steps = [step for step in steps if step.get("name") in recognized]
        if not business_steps:
            return False
        if any(
            step.get("status") != "completed" or step.get("conclusion") != "skipped"
            for step in business_steps
        ):
            return False
    return matched_writer_jobs > 0


_later_attempt_proved_no_business_entry = later_attempt_proved_no_business_entry


class ApiRunActionsEvidence(ActionsReservationEvidence):
    """Read identity/cessation from authenticated GitHub, never status JSON.

    The owning adapter supplies the run ID returned by the verified broker and
    its separate read-only Actions capability. Generation remains ledger-owned.
    Only the finite hosted job is qualified by this bounded startup experiment.
    """

    def __init__(self, http, *, token: str, command_run_id: str, job: str = "hosted"):
        if job not in BUSINESS_WRITER_JOBS:
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

    def _current_owner_with_jobs(
        self,
        identity: SessionIdentity,
        *,
        attempt: int,
        generation: int,
        event: str | None = None,
        jobs: list[dict[str, Any]] | None = None,
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
        observed_jobs = self._jobs(identity, attempt) if jobs is None else jobs
        matching = [j for j in observed_jobs if _writer_name(j.get("name"), self.job)]
        # Conditional disabled sibling jobs report status="completed", conclusion="skipped".
        # Any non-terminal sibling writer (including queued/waiting/pending/requested) in the
        # same attempt is scheduled to run concurrently and must be refused as overlapping.
        active_writers = [
            j
            for j in observed_jobs
            if not is_terminal_job(j)
            and any(_writer_name(j.get("name"), n) for n in BUSINESS_WRITER_JOBS)
        ]
        if (
            len(matching) != 1
            or matching[0].get("status") != "in_progress"
            or active_writers != matching
        ):
            raise ReviewInputError("API writer ownership is ambiguous or overlapping")
        return owner

    def current_owner(
        self,
        identity: SessionIdentity,
        *,
        attempt: int,
        generation: int,
        event: str | None = None,
    ) -> ApiExecutionOwner:
        return self._current_owner_with_jobs(
            identity, attempt=attempt, generation=generation, event=event
        )

    def ceased_run_owner(
        self,
        identity: SessionIdentity,
        run_id: int,
        predecessor: ApiExecutionOwner,
        *,
        complete_terminal_inventory: bool = False,
        require_startup_crash_proof: bool = False,
    ) -> tuple[object, ...]:
        if type(run_id) is not int or run_id < 1 or str(run_id) == self.command_run_id:
            raise ReviewInputError("previous Actions run identity conflicts")
        if require_startup_crash_proof:
            complete_terminal_inventory = True
        previous = ApiRunActionsEvidence(
            self.http, token=self.token, command_run_id=str(run_id), job=predecessor.job
        )
        run = previous._get(identity, f"/actions/runs/{run_id}")
        repository = run.get("repository")
        latest_attempt = run.get("run_attempt")
        if (
            type(run.get("id")) is not int
            or run["id"] != run_id
            or not isinstance(repository, dict)
            or repository.get("id") != identity.repository_id
            or type(repository.get("id")) is not int
            or repository.get("full_name") != identity.repository
            or run.get("status") != "completed"
            or type(latest_attempt) is not int
            or latest_attempt < 1
        ):
            raise ReviewInputError(
                "previous Actions run has not ceased or its identity conflicts"
            )
        # Legacy same-attempt callers omitting "conclusion" are accepted only when
        # complete_terminal_inventory=False and no later attempt exists; any later
        # attempt, complete-inventory check, or explicit conclusion field must be terminal.
        require_terminal_run_conclusion = (
            complete_terminal_inventory
            or latest_attempt > predecessor.run_attempt
            or "conclusion" in run
        )
        if require_terminal_run_conclusion and not is_terminal_job(run):
            raise ReviewInputError(
                "previous Actions run has not ceased or its identity conflicts"
            )
        crashed_head_sha: str | None = None
        run_conclusion = run.get("conclusion")
        if require_startup_crash_proof:
            if (
                not isinstance(run_conclusion, str)
                or run_conclusion not in _STARTUP_CRASH_RUN_CONCLUSIONS
            ):
                raise ReviewInputError(
                    "previous Actions run did not terminate in a failed startup state"
                )
            head_sha_value = run.get("head_sha")
            if (
                not isinstance(head_sha_value, str)
                or _HEX40_RE.fullmatch(head_sha_value) is None
            ):
                raise ReviewInputError(
                    "previous Actions run head_sha is unavailable or invalid"
                )
            crashed_head_sha = head_sha_value
        if latest_attempt < predecessor.run_attempt:
            raise ReviewInputError(
                "previous Actions run attempt conflicts with predecessor"
            )
        if latest_attempt - predecessor.run_attempt > MAX_LATER_ATTEMPT_INSPECTION:
            raise ReviewInputError(
                "previous Actions run has too many later attempts to verify cessation"
            )
        predecessor_proof = previous.ceased_owner(
            identity,
            predecessor,
            complete_terminal_inventory=complete_terminal_inventory,
        )
        if require_startup_crash_proof and predecessor_proof[2] == "success":
            raise ReviewInputError(
                "previous Actions owning job did not terminate without success"
            )
        later_proofs: list[tuple[object, ...]] = []
        for later_attempt in range(predecessor.run_attempt + 1, latest_attempt + 1):
            later_jobs = previous._jobs(identity, later_attempt)
            incomplete_jobs = [
                j
                for j in later_jobs
                if (
                    complete_terminal_inventory
                    or any(_writer_name(j.get("name"), n) for n in BUSINESS_WRITER_JOBS)
                )
                and not is_terminal_job(j)
            ]
            if incomplete_jobs:
                raise ReviewInputError(
                    f"later Actions attempt {later_attempt} has not ceased"
                )
            if not later_attempt_proved_no_business_entry(later_jobs):
                raise ReviewInputError(
                    f"later Actions attempt {later_attempt} entered writer step with unconfirmed effects or charges"
                )
            later_proofs.append(
                (
                    later_attempt,
                    tuple(
                        (
                            j["id"],
                            str(j.get("name", "")),
                            str(j.get("conclusion", "")),
                            j.get("completed_at"),
                        )
                        for j in sorted(later_jobs, key=lambda row: row["id"])
                    ),
                )
            )
        proof: tuple[object, ...]
        if later_proofs:
            proof = (run_id, *predecessor_proof, tuple(later_proofs))
        else:
            proof = (run_id, *predecessor_proof)
        if require_startup_crash_proof:
            assert crashed_head_sha is not None
            return (*proof, crashed_head_sha, run_conclusion)
        return proof

    def startup_crash_head_sha(
        self,
        identity: SessionIdentity,
        run_id: int,
        predecessor: ApiExecutionOwner,
    ) -> str:
        proof = self.ceased_run_owner(
            identity,
            run_id,
            predecessor,
            complete_terminal_inventory=True,
            require_startup_crash_proof=True,
        )
        return str(proof[-2])

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
        mismatches: list[str] = []
        if type(run.get("id")) is not int or run.get("id") != run_id:
            mismatches.append(f"run_id={run.get('id')!r}")
        if (
            not isinstance(repository, dict)
            or type(repository.get("id")) is not int
            or repository.get("id") != identity.repository_id
            or repository.get("full_name") != identity.repository
        ):
            mismatches.append("repository_mismatch")
        if (
            type(run.get("run_attempt")) is not int
            or run.get("run_attempt") != predecessor.run_attempt
        ):
            mismatches.append(f"run_attempt={run.get('run_attempt')!r}")
        if run.get("status") != "completed":
            mismatches.append(f"status={run.get('status')!r}")
        if run.get("conclusion") != "success":
            mismatches.append(f"conclusion={run.get('conclusion')!r}")
        if run.get("head_sha") != head_sha:
            mismatches.append(
                f"head_sha_mismatch={run.get('head_sha')!r}!={head_sha!r}"
            )
        if run.get("event") != "pull_request":
            mismatches.append(f"event={run.get('event')!r}")
        if mismatches:
            raise ReviewInputError(
                "historical executor did not prove successful review exit ("
                f"run_id={run_id} status={run.get('status')!r} "
                f"conclusion={run.get('conclusion')!r} event={run.get('event')!r} "
                f"run_attempt={run.get('run_attempt')!r} "
                f"mismatches={','.join(mismatches)}); "
                "recover with `@reviewsensei review reenroll` or controlled restart"
            )
        references = run.get("referenced_workflows")
        if (
            not isinstance(references, list)
            or len(references) != 1
            or not isinstance(references[0], dict)
        ):
            raise ReviewInputError(
                "historical executor source is unrecognized ("
                f"run_id={run_id} referenced_workflows_invalid); "
                "recover with `@reviewsensei review reenroll` or controlled restart"
            )
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
            raise ReviewInputError(
                "historical executor source is unrecognized ("
                f"run_id={run_id} source_sha={source_sha!r} "
                f"workflow_ref={source.get('ref')!r} workflow_path={source.get('path')!r}); "
                "recover with `@reviewsensei review reenroll` or controlled restart"
            )
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
        self,
        identity: SessionIdentity,
        predecessor: ApiExecutionOwner,
        *,
        complete_terminal_inventory: bool = False,
    ) -> tuple[int, str, str]:
        if predecessor.job != self.job:
            raise ReviewInputError("API predecessor job is unsupported")
        jobs = self._jobs(identity, predecessor.run_attempt)
        matching = [j for j in jobs if _writer_name(j.get("name"), predecessor.job)]
        if len(matching) != 1:
            raise ReviewInputError("API predecessor job is missing or ambiguous")
        # On the GitHub Actions REST API, conditional sibling writer jobs whose
        # `if:` guard evaluated to false report `status == "completed"` with
        # `conclusion == "skipped"`. Any writer job with `status != "completed"`
        # (for example `queued`, `in_progress`, `waiting`, `pending`, or
        # `requested`) or without a terminal conclusion has not ceased.
        incomplete_jobs = [
            j
            for j in jobs
            if (
                complete_terminal_inventory
                or any(_writer_name(j.get("name"), n) for n in BUSINESS_WRITER_JOBS)
            )
            and not is_terminal_job(j)
        ]
        if incomplete_jobs:
            if complete_terminal_inventory:
                raise ReviewInputError(
                    "pre-entry abort predecessor job inventory has not ceased"
                )
            details = ", ".join(
                f"{j.get('name', '<unnamed>')}={j.get('status', '<unknown>')}/{j.get('conclusion')}"
                for j in incomplete_jobs
            )
            raise ReviewInputError(
                f"API predecessor has not ceased (incomplete writer jobs: {details})"
            )
        job = matching[0]
        conclusion = job.get("conclusion")
        if (
            job.get("status") != "completed"
            or not isinstance(conclusion, str)
            or conclusion not in _TERMINAL_JOB_CONCLUSIONS
        ):
            raise ReviewInputError("API predecessor has not ceased")
        stamp = job.get("completed_at")
        if not isinstance(stamp, str):
            raise ReviewInputError("API predecessor completion is unavailable")
        _stamp(stamp)
        return job["id"], stamp, conclusion

    def inspect_concurrency_posture(
        self,
        identity: SessionIdentity,
        *,
        attempt: int,
        event: str | None = None,
        previous_run_id: int | None = None,
        predecessor: ApiExecutionOwner | None = None,
    ) -> OwnerConcurrencyPosture:
        """Inspect Actions job exclusivity, cessation, and non-FIFO delivery.

        This is not ledger-generation or canonical-writer authority. The owner
        generation passed below only constructs a discarded owner value; job
        identity/exclusivity is checked against the Actions run and jobs API.
        Unavailable job evidence raises rather than reporting an empty job set.
        """

        current_run_int = int(self.command_run_id)
        non_fifo = previous_run_id is not None and previous_run_id > current_run_int
        # Missing jobs evidence is not evidence of writer absence. In particular,
        # a transient first read must not become an empty/clean overlap report
        # even if current_owner's later independent read succeeds.
        jobs = self._jobs(identity, attempt)
        # Conditional disabled sibling jobs report status="completed", conclusion="skipped".
        # Any non-terminal sibling writer (including queued/waiting/pending/requested) in the
        # same attempt is scheduled to run concurrently and must be refused as overlapping.
        active_writer_jobs = tuple(
            str(j.get("name", ""))
            for j in jobs
            if not is_terminal_job(j)
            and any(_writer_name(j.get("name"), n) for n in BUSINESS_WRITER_JOBS)
        )
        exclusive_owner = False
        refusal_reason: str | None = None
        try:
            self._current_owner_with_jobs(
                identity, attempt=attempt, generation=0, event=event, jobs=jobs
            )
            exclusive_owner = True
        except ReviewInputError as exc:
            refusal_reason = str(exc)
        predecessor_ceased: bool | None = None
        if previous_run_id is not None and predecessor is not None:
            try:
                self.ceased_run_owner(identity, previous_run_id, predecessor)
                predecessor_ceased = True
            except ReviewInputError as exc:
                predecessor_ceased = False
                if refusal_reason is None:
                    refusal_reason = str(exc)
        elif predecessor is not None:
            try:
                self.ceased_owner(identity, predecessor)
                predecessor_ceased = True
            except ReviewInputError as exc:
                predecessor_ceased = False
                if refusal_reason is None:
                    refusal_reason = str(exc)
        return OwnerConcurrencyPosture(
            command_run_id=current_run_int,
            job=self.job,
            attempt=attempt,
            active_writer_jobs=active_writer_jobs,
            overlapping_active_writers=len(active_writer_jobs) > 1,
            exclusive_active_owner=exclusive_owner,
            previous_run_id=previous_run_id,
            non_fifo_run_order=non_fifo,
            predecessor_ceased=predecessor_ceased,
            refusal_reason=refusal_reason,
        )


@dataclass(frozen=True)
class OwnerConcurrencyPosture:
    """Read-only concurrency qualification snapshot for one Actions writer owner."""

    command_run_id: int
    job: str
    attempt: int
    active_writer_jobs: tuple[str, ...]
    overlapping_active_writers: bool
    exclusive_active_owner: bool
    previous_run_id: int | None
    non_fifo_run_order: bool
    predecessor_ceased: bool | None
    refusal_reason: str | None
