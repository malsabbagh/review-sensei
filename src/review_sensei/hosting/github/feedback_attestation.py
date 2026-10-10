"""Closed metadata-only broker protocol for authenticated feedback mutations.

Parsing checks reference integrity only. The broker reloads complete sources,
checks live actors and consumes a grant; this module cannot mint authority.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any, NoReturn

from ...evidence import evidence_digest
from .errors import GitHubBrokerClientError

REQUEST_KEYS = frozenset(
    "version repository repository_id pull_request head_sha operation source_comment_id "
    "run_id issued_at concurrency_group job_workflow_ref job_workflow_sha feedback mutation".split()
)
GRANT_KEYS = REQUEST_KEYS | {"actor", "actor_id", "actor_type", "association"}
SELECTION_KEYS = frozenset(
    "interface repository pull_request base_sha head_sha trigger target_ids total_bytes sources selection_digest event_key".split()
)
SOURCE_KEYS = frozenset(
    "kind comment_id updated_at author author_id association root_comment_id body_bytes body_sha256".split()
)
MUTATION_KEYS = frozenset(
    "operation_id source_digest authority_digest execution_identity inventory_digest inventory_generation "
    "reservation_id root_digest root_generation reason request_digest dispatch_digest read_accounting".split()
)
ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _refuse() -> NoReturn:
    raise GitHubBrokerClientError("Broker feedback attestation is invalid")


def _object(value: object, keys: frozenset[str] | set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _refuse()
    assert isinstance(value, Mapping)
    return value


def _integer(value: object, minimum: int = 1, maximum: int = 2**53 - 1) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        _refuse()


def _text(value: object, maximum: int) -> None:
    if not isinstance(value, str) or not value:
        _refuse()
    assert isinstance(value, str)
    try:
        if len(value.encode("utf-8")) > maximum:
            _refuse()
    except UnicodeError:
        _refuse()


def _hash(value: object, width: int = 64) -> None:
    if not isinstance(value, str) or not re.fullmatch(rf"[a-f0-9]{{{width}}}", value):
        _refuse()


def _parse_feedback_attestation(
    value: object, *, grant: bool = False
) -> dict[str, Any]:
    data = _object(value, GRANT_KEYS if grant else REQUEST_KEYS)
    if (
        data["version"] != 2
        or type(data["version"]) is not int
        or data["operation"] != "feedback"
    ):
        _refuse()
    repository = data["repository"]
    _text(repository, 512)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(
        part in {".", ".."} for part in repository.split("/")
    ):
        _refuse()
    for key in ("repository_id", "pull_request", "source_comment_id", "issued_at"):
        _integer(data[key])
    for key in ("head_sha", "job_workflow_sha"):
        _hash(data[key], 40)
    _text(data["run_id"], 19)
    if not re.fullmatch(r"[1-9][0-9]{0,18}", data["run_id"]):
        _refuse()
    _text(data["job_workflow_ref"], 2048)
    if (
        data["concurrency_group"]
        != f"reviewsensei-session-{data['repository_id']}-{data['pull_request']}"
    ):
        _refuse()
    selection = _object(data["feedback"], SELECTION_KEYS)
    if (
        selection["interface"] != "feedback-v1"
        or selection["repository"] != repository
        or selection["pull_request"] != data["pull_request"]
        or selection["head_sha"] != data["head_sha"]
    ):
        _refuse()
    _hash(selection["base_sha"], 40)
    trigger = _object(selection["trigger"], {"kind", "comment_id", "updated_at"})
    _integer(trigger["comment_id"])
    if (
        trigger["kind"] not in {"issue", "inline"}
        or trigger["comment_id"] != data["source_comment_id"]
    ):
        _refuse()
    _text(trigger["updated_at"], 128)
    _hash(selection["selection_digest"])
    event_key = evidence_digest(
        {
            "domain": "reviewsensei:feedback-event:v1",
            "repository": repository,
            "pull_request": data["pull_request"],
            "trigger": {"kind": trigger["kind"], "comment_id": trigger["comment_id"]},
        }
    )
    if selection["event_key"] != event_key:
        _refuse()
    targets = selection["target_ids"]
    if not isinstance(targets, list) or len(targets) > 250:
        _refuse()
    for target in targets:
        _hash(target)
    if len(set(targets)) != len(targets):
        _refuse()
    sources = selection["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        _refuse()
    total = 0
    identities: set[tuple[str, int]] = set()
    selected_trigger = None
    for raw in sources:
        source = _object(raw, SOURCE_KEYS)
        if (
            source["kind"] not in {"issue", "inline"}
            or source["association"] not in ASSOCIATIONS
        ):
            _refuse()
        for key in ("comment_id", "author_id"):
            _integer(source[key])
        _text(source["author"], 256)
        _text(source["updated_at"], 128)
        _integer(source["body_bytes"], maximum=65536)
        _hash(source["body_sha256"])
        if source["kind"] == "issue":
            if source["root_comment_id"] is not None:
                _refuse()
        else:
            _integer(source["root_comment_id"])
        identity = (source["kind"], source["comment_id"])
        if identity in identities:
            _refuse()
        identities.add(identity)
        total += source["body_bytes"]
        if all(source[key] == trigger[key] for key in trigger):
            selected_trigger = source
    _integer(selection["total_bytes"], maximum=262144)
    if selected_trigger is None or total != selection["total_bytes"]:
        _refuse()
    mutation = _object(data["mutation"], MUTATION_KEYS)
    for key in (
        "operation_id",
        "source_digest",
        "authority_digest",
        "inventory_digest",
        "root_digest",
    ):
        _hash(mutation[key])
    _hash(mutation["execution_identity"], 32)
    _text(mutation["reservation_id"], 128)
    for key in ("inventory_generation", "root_generation"):
        _integer(mutation[key], 0, 2147483647)
    if mutation["reason"] not in {
        "admission",
        "dispatch",
        "accounting",
        "accepted",
        "pending",
        "replay",
        "finalize",
    }:
        _refuse()
    for key in ("request_digest", "dispatch_digest"):
        if mutation[key] is not None:
            _hash(mutation[key])
    accounting = _object(
        mutation["read_accounting"], {"schema_version", "calls", "deadline_unix_ms"}
    )
    if accounting["schema_version"] != "1.0":
        _refuse()
    _integer(accounting["calls"], 0, 64)
    _integer(accounting["deadline_unix_ms"])
    if grant and (
        data["actor"] != selected_trigger["author"]
        or data["actor_id"] != selected_trigger["author_id"]
        or type(data["actor_id"]) is not int
        or data["actor_type"] != "User"
        or data["association"] != selected_trigger["association"]
    ):
        _refuse()
    return copy.deepcopy(dict(data))


def parse_feedback_attestation(value: object, *, grant: bool = False) -> dict[str, Any]:
    try:
        return _parse_feedback_attestation(value, grant=grant)
    except (TypeError, ValueError, KeyError, UnicodeError) as exc:
        raise GitHubBrokerClientError("Broker feedback attestation is invalid") from exc
