"""Positive one-shot Actions creation slot for quarantined review lineage.

Registration is trusted packaged configuration, not an operator-supplied grant.
No census absence, timeout, missing comment or deleted run creates another slot.
This module never writes a ledger or settles predecessor effects.
"""

from __future__ import annotations

import json
import re
import weakref
from importlib.resources import files

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

WORKFLOW_PATH = ".github/workflows/review-sensei-quarantine.yml"
_ISSUED: weakref.WeakSet[CreationSlot] = weakref.WeakSet()
_SEEN_EXECUTIONS: set[tuple[int, int, int]] = set()
_KEYS = frozenset(
    {
        "schema_version",
        "repository",
        "repository_id",
        "pull_request",
        "predecessor_comment_id",
        "predecessor_record_sha256",
        "transaction_id",
        "head_sha",
        "base_sha",
        "workflow_id",
        "workflow_path",
        "workflow_blob",
        "run_number",
    }
)
_INPUTS = (
    "predecessor_comment_id",
    "predecessor_record_sha256",
    "transaction_id",
    "head_sha",
    "base_sha",
    "pull_request",
)


def normalize_registration(value):
    body_limit = (
        isinstance(value, dict)
        and value.get("schema_version") == "quarantine-body-limit-activation-v2"
    )
    published = isinstance(value, dict) and value.get("schema_version") in {
        "quarantine-published-activation-v1",
        "quarantine-published-activation-v2",
    }
    if not isinstance(value, dict) or set(value) != (
        _KEYS | {"published_tail"}
        if published
        else _KEYS | {"prepublication"}
        if body_limit
        else _KEYS
    ):
        raise ReviewInputError("quarantine activation registration is unsupported")
    value = json.loads(canonical_bytes(value))
    if (
        value["schema_version"]
        not in {
            "quarantine-activation-v1",
            "quarantine-activation-v2",
            "quarantine-published-activation-v1",
            "quarantine-published-activation-v2",
            "quarantine-body-limit-activation-v2",
        }
        or not isinstance(value["repository"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]) is None
        or any(
            type(value[k]) is not int or not 1 <= value[k] <= 9_007_199_254_740_991
            for k in (
                "repository_id",
                "pull_request",
                "predecessor_comment_id",
                "workflow_id",
            )
        )
        or type(value["run_number"]) is not int
        or not 1 <= value["run_number"] <= 9_007_199_254_740_991
        or (value["schema_version"].endswith("-v1") and value["run_number"] != 1)
        or value["workflow_path"] != WORKFLOW_PATH
        or any(
            not isinstance(value[k], str)
            or re.fullmatch(r"[a-f0-9]{40}", value[k]) is None
            for k in ("head_sha", "base_sha", "workflow_blob")
        )
        or any(
            not isinstance(value[k], str)
            or re.fullmatch(r"[a-f0-9]{64}", value[k]) is None
            for k in ("predecessor_record_sha256", "transaction_id")
        )
    ):
        raise ReviewInputError("quarantine activation registration is invalid")
    if published or body_limit:
        from .quarantine_published_tail import BODY_CONTRACT, normalize_tail_contract

        execution = normalize_tail_contract(
            value["prepublication" if body_limit else "published_tail"]
        )
        if (execution["schema_version"] == BODY_CONTRACT) != body_limit:
            raise ReviewInputError("quarantine activation execution class conflicts")
    return value


def _registered_rows():
    """Closed installed registry; CLI paths/inputs cannot replace this resource."""
    raw = (
        files("review_sensei_github")
        .joinpath("quarantine_activations.json")
        .read_bytes()
    )
    return read_registry(raw)


def read_registry(raw):
    """Validate registry data for package/CI comparison; never issue authority."""
    if not isinstance(raw, bytes):
        raise ReviewInputError("quarantine activation registry bytes are required")
    if len(raw) > 32_768:
        raise ReviewInputError("quarantine activation registry is oversized")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "registrations"}
        or value["schema_version"] != "quarantine-activation-registry-v1"
        or not isinstance(value["registrations"], list)
        or len(value["registrations"]) > 32
    ):
        raise ReviewInputError("quarantine activation registry is unsupported")
    rows = [normalize_registration(v) for v in value["registrations"]]
    keys = [
        (r["repository_id"], r["pull_request"], r["predecessor_comment_id"])
        for r in rows
    ]
    if len(set(keys)) != len(keys):
        raise ReviewInputError("quarantine predecessor creation slot was reassigned")
    if len(
        {(r["repository_id"], r["workflow_id"], r["run_number"]) for r in rows}
    ) != len(rows):
        raise ReviewInputError("quarantine workflow creation slot was reused")
    return rows


