"""Run in-process CLI tests outside the checkout's own configuration root.

The resolver discovers the default configuration filename relative to the
working directory, so this repository's dogfood ``.reviewsensei.yml`` would
otherwise decide the backend, model, and credentials of every in-process CLI
invocation.  Contract tests assert the CLI's own behavior, so each case runs
from an empty directory instead.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# A GitHub Actions job always sets these. Publication then treats the process
# as a hosted review and refuses unless a live operation record matches.
_HOST_ACTIONS_ENV = ("GITHUB_RUN_ID", "GITHUB_TOKEN", "GH_TOKEN")


class IsolatedWorkingDirectoryMixin:
    """Run each test from a fresh empty working directory."""

    def setUp(self) -> None:
        super().setUp()
        self._origin_directory = Path.cwd()
        self._isolated_directory = tempfile.TemporaryDirectory()
        os.chdir(self._isolated_directory.name)
        self._host_actions_env = {
            name: os.environ.get(name) for name in _HOST_ACTIONS_ENV
        }
        for name in _HOST_ACTIONS_ENV:
            os.environ.pop(name, None)
        self.addCleanup(self._restore_origin_directory)

    def _restore_origin_directory(self) -> None:
        os.chdir(self._origin_directory)
        self._isolated_directory.cleanup()
        for name, value in self._host_actions_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
