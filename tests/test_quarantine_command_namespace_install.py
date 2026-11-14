"""The negative namespace cannot register a fabricated transport definition."""

import subprocess
import sys
import unittest


class NamespaceInstallerTests(unittest.TestCase):
    def test_forged_loader_code_and_separate_globals_are_rejected(self):
        source = """
import importlib.util
import sys
import types
from pathlib import Path
from review_sensei_github import quarantine_command_namespace as hooks
from review_sensei.errors import ReviewInputError
name = "review_sensei_github.api_run_requests"
original = sys.modules.get(name)
for separate in (False, True):
    forged = types.ModuleType(name)
    spec = importlib.util.spec_from_file_location(name, Path(hooks.__file__).with_name("api_run_requests.py"))
    spec._initializing = True
    forged.__spec__ = spec
    forged.install = hooks.install
    forged.ApiRunRequestTransport = type("ApiRunRequestTransport", (), {})
    sys.modules[name] = forged
    namespace = vars(forged)
    try:
        exec(compile("install(ApiRunRequestTransport)", "<forged>", "exec"), namespace, dict(namespace) if separate else namespace)
    except ReviewInputError as error:
        assert "original defining source" in str(error)
    else:
        raise AssertionError("forged namespace source installed")
    finally:
        if original is None:
            del sys.modules[name]
        else:
            sys.modules[name] = original
from review_sensei_github import api_run_requests
from review_sensei.source_registry import require_module_seal
require_module_seal(hooks)
"""
        result = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
