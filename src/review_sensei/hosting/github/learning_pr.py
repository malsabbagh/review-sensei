"""Deterministic learning proposal draft-PR publisher."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from ...models import LearningProposal
from .errors import (
    GitHubHTTPTransientError,
    GitHubLearningProposalError,
    GitHubLearningProposalTransientError,
)
from .http import MAX_PAGINATION_PAGES, GitHubHttp

LEARNING_DIRECTORY = ".github/review-sensei/learnings"
GIT_SHA_HEX = re.compile(r"^[a-f0-9]{40}$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")


def proposal_digest(proposal: LearningProposal) -> str:
    canonical = json.dumps(
        proposal.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_proposals(
    proposals: Sequence[LearningProposal],
) -> tuple[LearningProposal, ...]:
    if isinstance(proposals, (str, bytes)) or not isinstance(proposals, Sequence):
        raise GitHubLearningProposalError("learning proposals must be a sequence")
    if not proposals:
        raise GitHubLearningProposalError("learning proposal batch must not be empty")
    by_digest: dict[str, LearningProposal] = {}
    for proposal in proposals:
        if not isinstance(proposal, LearningProposal):
            raise GitHubLearningProposalError(
                "learning proposal batch contains an invalid proposal"
            )
        digest = proposal_digest(proposal)
        existing = by_digest.setdefault(digest, proposal)
        if existing != proposal:
            raise GitHubLearningProposalError("learning proposal digest conflict")
    return tuple(by_digest[digest] for digest in sorted(by_digest))


def batch_digest(proposals: Sequence[LearningProposal]) -> str:
    """Return the stable identity for one deduplicated proposal batch."""

    normalized = _normalized_proposals(proposals)
    if len(normalized) == 1:
        return proposal_digest(normalized[0])
    canonical = json.dumps(
        [proposal.to_dict() for proposal in normalized],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def learning_path(proposal: LearningProposal) -> str:
    digest = proposal_digest(proposal)
    return f"{LEARNING_DIRECTORY}/sensei-{digest[:16]}.json"


def _proposal_content(proposal: LearningProposal) -> str:
    digest = proposal_digest(proposal)
    entry = proposal.to_entry(id=f"sensei-{digest[:16]}")
    return json.dumps(entry.to_dict(), indent=2) + "\n"


@dataclass(frozen=True)
class LearningPRResult:
    status: str
    pull_request_number: int | None = None


class LearningPRPublisher:
    """Create or reuse a draft learning proposal PR at an exact base head."""

    def __init__(self, *, http: GitHubHttp) -> None:
        self.http = http

    def propose(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        proposal: LearningProposal,
    ) -> LearningPRResult:
        """Compatibility wrapper for a single-proposal batch."""

        return self.propose_batch(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
            proposals=(proposal,),
        )

    def propose_batch(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
        proposals: Sequence[LearningProposal],
    ) -> LearningPRResult:
        """Create or reuse one draft PR for a canonical proposal batch."""

        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id < 1
        ):
            raise GitHubLearningProposalError("source repository is invalid")
        if not GIT_SHA_HEX.fullmatch(head_sha):
            raise GitHubLearningProposalError("learning head sha is invalid")
        if not GIT_SHA_HEX.fullmatch(base_sha):
            raise GitHubLearningProposalError("learning base sha is invalid")
        if (
            isinstance(pull_request, bool)
            or not isinstance(pull_request, int)
            or pull_request < 1
        ):
            raise GitHubLearningProposalError("source pull request is invalid")
        if (
            not isinstance(base_branch, str)
            or not base_branch.strip()
            or BRANCH_PATTERN.fullmatch(base_branch) is None
            or base_branch.startswith((".", "/"))
            or base_branch.endswith((".", "/"))
            or ".." in base_branch
            or "//" in base_branch
        ):
            raise GitHubLearningProposalError("learning base branch is invalid")
        normalized = _normalized_proposals(proposals)
        preflight = self._preflight_source_pr(
            token=token,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            base_branch=base_branch,
            base_sha=base_sha,
        )
        if preflight is not None:
            return preflight
        digest = batch_digest(normalized)
        branch = f"review-sensei/learnings/pr-{pull_request}-{digest[:16]}"
        marker = f"<!-- reviewsensei:learning:v1 pr={pull_request} batch={digest} -->"
        existing = self._find_open_pr(
            token=token,
            repository=repository,
            branch=branch,
            marker=marker,
        )
        if existing is not None:
            return LearningPRResult(
                status="skipped_pull_request_exists",
                pull_request_number=existing,
            )
        pending: list[tuple[str, LearningProposal]] = []
        for proposal in normalized:
            path = learning_path(proposal)
            if not self._reconcile_existing_file(
                token=token,
                repository=repository,
                base_ref=base_sha,
                path=path,
                proposal=proposal,
            ):
                pending.append((path, proposal))
        if not pending:
            return LearningPRResult(status="skipped_identical")
        ref_path = self.http.repository_path(repository, "/git/refs")
        try:
            status, _payload = self.http.request(
                "POST",
                ref_path,
                token=token,
                body={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
        except GitHubHTTPTransientError as exc:
            if not self._branch_points_to(
                token=token,
                repository=repository,
                branch=branch,
                expected_sha=base_sha,
            ):
                raise GitHubLearningProposalTransientError(
                    "learning branch creation failed temporarily"
                ) from exc
            status = 201
        if status < 200 or status >= 300:
            existing_after = self._find_open_pr(
                token=token,
                repository=repository,
                branch=branch,
                marker=marker,
            )
            if existing_after is not None:
                return LearningPRResult(
                    status="skipped_pull_request_exists",
                    pull_request_number=existing_after,
                )
            if status in {409, 422, 429} or status >= 500:
                if self._branch_points_to(
                    token=token,
                    repository=repository,
                    branch=branch,
                    expected_sha=base_sha,
                ):
                    status = 201
                elif self._completed_branch_matches(
                    token=token,
                    repository=repository,
                    branch=branch,
                    base_sha=base_sha,
                    pending=pending,
                ):
                    return self._create_learning_pull_request(
                        token=token,
                        repository=repository,
                        branch=branch,
                        base_branch=base_branch,
                        marker=marker,
                        pending=pending,
                    )
                elif status == 422:
                    raise GitHubLearningProposalError(
                        "learning branch conflicts with the deterministic proposal"
                    )
                else:
                    raise GitHubLearningProposalTransientError(
                        "learning branch creation failed temporarily"
                    )
            else:
                raise GitHubLearningProposalError(
                    "learning branch creation was rejected"
                )

        blobs: list[tuple[str, str]] = []
        for path, proposal in pending:
            status, blob = self.http.request(
                "POST",
                self.http.repository_path(repository, "/git/blobs"),
                token=token,
                body={
                    "content": _proposal_content(proposal),
                    "encoding": "utf-8",
                },
            )
            if status < 200 or status >= 300 or not isinstance(blob, dict):
                raise GitHubLearningProposalError("learning blob creation was rejected")
            blob_sha = blob.get("sha")
            if not isinstance(blob_sha, str):
                raise GitHubLearningProposalError("learning blob response was invalid")
            blobs.append((path, blob_sha))

        status, base_commit = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/git/commits/{base_sha}"),
            token=token,
        )
        if status < 200 or status >= 300 or not isinstance(base_commit, dict):
            raise GitHubLearningProposalError("learning base commit was invalid")
        base_tree = base_commit.get("tree")
        if not isinstance(base_tree, dict) or not isinstance(base_tree.get("sha"), str):
            raise GitHubLearningProposalError("learning base commit was invalid")

        tree_payload: dict[str, object] = {
            "base_tree": base_tree["sha"],
            "tree": [
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
                for path, blob_sha in blobs
            ],
        }
        status, tree = self.http.request(
            "POST",
            self.http.repository_path(repository, "/git/trees"),
            token=token,
            body=tree_payload,
        )
        if status < 200 or status >= 300 or not isinstance(tree, dict):
            raise GitHubLearningProposalError("learning tree creation was rejected")
        tree_sha = tree.get("sha")
        if not isinstance(tree_sha, str):
            raise GitHubLearningProposalError("tree response was invalid")

        status, commit = self.http.request(
            "POST",
            self.http.repository_path(repository, "/git/commits"),
            token=token,
            body={
                "message": "chore: propose ReviewSensei learnings",
                "tree": tree_sha,
                "parents": [base_sha],
            },
        )
        if status < 200 or status >= 300 or not isinstance(commit, dict):
            raise GitHubLearningProposalError("learning commit creation was rejected")
        commit_sha = commit.get("sha")
        if not isinstance(commit_sha, str):
            raise GitHubLearningProposalError("commit response was invalid")

        try:
            status, _ = self.http.request(
                "PATCH",
                self.http.repository_path(
                    repository,
                    f"/git/refs/heads/{branch}",
                ),
                token=token,
                body={"sha": commit_sha, "force": False},
            )
        except GitHubHTTPTransientError as exc:
            if not self._branch_points_to(
                token=token,
                repository=repository,
                branch=branch,
                expected_sha=commit_sha,
            ):
                raise GitHubLearningProposalTransientError(
                    "learning branch update failed temporarily"
                ) from exc
            status = 200
        if status < 200 or status >= 300:
            if status in {409, 422, 429} or status >= 500:
                if self._branch_points_to(
                    token=token,
                    repository=repository,
                    branch=branch,
                    expected_sha=commit_sha,
                ):
                    status = 200
                elif status == 422:
                    raise GitHubLearningProposalError(
                        "learning branch update conflicted"
                    )
                else:
                    raise GitHubLearningProposalTransientError(
                        "learning branch update failed temporarily"
                    )
            else:
                raise GitHubLearningProposalError("learning branch update was rejected")

        return self._create_learning_pull_request(
            token=token,
            repository=repository,
            branch=branch,
            base_branch=base_branch,
            marker=marker,
            pending=pending,
        )

    def _create_learning_pull_request(
        self,
        *,
        token: str,
        repository: str,
        branch: str,
        base_branch: str,
        marker: str,
        pending: Sequence[tuple[str, LearningProposal]],
    ) -> LearningPRResult:
        try:
            status, pr = self.http.request(
                "POST",
                self.http.repository_path(repository, "/pulls"),
                token=token,
                body={
                    "title": "Propose ReviewSensei learnings",
                    "head": branch,
                    "base": base_branch,
                    "draft": True,
                    "body": "\n".join(
                        [
                            "Proposed ReviewSensei learning files:",
                            *[f"- `{path}`" for path, _proposal in pending],
                            "",
                            marker,
                        ]
                    ),
                },
            )
        except GitHubHTTPTransientError as exc:
            existing_after = self._find_open_pr(
                token=token,
                repository=repository,
                branch=branch,
                marker=marker,
            )
            if existing_after is not None:
                return LearningPRResult(
                    status="skipped_pull_request_exists",
                    pull_request_number=existing_after,
                )
            raise GitHubLearningProposalTransientError(
                "learning PR creation failed temporarily"
            ) from exc
        if status < 200 or status >= 300 or not isinstance(pr, dict):
            if status in {409, 422, 429} or status >= 500:
                existing_after = self._find_open_pr(
                    token=token,
                    repository=repository,
                    branch=branch,
                    marker=marker,
                )
                if existing_after is not None:
                    return LearningPRResult(
                        status="skipped_pull_request_exists",
                        pull_request_number=existing_after,
                    )
                if status != 422:
                    raise GitHubLearningProposalTransientError(
                        "learning PR creation failed temporarily"
                    )
            raise GitHubLearningProposalError("learning PR creation was rejected")
        number = pr.get("number")
        if not isinstance(number, int):
            raise GitHubLearningProposalError("learning PR response was invalid")
        return LearningPRResult(status="created", pull_request_number=number)

    def _preflight_source_pr(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
    ) -> LearningPRResult | None:
        status, payload = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/pulls/{pull_request}"),
            token=token,
        )
        if status == 404:
            raise GitHubLearningProposalError("learning source was not found")
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            raise GitHubLearningProposalError("learning source preflight failed")
        if payload.get("state") != "open" or payload.get("draft") is True:
            return LearningPRResult(status="skipped_pr_state")
        head = payload.get("head")
        base = payload.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise GitHubLearningProposalError("learning source binding was invalid")
        if head.get("sha") != head_sha:
            return LearningPRResult(status="skipped_stale_head")
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
            raise GitHubLearningProposalError("learning source repository was invalid")
        head_fork = head_repo.get("fork")
        base_fork = base_repo.get("fork")
        if not isinstance(head_fork, bool) or not isinstance(base_fork, bool):
            raise GitHubLearningProposalError("learning source repository was invalid")
        if head_fork or base_fork:
            return LearningPRResult(status="skipped_fork")
        if head_repo.get("full_name") != repository:
            return LearningPRResult(status="skipped_fork")
        base_id = base_repo.get("id")
        if (
            isinstance(base_id, bool)
            or not isinstance(base_id, int)
            or base_id != repository_id
            or base_repo.get("full_name") != repository
        ):
            return LearningPRResult(status="skipped_repository_mismatch")
        if base.get("ref") != base_branch or base.get("sha") != base_sha:
            return LearningPRResult(status="skipped_stale_base")
        return None

    def _reconcile_existing_file(
        self,
        *,
        token: str,
        repository: str,
        base_ref: str,
        path: str,
        proposal: LearningProposal,
    ) -> bool:
        """Fail closed when the deterministic target path already exists."""

        status, payload = self.http.request(
            "GET",
            self.http.repository_path(
                repository,
                f"/contents/{quote(path, safe='/')}?ref={quote(base_ref, safe='')}",
            ),
            token=token,
        )
        if status == 404:
            return False
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            raise GitHubLearningProposalError("learning file lookup failed")
        encoded = payload.get("content")
        encoding = payload.get("encoding")
        if not isinstance(encoded, str) or encoding != "base64":
            raise GitHubLearningProposalError("learning file response was invalid")
        try:
            existing = base64.b64decode("".join(encoded.split()), validate=True).decode(
                "utf-8", errors="strict"
            )
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise GitHubLearningProposalError(
                "learning file response was invalid"
            ) from exc
        expected = _proposal_content(proposal)
        if existing == expected:
            return True
        raise GitHubLearningProposalError(
            "learning file conflicts with the deterministic proposal"
        )

    def _find_open_pr(
        self,
        *,
        token: str,
        repository: str,
        branch: str,
        marker: str | None = None,
    ) -> int | None:
        path = self.http.repository_path(repository, "/pulls")
        for page in range(1, MAX_PAGINATION_PAGES + 1):
            try:
                status, payload = self.http.request(
                    "GET",
                    f"{path}?state=open&per_page=100&page={page}",
                    token=token,
                )
            except GitHubHTTPTransientError as exc:
                raise GitHubLearningProposalTransientError(
                    "learning PR lookup failed temporarily"
                ) from exc
            if status == 429 or status >= 500:
                raise GitHubLearningProposalTransientError(
                    "learning PR lookup failed temporarily"
                )
            if status < 200 or status >= 300 or not isinstance(payload, list):
                raise GitHubLearningProposalError("learning PR lookup failed")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                head = item.get("head")
                number = item.get("number")
                body = item.get("body")
                if (
                    isinstance(head, dict)
                    and head.get("ref") == branch
                    and isinstance(number, int)
                    and (marker is None or (isinstance(body, str) and marker in body))
                ):
                    return number
            if len(payload) < 100:
                return None
        raise GitHubLearningProposalError("learning PR pagination exceeded its limit")

    def _branch_points_to(
        self,
        *,
        token: str,
        repository: str,
        branch: str,
        expected_sha: str,
    ) -> bool:
        try:
            status, payload = self.http.request(
                "GET",
                self.http.repository_path(repository, f"/git/ref/heads/{branch}"),
                token=token,
            )
        except GitHubHTTPTransientError as exc:
            raise GitHubLearningProposalTransientError(
                "learning branch reconciliation failed temporarily"
            ) from exc
        if status == 404:
            return False
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            raise GitHubLearningProposalError("learning branch reconciliation failed")
        ref_object = payload.get("object")
        if not isinstance(ref_object, dict):
            raise GitHubLearningProposalError("learning branch reconciliation failed")
        sha = ref_object.get("sha")
        if not isinstance(sha, str):
            raise GitHubLearningProposalError("learning branch reconciliation failed")
        return sha == expected_sha

    def _completed_branch_matches(
        self,
        *,
        token: str,
        repository: str,
        branch: str,
        base_sha: str,
        pending: Sequence[tuple[str, LearningProposal]],
    ) -> bool:
        """Recognize only the exact deterministic commit left by a partial run."""

        status, ref_payload = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/git/ref/heads/{branch}"),
            token=token,
        )
        if status == 404:
            return False
        if status == 429 or status >= 500:
            raise GitHubLearningProposalTransientError(
                "learning branch reconciliation failed temporarily"
            )
        if status < 200 or status >= 300 or not isinstance(ref_payload, dict):
            raise GitHubLearningProposalError("learning branch reconciliation failed")
        ref_object = ref_payload.get("object")
        if not isinstance(ref_object, dict) or not isinstance(
            ref_object.get("sha"), str
        ):
            raise GitHubLearningProposalError("learning branch reconciliation failed")
        commit_sha = ref_object["sha"]

        status, commit = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/git/commits/{commit_sha}"),
            token=token,
        )
        if status == 429 or status >= 500:
            raise GitHubLearningProposalTransientError(
                "learning branch reconciliation failed temporarily"
            )
        if status < 200 or status >= 300 or not isinstance(commit, dict):
            return False
        parents = commit.get("parents")
        tree = commit.get("tree")
        if (
            commit.get("message") != "chore: propose ReviewSensei learnings"
            or not isinstance(parents, list)
            or len(parents) != 1
            or not isinstance(parents[0], dict)
            or parents[0].get("sha") != base_sha
            or not isinstance(tree, dict)
            or not isinstance(tree.get("sha"), str)
        ):
            return False

        status, comparison = self.http.request(
            "GET",
            self.http.repository_path(
                repository,
                f"/compare/{base_sha}...{commit_sha}",
            ),
            token=token,
        )
        if status == 429 or status >= 500:
            raise GitHubLearningProposalTransientError(
                "learning branch reconciliation failed temporarily"
            )
        if status < 200 or status >= 300 or not isinstance(comparison, dict):
            return False
        files = comparison.get("files")
        expected_paths = {path for path, _proposal in pending}
        if not isinstance(files, list) or len(files) != len(expected_paths):
            return False
        actual_paths: set[str] = set()
        for item in files:
            if (
                not isinstance(item, dict)
                or item.get("status") != "added"
                or not isinstance(item.get("filename"), str)
                or "previous_filename" in item
            ):
                return False
            actual_paths.add(item["filename"])
        if actual_paths != expected_paths:
            return False

        status, parent_entries = self.http.request(
            "GET",
            self.http.repository_path(
                repository,
                f"/contents/.github/review-sensei?ref={quote(commit_sha, safe='')}",
            ),
            token=token,
        )
        if status == 429 or status >= 500:
            raise GitHubLearningProposalTransientError(
                "learning branch reconciliation failed temporarily"
            )
        if status < 200 or status >= 300 or not isinstance(parent_entries, list):
            return False
        learning_directories = [
            item
            for item in parent_entries
            if isinstance(item, dict)
            and item.get("name") == "learnings"
            and item.get("path") == LEARNING_DIRECTORY
            and item.get("type") == "dir"
            and isinstance(item.get("sha"), str)
        ]
        if len(learning_directories) != 1:
            return False
        status, tree_payload = self.http.request(
            "GET",
            self.http.repository_path(
                repository,
                f"/git/trees/{learning_directories[0]['sha']}",
            ),
            token=token,
        )
        if status == 429 or status >= 500:
            raise GitHubLearningProposalTransientError(
                "learning branch reconciliation failed temporarily"
            )
        if (
            status < 200
            or status >= 300
            or not isinstance(tree_payload, dict)
            or tree_payload.get("truncated") is not False
            or not isinstance(tree_payload.get("tree"), list)
        ):
            return False
        expected_entries: dict[str, str] = {}
        expected_names = {
            path.removeprefix(f"{LEARNING_DIRECTORY}/"): path for path in expected_paths
        }
        for item in tree_payload["tree"]:
            if not isinstance(item, dict) or item.get("path") not in expected_names:
                continue
            path = expected_names[item["path"]]
            if (
                path in expected_entries
                or item.get("mode") != "100644"
                or item.get("type") != "blob"
                or not isinstance(item.get("sha"), str)
            ):
                return False
            expected_entries[path] = item["sha"]
        if set(expected_entries) != expected_paths:
            return False

        return all(
            self._blob_matches_proposal(
                token=token,
                repository=repository,
                blob_sha=expected_entries[path],
                proposal=proposal,
            )
            for path, proposal in pending
        )

    def _blob_matches_proposal(
        self,
        *,
        token: str,
        repository: str,
        blob_sha: str,
        proposal: LearningProposal,
    ) -> bool:
        status, payload = self.http.request(
            "GET",
            self.http.repository_path(repository, f"/git/blobs/{blob_sha}"),
            token=token,
        )
        if status == 429 or status >= 500:
            raise GitHubLearningProposalTransientError(
                "learning branch reconciliation failed temporarily"
            )
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            return False
        encoded = payload.get("content")
        if not isinstance(encoded, str) or payload.get("encoding") != "base64":
            return False
        try:
            content = base64.b64decode("".join(encoded.split()), validate=True).decode(
                "utf-8", errors="strict"
            )
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        return content == _proposal_content(proposal)
