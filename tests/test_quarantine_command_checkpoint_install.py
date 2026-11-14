"""Cold hooks are registered only by each original defining loader frame."""

import subprocess
import sys
import unittest


class ColdCheckpointInstallerTests(unittest.TestCase):
    def test_forged_loader_code_and_separate_globals_are_rejected(self):
        source = """
import importlib.util
import sys
import types
from pathlib import Path
from review_sensei_github import quarantine_command_checkpoint as hooks
from review_sensei.errors import ReviewInputError
for kind, suffix, qualified in (
    ("census", "packed_comment_census", "fetch_packed_comments"),
    ("get", "quarantine_checkpoint", "_CheckpointSnapshot.get"),
    ("open", "api_run_requests", "ApiRunRequestTransport.open"),
):
    name = "review_sensei_github." + suffix
    original_module = sys.modules.get(name)
    assert original_module is None or kind == "open"
    for separate in (False, True):
        forged = types.ModuleType(name)
        spec = importlib.util.spec_from_file_location(name, Path(hooks.__file__).with_name(suffix + ".py"))
        spec._initializing = True
        forged.__spec__ = spec
        forged.install = hooks.install_source
        forged.kind = kind
        exec("def function(): pass", vars(forged))
        forged.function.__qualname__ = qualified
        sys.modules[name] = forged
        namespace = vars(forged)
        try:
            exec(compile("install(kind, function)", "<forged loader>", "exec"), namespace, dict(namespace) if separate else namespace)
        except ReviewInputError as error:
            assert "defining source" in str(error)
        else:
            raise AssertionError("forged source installed")
        finally:
            if original_module is None:
                del sys.modules[name]
            else:
                sys.modules[name] = original_module
from review_sensei_github import api_run_requests, quarantine_checkpoint, packed_comment_census
from review_sensei.source_registry import require_module_seal
require_module_seal(hooks)
"""
        completed = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
