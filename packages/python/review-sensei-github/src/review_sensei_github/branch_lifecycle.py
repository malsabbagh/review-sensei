"""Pull-request reopen, branch deletion/recreation, and live write gating (G05).

This module formalizes the stateful GitHub pull-request lifecycle contract
around close/reopen transitions and deleted/recreated head branches:

1. Session and obligation identity is bound strictly to
   ``(repository_id, pull_request)`` via ``SessionIdentity``, never to the
   mutable ``head.ref`` branch name. Reopening the same numeric pull request
   preserves prior obligations, convergence history, and head-scoped failed
   attempts, whereas opening a different numeric pull request on a recreated
   branch starts an isolated session without cross-PR leakage.
2. Pre-effect live pull-request gates block newly admitted review/approval
   writes while ``state != "open"``, ``draft is True``, or ``head.repo`` is
   absent/mismatched, while never claiming atomic revocation of requests that
   were already dispatched before a concurrent transition (the G02 boundary).
3. Same-SHA reopen or same-SHA branch recreation mints zero fresh
   ``failed_attempts`` allowance and reuses already-published or retained
   snapshot state without duplicate model inference.
4. Reopening or recreating a branch at a different ``head_sha`` or ``base_sha``
   invalidates prior snapshot-bound approvals while preserving unresolved
   baseline obligations for verification/rebinding on the new snapshot.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from review_sensei.session import (
    SessionIdentity,
    SessionRecord,
    failed_attempts_for_head,
    read_session_baseline,
)

from .errors import GitHubPublicationError

_GIT_SHA_HEX = re.compile(r"^[0-9a-f]{40}$")

ReopenTransitionKind = Literal[
    "distinct_pull_request_on_recreated_branch",
    "closed_blocking_writes",
    "head_branch_deleted_blocking_writes",
    "branch_recreated_same_sha",
    "branch_recreated_changed_sha",
    "reopened_same_sha",
    "reopened_changed_sha",
    "duplicate_open_delivery",
    "open_head_or_base_changed",
]

LifecycleRecoveryAction = Literal[
    "isolated_new_pull_request_session",
    "await_reopen_preserve_ledger",
    "restore_head_branch_or_reopen",
    "reuse_completed_or_retained_snapshot",
    "rebind_obligations_and_verify_new_snapshot",
]


@dataclass(frozen=True)
class LiveWriteGateDecision:
    """Result of validating a live pull-request payload before a native write."""

    admitted: bool
    skip_status: str | None
    reason: str
    app_authored: bool = False
    revokes_in_flight_dispatches: bool = False


def evaluate_live_pr_write_gate(
    payload: object,
    *,
    repository: str,
    repository_id: int,
    head_sha: str,
    base_branch: str,
    base_sha: str,
    app_slug: str,
) -> LiveWriteGateDecision:
    """Validate live PR state before admitting a GitHub publication write.

    Enforces exact-boolean ``draft`` validation (G24), open-state admission,
    same-repository non-fork head/base checks (including deleted ``head.repo``
    refusal), and exact ``head_sha`` plus ``(base_branch, base_sha)`` binding.
    """

    if not isinstance(payload, dict):
        raise GitHubPublicationError("review preflight response was invalid")
    if type(payload.get("draft")) is not bool:
        raise GitHubPublicationError("review preflight draft was invalid")
    if payload.get("state") != "open":
        return LiveWriteGateDecision(
            admitted=False,
            skip_status="skipped_pr_state",
            reason="pr_closed",
        )
    if payload.get("draft") is True:
        return LiveWriteGateDecision(
            admitted=False,
            skip_status="skipped_pr_state",
            reason="pr_draft",
        )
    head = payload.get("head")
    if not isinstance(head, dict):
        raise GitHubPublicationError("review preflight head was invalid")
    if head.get("sha") != head_sha:
        return LiveWriteGateDecision(
            admitted=False,
            skip_status="skipped_stale_head",
            reason="stale_head_sha",
        )
    head_repository = head.get("repo")
    if not isinstance(head_repository, dict):
        raise GitHubPublicationError("review preflight head repository was invalid")
    head_fork = head_repository.get("fork")
    if type(head_fork) is not bool:
        raise GitHubPublicationError("review preflight head repository was invalid")
    if head_fork or head_repository.get("full_name") != repository:
        return LiveWriteGateDecision(
            admitted=False,
            skip_status="skipped_fork",
            reason="head_repository_fork_or_mismatch",
        )
    base = payload.get("base")
    if not isinstance(base, dict):
        raise GitHubPublicationError("review preflight base was invalid")
    repository_info = base.get("repo")
    if not isinstance(repository_info, dict):
        raise GitHubPublicationError("review preflight repository was invalid")
    base_id = repository_info.get("id")
    if isinstance(base_id, bool) or not isinstance(base_id, int) or base_id <= 0:
        raise GitHubPublicationError("review preflight repository was invalid")
    base_fork = repository_info.get("fork")
    if type(base_fork) is not bool:
        raise GitHubPublicationError("review preflight repository was invalid")
    if base_id != repository_id or repository_info.get("full_name") != repository:
        return LiveWriteGateDecision(
            admitted=False,
            skip_status="skipped_repository_mismatch",
            reason="base_repository_mismatch",
        )
    if base_fork:
        return LiveWriteGateDecision(
            admitted=False,
            skip_status="skipped_fork",
            reason="base_repository_fork",
        )
    actual_base_branch = base.get("ref")
    actual_base_sha = base.get("sha")
    if not isinstance(actual_base_branch, str) or not _GIT_SHA_HEX.fullmatch(
        actual_base_sha if isinstance(actual_base_sha, str) else ""
    ):
        raise GitHubPublicationError("review preflight base binding was invalid")
    if actual_base_sha != base_sha:
        return LiveWriteGateDecision(
            admitted=False,
            skip_status="skipped_stale_base",
            reason="stale_base_sha",
        )
    if actual_base_branch != base_branch:
        return LiveWriteGateDecision(
            admitted=False,
            skip_status="skipped_stale_base",
            reason="stale_base_ref",
        )
    author = payload.get("user")
    if not isinstance(author, dict):
        raise GitHubPublicationError("review preflight author was invalid")
    author_login = author.get("login")
    if not isinstance(author_login, str) or not author_login.strip():
        raise GitHubPublicationError("review preflight author was invalid")
    return LiveWriteGateDecision(
        admitted=True,
        skip_status=None,
        reason="admitted",
        app_authored=author_login == app_slug,
    )


@dataclass(frozen=True)
class PullRequestLifecycleSnapshot:
    """Validated snapshot of a pull request across close/reopen/branch changes."""

    repository: str
    repository_id: int
    pull_request: int
    state: str
    draft: bool
    base_ref: str
    base_sha: str
    base_fork: bool
    head_ref: str
    head_sha: str
    head_repo_present: bool
    head_repo_full_name: str | None
    head_repo_fork: bool | None
    author_login: str
    branch_ref_exists: bool = True

    @classmethod
    def from_pull_request_payload(
        cls,
        payload: object,
        *,
        branch_ref_exists: bool = True,
    ) -> PullRequestLifecycleSnapshot:
        """Validate a GitHub pull-request payload into an immutable snapshot."""

        if not isinstance(payload, dict):
            raise GitHubPublicationError("pull request payload was invalid")
        if type(branch_ref_exists) is not bool:
            raise GitHubPublicationError("branch_ref_exists must be a boolean")
        number = payload.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise GitHubPublicationError("pull request number was invalid")
        state = payload.get("state")
        if state not in {"open", "closed"}:
            raise GitHubPublicationError("pull request state was invalid")
        draft = payload.get("draft")
        if type(draft) is not bool:
            raise GitHubPublicationError("review preflight draft was invalid")
        head = payload.get("head")
        if not isinstance(head, dict):
            raise GitHubPublicationError("review preflight head was invalid")
        head_ref = head.get("ref")
        head_sha = head.get("sha")
        if (
            not isinstance(head_ref, str)
            or not head_ref.strip()
            or not isinstance(head_sha, str)
            or _GIT_SHA_HEX.fullmatch(head_sha) is None
        ):
            raise GitHubPublicationError("pull request head binding was invalid")
        raw_head_repo = head.get("repo")
        if raw_head_repo is None:
            head_repo_present = False
            head_repo_full_name: str | None = None
            head_repo_fork: bool | None = None
        elif isinstance(raw_head_repo, dict):
            full_name = raw_head_repo.get("full_name")
            fork = raw_head_repo.get("fork")
            if (
                not isinstance(full_name, str)
                or not full_name.strip()
                or type(fork) is not bool
            ):
                raise GitHubPublicationError(
                    "review preflight head repository was invalid"
                )
            head_repo_present = True
            head_repo_full_name = full_name
            head_repo_fork = fork
        else:
            raise GitHubPublicationError("review preflight head repository was invalid")
        base = payload.get("base")
        if not isinstance(base, dict):
            raise GitHubPublicationError("review preflight base was invalid")
        base_ref = base.get("ref")
        base_sha = base.get("sha")
        if (
            not isinstance(base_ref, str)
            or not base_ref.strip()
            or not isinstance(base_sha, str)
            or _GIT_SHA_HEX.fullmatch(base_sha) is None
        ):
            raise GitHubPublicationError("review preflight base binding was invalid")
        base_repo = base.get("repo")
        if not isinstance(base_repo, dict):
            raise GitHubPublicationError("review preflight repository was invalid")
        repository_id = base_repo.get("id")
        repository = base_repo.get("full_name")
        base_fork = base_repo.get("fork")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or not isinstance(repository, str)
            or not repository.strip()
            or type(base_fork) is not bool
        ):
            raise GitHubPublicationError("review preflight repository was invalid")
        author = payload.get("user")
        if not isinstance(author, dict):
            raise GitHubPublicationError("review preflight author was invalid")
        author_login = author.get("login")
        if not isinstance(author_login, str) or not author_login.strip():
            raise GitHubPublicationError("review preflight author was invalid")
        return cls(
            repository=repository,
            repository_id=repository_id,
            pull_request=number,
            state=state,
            draft=draft,
            base_ref=base_ref,
            base_sha=base_sha,
            base_fork=base_fork,
            head_ref=head_ref,
            head_sha=head_sha,
            head_repo_present=head_repo_present,
            head_repo_full_name=head_repo_full_name,
            head_repo_fork=head_repo_fork,
            author_login=author_login,
            branch_ref_exists=branch_ref_exists,
        )

    @property
    def session_identity(self) -> SessionIdentity:
        return SessionIdentity(
            self.repository,
            self.pull_request,
            repository_id=self.repository_id,
        )

    @property
    def has_live_same_repo_head(self) -> bool:
        return (
            self.head_repo_present
            and self.head_repo_fork is False
            and self.head_repo_full_name == self.repository
            and not self.base_fork
            and self.branch_ref_exists
        )

    @property
    def admits_live_writes(self) -> bool:
        return (
            self.state == "open"
            and self.draft is False
            and self.has_live_same_repo_head
        )


def session_has_unresolved_obligations(
    record: SessionRecord | None,
    *,
    ledger: object | None = None,
) -> bool:
    """Return True when the persisted session baseline carries active findings."""

    if record is None:
        return False
    baseline = read_session_baseline(ledger, record)
    if baseline is None:
        return False
    return bool(baseline.findings)


@dataclass(frozen=True)
class ReopenBranchLifecycleDecision:
    """Lifecycle classification and recovery contract for close/reopen/branch state."""

    transition_kind: ReopenTransitionKind
    shares_session_identity: bool
    admits_live_writes: bool
    preserves_prior_obligations: bool
    unresolved_obligations_present: bool
    effective_failed_attempts: int
    mints_fresh_failed_attempt_allowance: bool
    prior_approval_valid: bool
    revokes_in_flight_dispatches: bool
    recovery_action: LifecycleRecoveryAction


def classify_reopen_branch_transition(
    previous: PullRequestLifecycleSnapshot,
    current: PullRequestLifecycleSnapshot,
    *,
    prior_record: SessionRecord | None = None,
    ledger: object | None = None,
    prior_approved_binding: tuple[str, str] | None = None,
) -> ReopenBranchLifecycleDecision:
    """Classify a close/reopen or branch delete/recreate transition (G05).

    ``prior_approved_binding`` is ``(base_sha, head_sha)`` of any previously
    published bot ``APPROVED`` review on ``previous.pull_request``.
    """

    if (
        previous.repository_id != current.repository_id
        or previous.repository != current.repository
    ):
        raise GitHubPublicationError("lifecycle snapshots span different repositories")

    if previous.pull_request != current.pull_request:
        return ReopenBranchLifecycleDecision(
            transition_kind="distinct_pull_request_on_recreated_branch",
            shares_session_identity=False,
            admits_live_writes=current.admits_live_writes,
            preserves_prior_obligations=False,
            unresolved_obligations_present=False,
            effective_failed_attempts=0,
            mints_fresh_failed_attempt_allowance=False,
            prior_approval_valid=False,
            revokes_in_flight_dispatches=False,
            recovery_action="isolated_new_pull_request_session",
        )

    unresolved = session_has_unresolved_obligations(prior_record, ledger=ledger)
    effective_failed = (
        failed_attempts_for_head(prior_record, current.head_sha)
        if prior_record is not None
        else 0
    )
    same_snapshot = (
        current.head_sha == previous.head_sha and current.base_sha == previous.base_sha
    )
    approval_matches_current = (
        prior_approved_binding is not None
        and prior_approved_binding == (current.base_sha, current.head_sha)
        and not unresolved
    )

    if current.state != "open" or current.draft is True:
        return ReopenBranchLifecycleDecision(
            transition_kind="closed_blocking_writes",
            shares_session_identity=True,
            admits_live_writes=False,
            preserves_prior_obligations=True,
            unresolved_obligations_present=unresolved,
            effective_failed_attempts=effective_failed,
            mints_fresh_failed_attempt_allowance=False,
            prior_approval_valid=approval_matches_current,
            revokes_in_flight_dispatches=False,
            recovery_action="await_reopen_preserve_ledger",
        )

    if not current.has_live_same_repo_head:
        return ReopenBranchLifecycleDecision(
            transition_kind="head_branch_deleted_blocking_writes",
            shares_session_identity=True,
            admits_live_writes=False,
            preserves_prior_obligations=True,
            unresolved_obligations_present=unresolved,
            effective_failed_attempts=effective_failed,
            mints_fresh_failed_attempt_allowance=False,
            prior_approval_valid=False,
            revokes_in_flight_dispatches=False,
            recovery_action="restore_head_branch_or_reopen",
        )

    branch_was_deleted = (
        not previous.head_repo_present or not previous.branch_ref_exists
    )
    if branch_was_deleted:
        if same_snapshot:
            return ReopenBranchLifecycleDecision(
                transition_kind="branch_recreated_same_sha",
                shares_session_identity=True,
                admits_live_writes=True,
                preserves_prior_obligations=True,
                unresolved_obligations_present=unresolved,
                effective_failed_attempts=effective_failed,
                mints_fresh_failed_attempt_allowance=False,
                prior_approval_valid=approval_matches_current,
                revokes_in_flight_dispatches=False,
                recovery_action="reuse_completed_or_retained_snapshot",
            )
        return ReopenBranchLifecycleDecision(
            transition_kind="branch_recreated_changed_sha",
            shares_session_identity=True,
            admits_live_writes=True,
            preserves_prior_obligations=True,
            unresolved_obligations_present=unresolved,
            effective_failed_attempts=effective_failed,
            mints_fresh_failed_attempt_allowance=False,
            prior_approval_valid=False,
            revokes_in_flight_dispatches=False,
            recovery_action="rebind_obligations_and_verify_new_snapshot",
        )

    if previous.state != "open" or previous.draft is True:
        if same_snapshot:
            return ReopenBranchLifecycleDecision(
                transition_kind="reopened_same_sha",
                shares_session_identity=True,
                admits_live_writes=True,
                preserves_prior_obligations=True,
                unresolved_obligations_present=unresolved,
                effective_failed_attempts=effective_failed,
                mints_fresh_failed_attempt_allowance=False,
                prior_approval_valid=approval_matches_current,
                revokes_in_flight_dispatches=False,
                recovery_action="reuse_completed_or_retained_snapshot",
            )
        return ReopenBranchLifecycleDecision(
            transition_kind="reopened_changed_sha",
            shares_session_identity=True,
            admits_live_writes=True,
            preserves_prior_obligations=True,
            unresolved_obligations_present=unresolved,
            effective_failed_attempts=effective_failed,
            mints_fresh_failed_attempt_allowance=False,
            prior_approval_valid=False,
            revokes_in_flight_dispatches=False,
            recovery_action="rebind_obligations_and_verify_new_snapshot",
        )

    if same_snapshot:
        return ReopenBranchLifecycleDecision(
            transition_kind="duplicate_open_delivery",
            shares_session_identity=True,
            admits_live_writes=True,
            preserves_prior_obligations=True,
            unresolved_obligations_present=unresolved,
            effective_failed_attempts=effective_failed,
            mints_fresh_failed_attempt_allowance=False,
            prior_approval_valid=approval_matches_current,
            revokes_in_flight_dispatches=False,
            recovery_action="reuse_completed_or_retained_snapshot",
        )

    return ReopenBranchLifecycleDecision(
        transition_kind="open_head_or_base_changed",
        shares_session_identity=True,
        admits_live_writes=True,
        preserves_prior_obligations=True,
        unresolved_obligations_present=unresolved,
        effective_failed_attempts=effective_failed,
        mints_fresh_failed_attempt_allowance=False,
        prior_approval_valid=False,
        revokes_in_flight_dispatches=False,
        recovery_action="rebind_obligations_and_verify_new_snapshot",
    )


SourceIdentityTransitionKind = Literal[
    "unchanged_snapshot",
    "fast_forward_head_advance",
    "rebase_with_base_sha_drift",
    "unrelated_history",
    "force_push_revert_to_prior_sha",
    "force_push_identical_tree_new_sha",
    "force_push_changed_content",
    "base_sha_advance_constant_head",
    "same_sha_base_ref_retarget",
]

SourceRecoveryMode = Literal[
    "exact_snapshot_replay",
    "incremental_verify",
    "fallback_full_review",
    "refuse_prepare_diff_retarget_or_rescan",
    "verify_against_latest_obligations",
    "rebind_base_ref_without_base_sha_invalidation",
]


@dataclass(frozen=True)
class GitAncestryEvidence:
    """Bounded local Git commit-graph facts between two PR snapshots (G06)."""

    merge_base_sha: str | None
    prior_head_is_ancestor_of_new_head: bool
    prior_base_is_ancestor_of_new_base: bool
    prior_head_tree_sha: str | None = None
    new_head_tree_sha: str | None = None
    diff_bytes_identical: bool = False


def _validate_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _GIT_SHA_HEX.fullmatch(value) is None:
        raise GitHubPublicationError(f"{label} must be a 40-hex commit SHA")
    return value


def _validate_ref(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubPublicationError(f"{label} must be a non-empty ref name")
    return value


def _git_query(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo_root), "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True,
        check=False,
    )


def inspect_git_ancestry(
    repo_root: Path,
    *,
    previous_base_sha: str,
    previous_head_sha: str,
    current_base_sha: str,
    current_head_sha: str,
) -> GitAncestryEvidence:
    """Inspect local Git ancestry, merge-base, tree SHAs, and three-dot diff parity."""

    if not isinstance(repo_root, Path) or not repo_root.is_dir():
        raise GitHubPublicationError("repository root must be an existing directory")
    prev_base = _validate_sha(previous_base_sha, label="previous_base_sha")
    prev_head = _validate_sha(previous_head_sha, label="previous_head_sha")
    curr_base = _validate_sha(current_base_sha, label="current_base_sha")
    curr_head = _validate_sha(current_head_sha, label="current_head_sha")

    mb = _git_query(repo_root, ["merge-base", curr_base, curr_head])
    merge_base_text = mb.stdout.decode("utf-8", errors="replace").strip()
    merge_base_sha = (
        merge_base_text
        if mb.returncode == 0 and _GIT_SHA_HEX.fullmatch(merge_base_text) is not None
        else None
    )

    head_anc = _git_query(
        repo_root, ["merge-base", "--is-ancestor", prev_head, curr_head]
    )
    base_anc = _git_query(
        repo_root, ["merge-base", "--is-ancestor", prev_base, curr_base]
    )

    prev_tree_proc = _git_query(repo_root, ["rev-parse", f"{prev_head}^{{tree}}"])
    curr_tree_proc = _git_query(repo_root, ["rev-parse", f"{curr_head}^{{tree}}"])
    prev_tree_text = prev_tree_proc.stdout.decode("utf-8", errors="replace").strip()
    curr_tree_text = curr_tree_proc.stdout.decode("utf-8", errors="replace").strip()
    prev_tree = (
        prev_tree_text
        if prev_tree_proc.returncode == 0
        and _GIT_SHA_HEX.fullmatch(prev_tree_text) is not None
        else None
    )
    curr_tree = (
        curr_tree_text
        if curr_tree_proc.returncode == 0
        and _GIT_SHA_HEX.fullmatch(curr_tree_text) is not None
        else None
    )

    diff_identical = False
    prev_mb = _git_query(repo_root, ["merge-base", prev_base, prev_head])
    if merge_base_sha is not None and prev_mb.returncode == 0:
        old_diff = _git_query(repo_root, ["diff", f"{prev_base}...{prev_head}"])
        new_diff = _git_query(repo_root, ["diff", f"{curr_base}...{curr_head}"])
        diff_identical = (
            old_diff.returncode == 0
            and new_diff.returncode == 0
            and old_diff.stdout == new_diff.stdout
        )

    return GitAncestryEvidence(
        merge_base_sha=merge_base_sha,
        prior_head_is_ancestor_of_new_head=head_anc.returncode == 0,
        prior_base_is_ancestor_of_new_base=base_anc.returncode == 0,
        prior_head_tree_sha=prev_tree,
        new_head_tree_sha=curr_tree,
        diff_bytes_identical=diff_identical,
    )


@dataclass(frozen=True)
class SourceIdentityTransitionDecision:
    """Classification of PR head/base history rewrites and ref retargets (G06).

    Explicitly separates ``same_sha_base_ref_retarget`` (where ``base_ref``
    changes while ``base_sha`` is unchanged, keeping ``ReviewContextCacheKey``
    compatible while gating mid-flight review/reply writes as
    ``skipped_stale_base``) from ``base_sha`` drift invalidation
    (``rebase-or-base-change``).
    """

    transition_kind: SourceIdentityTransitionKind
    baseline_cache_compatible: bool
    baseline_invalidation_reason: str | None
    in_flight_publication_skip_status: str | None
    in_flight_reply_skip_status: str | None
    preserves_newer_session_obligations: bool
    reuses_superseded_approval_without_verification: bool
    revokes_post_dispatch_effects: bool
    recovery_mode: SourceRecoveryMode


def classify_source_identity_transition(
    *,
    previous_base_ref: str,
    previous_base_sha: str,
    previous_head_sha: str,
    current_base_ref: str,
    current_base_sha: str,
    current_head_sha: str,
    ancestry: GitAncestryEvidence,
    previously_reviewed_head_shas: Collection[str] = (),
) -> SourceIdentityTransitionDecision:
    """Classify all 7 G06 head/base ancestry and ref-retarget transitions."""

    prev_ref = _validate_ref(previous_base_ref, label="previous_base_ref")
    curr_ref = _validate_ref(current_base_ref, label="current_base_ref")
    prev_base = _validate_sha(previous_base_sha, label="previous_base_sha")
    prev_head = _validate_sha(previous_head_sha, label="previous_head_sha")
    curr_base = _validate_sha(current_base_sha, label="current_base_sha")
    curr_head = _validate_sha(current_head_sha, label="current_head_sha")

    head_changed = curr_head != prev_head
    base_sha_changed = curr_base != prev_base
    base_ref_changed = curr_ref != prev_ref

    if head_changed:
        in_flight_skip: str | None = "skipped_stale_head"
    elif base_sha_changed or base_ref_changed:
        in_flight_skip = "skipped_stale_base"
    else:
        in_flight_skip = None

    if ancestry.merge_base_sha is None:
        return SourceIdentityTransitionDecision(
            transition_kind="unrelated_history",
            baseline_cache_compatible=False,
            baseline_invalidation_reason="unrelated-history",
            in_flight_publication_skip_status=in_flight_skip,
            in_flight_reply_skip_status=in_flight_skip,
            preserves_newer_session_obligations=True,
            reuses_superseded_approval_without_verification=False,
            revokes_post_dispatch_effects=False,
            recovery_mode="refuse_prepare_diff_retarget_or_rescan",
        )

    if not head_changed and not base_sha_changed and not base_ref_changed:
        return SourceIdentityTransitionDecision(
            transition_kind="unchanged_snapshot",
            baseline_cache_compatible=True,
            baseline_invalidation_reason=None,
            in_flight_publication_skip_status=None,
            in_flight_reply_skip_status=None,
            preserves_newer_session_obligations=True,
            reuses_superseded_approval_without_verification=False,
            revokes_post_dispatch_effects=False,
            recovery_mode="exact_snapshot_replay",
        )

    if not head_changed and not base_sha_changed and base_ref_changed:
        return SourceIdentityTransitionDecision(
            transition_kind="same_sha_base_ref_retarget",
            baseline_cache_compatible=True,
            baseline_invalidation_reason=None,
            in_flight_publication_skip_status="skipped_stale_base",
            in_flight_reply_skip_status="skipped_stale_base",
            preserves_newer_session_obligations=True,
            reuses_superseded_approval_without_verification=False,
            revokes_post_dispatch_effects=False,
            recovery_mode="rebind_base_ref_without_base_sha_invalidation",
        )

    if not head_changed and base_sha_changed:
        return SourceIdentityTransitionDecision(
            transition_kind="base_sha_advance_constant_head",
            baseline_cache_compatible=False,
            baseline_invalidation_reason="rebase-or-base-change",
            in_flight_publication_skip_status="skipped_stale_base",
            in_flight_reply_skip_status="skipped_stale_base",
            preserves_newer_session_obligations=True,
            reuses_superseded_approval_without_verification=False,
            revokes_post_dispatch_effects=False,
            recovery_mode="fallback_full_review",
        )

    if head_changed and base_sha_changed:
        return SourceIdentityTransitionDecision(
            transition_kind="rebase_with_base_sha_drift",
            baseline_cache_compatible=False,
            baseline_invalidation_reason="rebase-or-base-change",
            in_flight_publication_skip_status="skipped_stale_head",
            in_flight_reply_skip_status="skipped_stale_head",
            preserves_newer_session_obligations=True,
            reuses_superseded_approval_without_verification=False,
            revokes_post_dispatch_effects=False,
            recovery_mode="fallback_full_review",
        )

    if ancestry.prior_head_is_ancestor_of_new_head:
        return SourceIdentityTransitionDecision(
            transition_kind="fast_forward_head_advance",
            baseline_cache_compatible=True,
            baseline_invalidation_reason=None,
            in_flight_publication_skip_status="skipped_stale_head",
            in_flight_reply_skip_status="skipped_stale_head",
            preserves_newer_session_obligations=True,
            reuses_superseded_approval_without_verification=False,
            revokes_post_dispatch_effects=False,
            recovery_mode="incremental_verify",
        )

    if not ancestry.prior_head_is_ancestor_of_new_head and (
        curr_head in previously_reviewed_head_shas
    ):
        return SourceIdentityTransitionDecision(
            transition_kind="force_push_revert_to_prior_sha",
            baseline_cache_compatible=True,
            baseline_invalidation_reason=None,
            in_flight_publication_skip_status="skipped_stale_head",
            in_flight_reply_skip_status="skipped_stale_head",
            preserves_newer_session_obligations=True,
            reuses_superseded_approval_without_verification=False,
            revokes_post_dispatch_effects=False,
            recovery_mode="verify_against_latest_obligations",
        )

    identical_tree_or_diff = (
        ancestry.prior_head_tree_sha is not None
        and ancestry.prior_head_tree_sha == ancestry.new_head_tree_sha
    ) or ancestry.diff_bytes_identical
    if not ancestry.prior_head_is_ancestor_of_new_head and identical_tree_or_diff:
        return SourceIdentityTransitionDecision(
            transition_kind="force_push_identical_tree_new_sha",
            baseline_cache_compatible=True,
            baseline_invalidation_reason=None,
            in_flight_publication_skip_status="skipped_stale_head",
            in_flight_reply_skip_status="skipped_stale_head",
            preserves_newer_session_obligations=True,
            reuses_superseded_approval_without_verification=False,
            revokes_post_dispatch_effects=False,
            recovery_mode="incremental_verify",
        )

    return SourceIdentityTransitionDecision(
        transition_kind="force_push_changed_content",
        baseline_cache_compatible=True,
        baseline_invalidation_reason=None,
        in_flight_publication_skip_status="skipped_stale_head",
        in_flight_reply_skip_status="skipped_stale_head",
        preserves_newer_session_obligations=True,
        reuses_superseded_approval_without_verification=False,
        revokes_post_dispatch_effects=False,
        recovery_mode="incremental_verify",
    )