def require_registry_successor(before, after):
    """Released registration slots cannot disappear, move or be reassigned."""
    previous, candidate = read_registry(before), read_registry(after)
    if candidate[: len(previous)] != previous:
        raise ReviewInputError(
            "quarantine activation registry must retain every existing row unchanged in append-only order"
        )
    return candidate


def registrations_for(identity):
    """Installed rows for discovery safety; this list grants no creation slot."""
    return [
        row
        for row in _registered_rows()
        if (row["repository"], row["repository_id"], row["pull_request"])
        == (identity.repository, identity.repository_id, identity.pull_request)
    ]


def registered_activation(identity, predecessor_comment_id):
    """Resolve exact installed scope internally; serialized rows are not authority."""
    rows = registrations_for(identity)
    matches = [
        r
        for r in rows
        if (
            r["repository"],
            r["repository_id"],
            r["pull_request"],
            r["predecessor_comment_id"],
        )
        == (
            identity.repository,
            identity.repository_id,
            identity.pull_request,
            predecessor_comment_id,
        )
    ]
    if len(matches) != 1:
        raise ReviewInputError("quarantine activation is not registered")
    return matches[0]


class CreationSlot:
    """Process-issued, HTTP/identity/run-bound and consumable once before POST."""

    def __init__(self, *, http, identity, run_id, registration, context=None):
        self._http, self._identity, self._run_id = http, identity, run_id
        self._registration = canonical_bytes(registration)
        self._consumed = False
        self._context = canonical_bytes(context) if context is not None else None

    @property
    def registration(self):
        return json.loads(self._registration)

    def require_process(self, *, http, identity, run_id):
        if (
            self not in _ISSUED
            or self._http is not http
            or self._identity != identity
            or self._run_id != run_id
            or self._consumed
        ):
            raise ReviewInputError("quarantine creation slot is foreign or consumed")

    def revalidate(
        self, *, http, identity, run_id, token, metadata_token, policy_commit
    ):
        self.require_process(http=http, identity=identity, run_id=run_id)
        if self._context is None:
            raise ReviewInputError("quarantine creation context is unavailable")
        context = json.loads(self._context)
        if context["policy_commit"] != policy_commit:
            raise ReviewInputError("quarantine current policy changed")
        scope = _authenticate_creation_scope(
            http=http,
            token=token,
            metadata_token=metadata_token,
            identity=identity,
            run_id=run_id,
            policy_commit=policy_commit,
            predecessor_comment_id=self.registration["predecessor_comment_id"],
            event=context["event"],
            actor=context["actor"],
            triggering_actor=context["triggering_actor"],
        )
        if canonical_bytes(scope) != self._registration:
            raise ReviewInputError("quarantine activation registration changed")

    def consume_creation(self, *, http, identity, run_id):
        self.require_process(http=http, identity=identity, run_id=run_id)
        # Consumed before dispatch, including a local exception or lost response.
        self._consumed = True


