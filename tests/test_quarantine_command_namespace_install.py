"""The negative namespace cannot register a fabricated transport definition."""

import subprocess
import sys
import unittest


class NamespaceInstallerTests(unittest.TestCase):
    def test_current_registration_rejects_unissued_callback_and_counter(self):
        from review_sensei_github import quarantine_command_namespace as hooks

        from review_sensei.errors import ReviewInputError

        def validate_namespace_request(caller, deadline):
            return False

        with self.assertRaisesRegex(ReviewInputError, "original defining source"):
            hooks.install_current_validator(validate_namespace_request)
        with self.assertRaisesRegex(ReviewInputError, "source is unavailable"):
            hooks.observed_sequence(object())

    def test_current_loader_class_implementation_and_path_are_not_caller_selected(self):
        source = r"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys
import types
from review_sensei_github import quarantine_command_namespace as hooks
from review_sensei.errors import ReviewInputError
name = "review_sensei_github.current_quarantine_finalization"
expected = str(Path(hooks.__file__).with_name("current_quarantine_finalization.py"))
original = sys.modules.get(name)
class ChangedLoader(SourceFileLoader):
    pass
for kind in ("class", "implementation", "path"):
    loader = (ChangedLoader if kind == "class" else SourceFileLoader)(name, expected)
    if kind == "implementation":
        loader.get_code = lambda fullname: None
    origin = expected if kind != "path" else str(Path(expected).with_name("wrong.py"))
    module = types.ModuleType(name)
    module.__file__ = origin
    module.__spec__ = importlib.util.spec_from_file_location(name, origin, loader=loader)
    module.__spec__._initializing = True
    module.install = hooks.install_current_validator
    sys.modules[name] = module
    try:
        exec(compile("def validate_namespace_request(caller, deadline): return False\ninstall(validate_namespace_request)", expected, "exec"), vars(module))
    except ReviewInputError:
        pass
    else:
        raise AssertionError("caller-selected loader registered")
    finally:
        if original is None:
            del sys.modules[name]
        else:
            sys.modules[name] = original
"""
        result = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_forged_loader_code_and_separate_globals_are_rejected(self):
        source = r"""
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

    def test_original_zip_loader_family_can_register_from_same_package(self):
        source = r"""
import importlib
from pathlib import Path
import sys
import tempfile
import zipfile
from review_sensei.errors import ReviewInputError
source_dir = Path("packages/python/review-sensei-github/src/review_sensei_github").resolve()
namespace = (source_dir / "quarantine_command_namespace.py").read_text()
callback = "def validate_namespace_request(caller, deadline):\n    return False\nfrom .quarantine_command_namespace import install_current_validator\ninstall_current_validator(validate_namespace_request)\nfrom review_sensei.source_registry import seal_module\nseal_module()\n"
with tempfile.TemporaryDirectory() as directory:
    archive = str(Path(directory) / "same-source.zip")
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("review_sensei_github/__init__.py", "__path__.append(" + repr(str(source_dir)) + ")")
        bundle.writestr("review_sensei_github/quarantine_command_namespace.py", namespace)
        bundle.writestr("review_sensei_github/current_quarantine_finalization.py", callback)
    sys.path.insert(0, archive)
    module = importlib.import_module("review_sensei_github.current_quarantine_finalization")
    from review_sensei.source_registry import require_module_seal
    require_module_seal(module)
    assert module.validate_namespace_request(None, 0) is False
"""
        result = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
