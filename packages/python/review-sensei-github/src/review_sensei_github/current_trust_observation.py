"""Closed current observations carried inside a broker-authenticated grant.

Parsing is not authentication. These facts describe current observations, not
continuous selection/permission history, an actor role or a permission epoch.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, NoReturn

from .errors import GitHubBrokerClientError


def _invalid() -> NoReturn:
    raise GitHubBrokerClientError("Broker current trust observation is invalid")


def _object(value: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _invalid()
    return value


def _id(value: object) -> bool:
    return type(value) is int and 0 < value <= (1 << 53) - 1


def _login(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9-]{1,100}", value) is not None
    )


def _permissions(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 64:
        _invalid()
    if any(
        not isinstance(key, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None
        or not isinstance(level, str)
        or level not in {"read", "write"}
        for key, level in value.items()
    ):
        _invalid()
    return {key: value[key] for key in sorted(value)}


def parse_current_observation(
    value: object, *, authority: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate data binding; the existing signed-grant exchange supplies trust."""
    row = _object(
        value, {"schema_version", "repository", "installation", "capability", "actor"}
    )
    if row["schema_version"] != "current-observation-v1":
        _invalid()
    repository = _object(
        row["repository"], {"id", "full_name", "owner_id", "owner_login", "fork"}
    )
    if (
        not _id(repository["id"])
        or repository["id"] != authority["repository_id"]
        or not _id(repository["owner_id"])
        or not _login(repository["owner_login"])
        or not isinstance(repository["full_name"], str)
        or len(repository["full_name"]) > 256
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository["full_name"])
        is None
        or repository["full_name"].lower() != authority["repository"].lower()
        or repository["owner_login"].lower()
        != repository["full_name"].split("/")[0].lower()
        or repository["fork"] is not False
    ):
        _invalid()
    installation = _object(
        row["installation"],
        {"id", "repository_selection", "repository_access", "permissions"},
    )
    if (
        not _id(installation["id"])
        or not isinstance(installation["repository_selection"], str)
        or installation["repository_selection"] not in {"all", "selected"}
        or installation["repository_access"] is not True
    ):
        _invalid()
    installed = _permissions(installation["permissions"])
    capability = _object(row["capability"], {"name", "permissions"})
    effective = _permissions(capability["permissions"])
    if (
        capability["name"] != "review_session"
        or effective.get("metadata") != "read"
        or effective.get("pull_requests") != "write"
        or set(effective) - {"metadata", "pull_requests", "contents"}
        or "contents" in effective
        and effective["contents"] != "read"
        or any(
            key not in installed or level == "write" and installed[key] != "write"
            for key, level in effective.items()
        )
    ):
        _invalid()
    actor = _object(row["actor"], {"id", "login", "source_association"})
    if not _id(actor["id"]) or not _login(actor["login"]):
        _invalid()
    granted_actor = authority["actor"]
    if authority["purpose"] == "review":
        if actor["source_association"] is not None:
            _invalid()
    elif (
        not isinstance(granted_actor, dict)
        or actor["login"].lower() != granted_actor["login"].lower()
        or actor["source_association"] != granted_actor["association"]
    ):
        _invalid()
    result = json.loads(json.dumps(row))
    result["installation"]["permissions"] = installed
    result["capability"]["permissions"] = effective
    return result
