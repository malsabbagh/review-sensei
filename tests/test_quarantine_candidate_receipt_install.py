"""A module-shaped frame cannot mint the actual candidate GET issuer."""

import subprocess
import sys
import unittest


class CandidateReceiptInstallerTests(unittest.TestCase):
    def test_forged_module_code_and_separate_locals_cannot_install(self):
        source = """
import importlib.util
import sys
import types
from pathlib import Path
from review_sensei_github import quarantine_candidate_receipt as receipts
from review_sensei.errors import ReviewInputError
name = 'review_sensei_github.quarantine_checkpoint'
assert name not in sys.modules
for distinct_locals in (False, True):
    forged = types.ModuleType(name)
    spec = importlib.util.spec_from_file_location(
        name, Path(receipts.__file__).with_name('quarantine_checkpoint.py'))
    spec._initializing = True
    forged.__spec__ = spec
    forged.install = receipts.install_source
    forged._CheckpointSnapshot = type('Snapshot', (), {'get': lambda self: None})
    sys.modules[name] = forged
    scope = vars(forged)
    try:
        exec(compile('install(_CheckpointSnapshot)', '<forged module>', 'exec'),
             scope, dict(scope) if distinct_locals else scope)
    except ReviewInputError as error:
        assert 'defining source' in str(error), str(error)
    else:
        raise AssertionError('forged module installed a candidate GET source')
    finally:
        del sys.modules[name]
from review_sensei_github import quarantine_checkpoint
from review_sensei.source_registry import require_module_seal
require_module_seal(receipts)
try:
    receipts.install_source(quarantine_checkpoint._CheckpointSnapshot)
except ReviewInputError:
    pass
else:
    raise AssertionError('repeated issuer installation accepted')
"""
        completed = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