def _authenticate_creation_scope(
    *,
    http,
    token,
    metadata_token,
    identity,
    run_id,
    policy_commit,
    predecessor_comment_id,
    event,
    actor,
    triggering_actor,
):
    """Authenticate a registered first execution, never a replacement chance.

    The issuer reads registration itself from the installed resource. The owning
    lifecycle supplies authenticated run/identity/current-policy values. Inputs
    are untrusted and must agree with those independent values. Current PR and
    policy/revocation fences remain required by the lifecycle before each effect.
    """
    scope = registered_activation(identity, predecessor_comment_id)
    sender = event.get("sender") if isinstance(event, dict) else None
    if (
        (scope["repository"], scope["repository_id"], scope["pull_request"])
        != (identity.repository, identity.repository_id, identity.pull_request)
        or not isinstance(metadata_token, str)
        or not metadata_token.strip()
        or type(run_id) is not int
        or run_id < 1
        or not isinstance(policy_commit, str)
        or re.fullmatch(r"[a-f0-9]{40}", policy_commit) is None
        or not isinstance(actor, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", actor) is None
        or triggering_actor != actor
        or not isinstance(event, dict)
        or not isinstance(event.get("repository"), dict)
        or type(event["repository"].get("id")) is not int
        or event["repository"].get("id") != identity.repository_id
        or event["repository"].get("full_name") != identity.repository
        or not isinstance(sender, dict)
        or sender.get("login") != actor
        or sender.get("type") != "User"
        or type(sender.get("id")) is not int
        or sender["id"] < 1
        or event.get("inputs") != {name: str(scope[name]) for name in _INPUTS}
    ):
        raise ReviewInputError(
            "quarantine intent scope or maintainer identity conflicts"
        )

    def get(path, *, credential):
        status, value = http.request(
            "GET", http.repository_path(identity.repository, path), token=credential
        )
        if status != 200 or not isinstance(value, dict):
            raise ReviewInputError("quarantine execution evidence is unavailable")
        return value

    run = get(f"/actions/runs/{run_id}", credential=token)
    repository = run.get("repository")
    author, trigger = run.get("actor"), run.get("triggering_actor")
    if (
        type(run.get("id")) is not int
        or run["id"] != run_id
        or not isinstance(repository, dict)
        or type(repository.get("id")) is not int
        or repository["id"] != identity.repository_id
        or repository.get("full_name") != identity.repository
        or type(run.get("workflow_id")) is not int
        or run["workflow_id"] != scope["workflow_id"]
        or type(run.get("run_number")) is not int
        or run["run_number"] != scope["run_number"]
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] != 1
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "in_progress"
        or run.get("path") != WORKFLOW_PATH
        or run.get("head_sha") != policy_commit
        or not isinstance(author, dict)
        or not isinstance(trigger, dict)
        or any(
            person.get("login") != actor
            or person.get("type") != "User"
            or type(person.get("id")) is not int
            or person["id"] != sender["id"]
            for person in (author, trigger)
        )
    ):
        raise ReviewInputError("quarantine first execution slot does not match")
    source = get(
        f"/contents/{WORKFLOW_PATH}?ref={policy_commit}", credential=metadata_token
    )
    if source.get("type") != "file" or source.get("sha") != scope["workflow_blob"]:
        raise ReviewInputError("quarantine workflow source changed")
    permission = get(f"/collaborators/{actor}/permission", credential=metadata_token)
    permission_user = permission.get("user")
    role = permission.get("permission")
    # Preserve the repository's existing built-in-maintain projection rule.
    if role == "write" and permission.get("role_name") == "maintain":
        role = "maintain"
    if (
        role not in {"admin", "maintain"}
        or not isinstance(permission_user, dict)
        or permission_user.get("login") != actor
        or permission_user.get("type") != "User"
        or type(permission_user.get("id")) is not int
        or permission_user["id"] != sender["id"]
    ):
        raise ReviewInputError(
            "quarantine requires current numeric maintainer permission"
        )
    return scope


def authorize_creation_slot(
    *,
    http,
    token,
    metadata_token,
    identity,
    run_id,
    policy_commit,
    predecessor_comment_id,
    event,
    actor,
    triggering_actor,
):
    """Issue once after positive installed registration and live metadata checks."""
    scope = _authenticate_creation_scope(
        http=http,
        token=token,
        metadata_token=metadata_token,
        identity=identity,
        run_id=run_id,
        policy_commit=policy_commit,
        predecessor_comment_id=predecessor_comment_id,
        event=event,
        actor=actor,
        triggering_actor=triggering_actor,
    )
    execution = (identity.repository_id, scope["workflow_id"], run_id)
    if execution in _SEEN_EXECUTIONS:
        raise ReviewInputError("quarantine execution slot was already issued")
    _SEEN_EXECUTIONS.add(execution)
    slot = CreationSlot(
        http=http,
        identity=identity,
        run_id=run_id,
        registration=scope,
        context={
            "policy_commit": policy_commit,
            "event": event,
            "actor": actor,
            "triggering_actor": triggering_actor,
        },
    )
    _ISSUED.add(slot)
    return slot
