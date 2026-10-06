import hashlib
import importlib.abc
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "diagnostic", HERE.parent / "scripts/diagnose_standalone.py"
)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)
SHA = "a" * 40


class DiagnosticTests(unittest.TestCase):
    def test_capture_preserves_normalized_hash_but_removes_credentials(self):
        cwd = Path("/tmp/public-diagnostic-case")
        text = f"error at {cwd.resolve()}: synthetic-secret"
        result = diagnostic.stream_record(text, cwd, {"API_KEY": "synthetic-secret"})
        normalized = "error at <repository>: synthetic-secret"
        self.assertEqual(
            result["normalized_sha256"], hashlib.sha256(normalized.encode()).hexdigest()
        )
        self.assertEqual(result["normalized_utf8_bytes"], len(normalized.encode()))
        self.assertEqual(result["sanitized_text"], "error at <repository>: <redacted>")
        self.assertFalse(result["truncated"])

    def test_long_stream_is_bounded_without_losing_full_hash(self):
        text = "x" * (diagnostic.LIMIT + 50)
        result = diagnostic.stream_record(text, Path("/tmp/public-case"), {})
        self.assertEqual(len(result["sanitized_text"]), diagnostic.LIMIT)
        self.assertTrue(result["truncated"])
        self.assertEqual(
            result["normalized_sha256"], hashlib.sha256(text.encode()).hexdigest()
        )

    def test_wrong_source_cannot_run_the_oracle(self):
        with patch.object(diagnostic.subprocess, "check_output", return_value="b" * 40):
            with self.assertRaisesRegex(ValueError, "does not match"):
                diagnostic.diagnose(Path("/tmp/public-case"), Path("/tmp/native"), SHA)

    def test_strict_failure_is_preserved_and_both_streams_are_retained(self):
        class SmokeError(ValueError):
            pass

        state = {}

        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                def original(command, cwd, env):
                    return subprocess.CompletedProcess(
                        command, 2, "", command[0] + " synthetic-secret"
                    )

                def smoke_run(executable, **kwargs):
                    cwd = kwargs["repository"]
                    for role in ("native", "direct"):
                        module._run(
                            [role, "missing-review-sensei-diff.patch"],
                            cwd,
                            {"API_KEY": "synthetic-secret"},
                        )
                    raise SmokeError(
                        "standalone output mismatch for validation-failure"
                    )

                module._run = original
                module.run_smoke = smoke_run
                module.SmokeError = SmokeError
                state.update(module=module, original=original)

        fake_spec = importlib.util.spec_from_loader("released_oracle", Loader())
        with (
            patch.object(diagnostic.subprocess, "check_output", return_value=SHA),
            patch.object(
                diagnostic.platform, "platform", return_value="synthetic macOS"
            ),
            patch.object(
                diagnostic.importlib.util,
                "spec_from_file_location",
                return_value=fake_spec,
            ),
        ):
            report = diagnostic.diagnose(
                Path("/tmp/public-case"), Path("/tmp/native"), SHA
            )
        self.assertEqual(report["result"], "failed")
        self.assertIn("validation-failure", report["failure"])
        self.assertFalse(report["publication_attempted"])
        self.assertEqual(
            [x["role"] for x in report["validation_failure_comparisons"]],
            ["native", "direct"],
        )
        self.assertNotIn(
            "synthetic-secret", str(report["validation_failure_comparisons"])
        )
        self.assertIs(state["module"]._run, state["original"])


if __name__ == "__main__":
    unittest.main()
