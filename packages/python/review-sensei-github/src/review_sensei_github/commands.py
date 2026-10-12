"""Load hosted command bodies against the engine CLI's helper names."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import review_sensei.cli as _engine

_SOURCE = Path(__file__).with_name("command_bodies.py").read_text(encoding="utf-8")


class _EngineGlobals(dict[str, Any]):
    """Resolve helper names from the engine CLI at call time.

    Copying the engine module once would freeze patches that tests apply to
    ``review_sensei.cli``. A missing name is read from that module when the
    hosted command runs.
    """

    def __missing__(self, key: str) -> Any:
        try:
            return getattr(_engine, key)
        except AttributeError:
            raise KeyError(key) from None


def _namespace() -> dict[str, Any]:
    namespace: dict[str, Any] = _EngineGlobals()
    namespace["__name__"] = "review_sensei_github.command_bodies"
    exec(
        compile(_SOURCE, str(Path(__file__).with_name("command_bodies.py")), "exec"),
        namespace,
    )
    return namespace


_COMMANDS = _namespace()
run_hosted_provider = cast(Callable[..., Any], _COMMANDS["run_hosted_provider"])
report_broker_failure = cast(Callable[..., bool], _COMMANDS["report_broker_failure"])
run_github = cast(Callable[..., int], _COMMANDS["run_github"])
run_host_plan = cast(Callable[..., int], _COMMANDS["run_host_plan"])
resolve_hosted_openrouter_parser = cast(
    Callable[[], Any], _COMMANDS["resolve_hosted_openrouter_parser"]
)
github_parser = cast(Callable[[], Any], _COMMANDS["_github_parser"])
host_plan_parser = cast(Callable[[], Any], _COMMANDS["_host_plan_parser"])
require_accepted_result = cast(
    Callable[..., Any], _COMMANDS["_require_accepted_result"]
)
