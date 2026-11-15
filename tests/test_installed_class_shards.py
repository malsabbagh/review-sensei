"""Class sharding controls use only synthetic extracted fixtures and bodies."""

import ast
import hashlib
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run_installed_test_suites.py"
COUNTS = (21, 34, 19, 8, 5)
MODULE = "test_class_probe"


class InstalledClassShardTests(unittest.TestCase):
    def execute(self, *, mode="success", workers=4, repeat=False, declared=True):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = root / "tests"
            lane = fixtures / "dist_safe"
            lane.mkdir(parents=True)
            support = textwrap.dedent("""\
                import atexit, json, os, socket, sys, unittest
                from pathlib import Path
                ROOT = Path(__file__).parent
                LOG = ROOT / ('events-' + str(os.getpid()) + '.jsonl')
                CASES = {}
                BEFORE_PATH = os.environ.get('PYTHONPATH')
                def record(kind, label=''):
                    with LOG.open('a') as stream:
                        stream.write(json.dumps([kind, label]) + '\\n')
                def sentinel(*args, **kwargs):
                    record('UNGUARDED')
                    raise RuntimeError('synthetic sentinel; never opens a network')
                socket.socket.connect = sentinel
                def guarded(label):
                    try: socket.socket.connect(None, ('invalid.example', 1))
                    except AssertionError as error:
                        assert str(error) == 'live network forbidden'
                    else: raise AssertionError('socket protection absent')
                    assert os.environ['PYTHONPATH'] == str(ROOT)
                    record('guarded', label)
                def cleanup(label, fail=False):
                    guarded(label)
                    record('cleanup', label)
                    if fail: raise RuntimeError('synthetic cleanup error')
                def restored():
                    assert socket.socket.connect is sentinel
                    assert os.environ.get('PYTHONPATH') == BEFORE_PATH
                    record('restored')
                atexit.register(restored)
                def identity(case):
                    assert CASES[case.id()] is case, 'original case reconstructed'
                    assert case.configuration == {'values': [42]}
                """)
            if mode in {"module-skip", "module-error"}:
                support += (
                    "class SocketMeta(type):\n"
                    "    def __setattr__(cls, name, value):\n"
                    "        if name == 'connect' and value is not sentinel and not isinstance(value, staticmethod):\n"
                    + (
                        "            raise unittest.SkipTest('synthetic module skip')\n"
                        if mode == "module-skip"
                        else "            raise RuntimeError('synthetic module failure')\n"
                    )
                    + "        super().__setattr__(name, value)\n"
                    "class SyntheticSocket(metaclass=SocketMeta):\n"
                    "    connect = staticmethod(sentinel)\n"
                    "socket.socket = SyntheticSocket\n"
                )
            if mode in {"environment-skip", "environment-error"}:
                support += (
                    "original_setitem = os._Environ.__setitem__\n"
                    "def setitem(self, key, value):\n"
                    "    if key == 'PYTHONPATH' and value == str(ROOT):\n"
                    "        record('second-context-failure')\n"
                    + (
                        "        raise unittest.SkipTest('synthetic environment skip')\n"
                        if mode == "environment-skip"
                        else "        raise RuntimeError('synthetic environment failure')\n"
                    )
                    + "    return original_setitem(self, key, value)\n"
                    "os._Environ.__setitem__ = setitem\n"
                )
            if mode == "global-guard-drift":
                support += "unittest.enterModuleContext = lambda cm: None\n"
            if mode == "stdlib-skip-drift":
                support += "unittest.TestCase.__unittest_skip__ = True\n"
            if mode == "stdlib-run-code-drift":
                support += (
                    "def replaced_run(self, result=None): return result\n"
                    "unittest.TestCase.run.__code__ = replaced_run.__code__\n"
                )
            (fixtures / "probe_support.py").write_text(support, encoding="utf-8")
            (fixtures / "packaging_guard.py").write_text(
                "from probe_support import record\n"
                "def assert_distribution_import(): record('installed-core')\n"
                "def is_disarmed(): return False\n",
                encoding="utf-8",
            )
            (fixtures / "review_sensei_github.py").write_text(
                "import sys\nfrom pathlib import Path\n"
                "__file__ = str(Path(sys.prefix) / 'synthetic-installed.py')\n",
                encoding="utf-8",
            )
            (fixtures / "child_probe.py").write_text(
                "import os, sys\nfrom pathlib import Path\n"
                "import review_sensei_github\n"
                "assert Path(review_sensei_github.__file__).is_relative_to(Path(sys.prefix))\n"
                "assert Path(os.environ['PYTHONPATH']).resolve() == Path(__file__).parent.resolve()\n"
                "print('synthetic-child-installed')\n",
                encoding="utf-8",
            )
            source = "import subprocess, sys, unittest\nfrom probe_support import guarded, cleanup, identity, record, ROOT\n"
            source += (
                "class FixtureParent:\n"
                "    VALUE = 42\n"
                "    def value(self): return self.VALUE\n"
                "class Fixture(FixtureParent):\n"
                "    def value(self): return super().value()\n"
            )
            rows = []
            for index, count in enumerate(COUNTS):
                cls = f"Base{index}"
                methods = tuple(f"test_{number:02d}" for number in range(count))
                rows.append((f"Wrapped{index}", "probe_cases", cls, methods))
                source += f"class {cls}(unittest.TestCase):\n"
                source += (
                    "    @staticmethod\n"
                    "    def make_value(factory=Fixture): return factory().value()\n"
                )
                source += (
                    "    @classmethod\n    def setUpClass(cls):\n"
                    "        guarded(cls.__name__ + ':class-setup')\n"
                    "        record('class-setup', cls.__name__)\n"
                    f"        cls.addClassCleanup(cleanup, cls.__name__ + ':class-cleanup', {mode == 'class-cleanup-error' and index == 0!r})\n"
                )
                if mode == "class-skip" and index == 0:
                    source += (
                        "        raise unittest.SkipTest('synthetic class skip')\n"
                    )
                if mode == "class-error" and index == 0:
                    source += "        raise RuntimeError('synthetic class error')\n"
                source += (
                    "    @classmethod\n    def tearDownClass(cls):\n"
                    "        guarded(cls.__name__ + ':class-teardown')\n"
                    "        record('class-teardown', cls.__name__)\n"
                    "    def setUp(self):\n"
                    "        identity(self)\n"
                    "        self.assertEqual(self.make_value(), 42)\n"
                    "        guarded(self.id() + ':setup')\n"
                    "        self.addCleanup(cleanup, self.id() + ':test-cleanup')\n"
                    "    def tearDown(self): guarded(self.id() + ':teardown')\n"
                )
                if mode == "class-state" and index == 0:
                    source += "    shared = []\n"
                if mode == "custom-dispatch" and index == 0:
                    source += "    def run(self, result): return super().run(result)\n"
                for number, method in enumerate(methods):
                    source += f"    def {method}(self):\n        record('body', self.id())\n        guarded(self.id())\n"
                    if number == 0:
                        source += (
                            f"        unittest.addModuleCleanup(cleanup, self.id() + ':module-cleanup', {mode == 'module-cleanup-error' and index == 0!r})\n"
                            "        child = subprocess.run([sys.executable, '-c', 'import child_probe'], cwd=ROOT.parent, capture_output=True, text=True, timeout=10)\n"
                            "        self.assertEqual(child.returncode, 0, child.stderr)\n"
                            "        self.assertIn('synthetic-child-installed', child.stdout)\n"
                            "        record('child', self.id())\n"
                        )
                    if mode == "test-failure" and index == 0 and number == 0:
                        source += "        self.fail('synthetic failure')\n"
                    if mode == "test-skip" and index == 0 and number == 0:
                        source += "        self.skipTest('synthetic test skip')\n"
            (fixtures / "probe_cases.py").write_text(source, encoding="utf-8")
            names = (
                "probe_cases.py",
                "probe_support.py",
                "packaging_guard.py",
                "child_probe.py",
            )
            sources = tuple(
                (
                    name,
                    hashlib.sha256(
                        (fixtures / name).read_text(encoding="utf-8").encode("utf-8")
                    ).hexdigest(),
                )
                for name in names
            )
            if mode == "missing-id":
                name, module, cls, methods = rows[0]
                rows[0] = (name, module, cls, methods[1:])
            if mode == "duplicate-id":
                name, module, cls, methods = rows[0]
                rows[0] = (name, module, cls, methods + (methods[0],))
            if mode == "duplicate-class":
                rows.append(rows[0])
            contract = ("repeatable-socket-path-classes-v1", tuple(rows), sources)
            wrapper = textwrap.dedent("""\
                import os
                import sys
                import unittest
                from pathlib import Path
                from unittest.mock import patch
                _TESTS = Path(__file__).resolve().parents[1]
                sys.path.insert(0, str(_TESTS))
                from packaging_guard import assert_distribution_import, is_disarmed
                assert_distribution_import()
                import review_sensei_github
                if not is_disarmed() and not Path(review_sensei_github.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
                    raise ImportError('installed hosted adapter required')
                import probe_cases
                """)
            if declared:
                wrapper += "INSTALLED_CLASS_SHARD_CONTRACT = " + repr(contract) + "\n"
            for index in range(5):
                wrapper += f"class Wrapped{index}(probe_cases.Base{index}):\n    pass\n"
            if mode == "unknown-class":
                wrapper += "class Extra(probe_cases.Base4):\n    pass\n"
            wrapper += textwrap.dedent("""\
                def setUpModule():
                    unittest.enterModuleContext(patch('socket.socket.connect', side_effect=AssertionError('live network forbidden')))
                    unittest.enterModuleContext(patch.dict(os.environ, {'PYTHONPATH': str(_TESTS)}))
                """)
            if mode == "guard-recipe":
                wrapper = wrapper.replace(
                    "live network forbidden", "changed protection"
                )
            if mode == "tests-root-binding":
                wrapper += "_TESTS = _TESTS.parent\n"
            if mode == "environment-binding":
                wrapper += "os.environ = dict(os.environ)\n"
            if mode == "patch-dict-binding":
                wrapper += "patch.dict = lambda *args, **kwargs: None\n"
            if mode == "guard-binding":
                wrapper += "patch = lambda *args, **kwargs: None\n"
            (lane / (MODULE + ".py")).write_text(wrapper, encoding="utf-8")
            if mode == "source-drift":
                with (fixtures / "probe_cases.py").open("a") as stream:
                    stream.write("# changed after review\n")
            if mode == "crlf":
                for name in names:
                    path = fixtures / name
                    path.write_bytes(path.read_text().replace("\n", "\r\n").encode())
            if mode == "unknown-hook":
                (lane / "test_other.py").write_text(
                    "import unittest\n"
                    "def load_tests(loader, tests, pattern): return tests\n",
                    encoding="utf-8",
                )
            driver = root / "driver.py"
            driver.write_text(
                textwrap.dedent("""\
                import importlib.util, sys, unittest
                from pathlib import Path
                spec = importlib.util.spec_from_file_location('synthetic_runner', RUNNER)
                runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
                runner.__file__ = __file__
                original = runner.discover
                def discover(*args, **kwargs):
                    result = original(*args, **kwargs)
                    import probe_support
                    shared = []
                    def configure(node):
                        if isinstance(node, unittest.TestSuite):
                            for child in node: configure(child)
                        else:
                            probe_support.CASES[node.id()] = node
                            node.configuration = {'values': [42]}
                            if MODE == 'shared-configuration': node.extra = shared
                            if MODE == 'worker-config' and '--worker-manifest' in sys.argv:
                                node.configuration = {'values': [43]}
                    for child in result[2]: configure(child)
                    import probe_cases
                    if MODE == 'default-reference':
                        probe_cases.Base0.make_value.__defaults__ = (type('Unpinned', (probe_cases.Fixture,), {}),)
                    if MODE == 'fixture-binding':
                        probe_cases.Fixture = type('Replacement', (probe_cases.Fixture,), {})
                    if MODE == 'helper-code':
                        probe_cases.FixtureParent.value.__code__ = (lambda self: 42).__code__
                    if MODE == 'injected-skip':
                        probe_cases.Base0.__unittest_skip__ = True
                    if MODE == 'deleted-setup': del probe_cases.Base0.setUp
                    if MODE == 'deleted-class-setup': del probe_cases.Base0.setUpClass
                    if MODE == 'deleted-helper': del probe_cases.FixtureParent.value
                    if MODE == 'plain-decorator':
                        probe_cases.Base0.make_value = vars(probe_cases.Base0)['make_value'].__func__
                    if MODE == 'class-decorator':
                        probe_cases.Base0.make_value = classmethod(vars(probe_cases.Base0)['make_value'].__func__)
                    if MODE == 'literal-attribute': probe_cases.FixtureParent.VALUE = 43
                    return result
                runner.discover = discover
                if MODE == 'reverse-order':
                    runner.HookAwareLoader.sortTestMethodsUsing = staticmethod(lambda a, b: (b > a) - (b < a))
                if MODE == 'custom-suite':
                    class Custom(unittest.TestSuite): pass
                    runner.HookAwareLoader.suiteClass = Custom
                if REPEAT and '--worker-manifest' not in sys.argv:
                    directory = Path(sys.argv[sys.argv.index('--start-directory') + 1])
                    before = discover(directory, 'test*.py')
                    setup = sys.modules['test_class_probe'].setUpModule
                    after = discover(directory, 'test*.py')
                    assert before[:2] == after[:2]
                    assert sys.modules['test_class_probe'].setUpModule is setup
                raise SystemExit(runner.main())
                """)
                .replace("RUNNER", repr(str(RUNNER)))
                .replace("MODE", repr(mode))
                .replace("REPEAT", repr(repeat)),
                encoding="utf-8",
            )
            report = root / "report.json"
            extra = []
            if mode == "parent-cost":
                costs = root / "costs.json"
                costs.write_text(json.dumps({MODULE + ".py": 500}), encoding="utf-8")
                extra = ["--costs", str(costs)]
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(driver),
                    "--start-directory",
                    str(lane),
                    "--workers",
                    str(workers),
                    "--report",
                    str(report),
                    *extra,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            data = json.loads(report.read_text()) if report.exists() else None
            events = [
                [json.loads(line) for line in path.read_text().splitlines()]
                for path in sorted(fixtures.glob("events-*.jsonl"))
            ]
            return result, data, events

    def assert_accounted(
        self, result, data, events, *, workers=4, started=87, exit_code=0
    ):
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        self.assertIsNotNone(data)
        self.assertEqual(data["discovered"], 87)
        self.assertEqual(data["units"], 5)
        self.assertEqual(len(data["workers"]), workers)
        self.assertEqual(sum(w["assigned"] for w in data["workers"]), 87)
        self.assertEqual(sum(w["started"] for w in data["workers"]), started)
        self.assertTrue(all(w["execution_complete"] for w in data["workers"]))
        expected = [
            f"{MODULE}.Wrapped{i}.test_{j:02d}"
            for i, count in enumerate(COUNTS)
            for j in range(count)
        ]
        if started == 87:
            self.assertEqual(
                sorted(
                    row[1] for process in events for row in process if row[0] == "body"
                ),
                expected,
            )
        keys = {key for worker in data["workers"] for key in worker["unit_seconds"]}
        if started == 87:
            self.assertEqual(
                keys, {f"{MODULE}.py::{MODULE}.Wrapped{i}" for i in range(5)}
            )
        for process in events:
            self.assertNotIn("UNGUARDED", [row[0] for row in process])
            self.assertEqual(process[-1][0], "restored")
            for index in range(5):
                cls = f"Wrapped{index}"
                labels = [row for row in process if cls in row[1]]
                if any(row[0] == "body" for row in labels):
                    self.assertEqual(labels[0], ["guarded", cls + ":class-setup"])
                    setup = next(
                        i for i, row in enumerate(labels) if row[0] == "class-setup"
                    )
                    teardown = next(
                        i for i, row in enumerate(labels) if row[0] == "class-teardown"
                    )
                    body_indices = [
                        i for i, row in enumerate(labels) if row[0] == "body"
                    ]
                    self.assertLess(setup, min(body_indices))
                    self.assertGreater(teardown, max(body_indices))
                    self.assertIn(
                        ["cleanup", cls + ":class-cleanup"], labels[teardown:]
                    )

    def test_actual_wrapper_inventory_and_all_dependency_pins_are_exact(self):
        # Parse only. Never import these real held fixtures or the wrapper.
        wrapper = ROOT / "tests/dist_safe/test_current_run_contract.py"
        tree = ast.parse(wrapper.read_text(encoding="utf-8"))
        declaration = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "INSTALLED_CLASS_SHARD_CONTRACT"
                for target in node.targets
            )
        ]
        self.assertEqual(len(declaration), 1)
        version, rows, sources = ast.literal_eval(declaration[0].value)
        self.assertEqual(version, "repeatable-socket-path-classes-v1")
        expected = {
            "InstalledActionReplyEntryTests": (
                "test_action_reply_entry",
                "ActionReplyEntryTests",
                8,
            ),
            "InstalledConsecutiveReviewRunTests": (
                "test_review_run",
                "ConsecutiveReviewRunTests",
                34,
            ),
            "InstalledCurrentCommandEntryTests": (
                "test_action_run",
                "CurrentCommandEntryTests",
                19,
            ),
            "InstalledRetainedEntryTests": (
                "test_review_run_rescan",
                "RetainedEntryTests",
                5,
            ),
            "InstalledReviewRunEntryTests": (
                "test_review_run",
                "ReviewRunEntryTests",
                21,
            ),
        }
        self.assertEqual(len(rows), 5)
        self.assertEqual([row[0] for row in rows], sorted(expected))
        aliases = {
            alias.asname or alias.name: alias.name
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        wrappers = {
            node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
        }
        self.assertEqual(set(wrappers), set(expected))
        identifiers = []
        for name, module, cls, methods in rows:
            self.assertEqual((module, cls, len(methods)), expected[name])
            original = ast.parse(
                (ROOT / "tests" / (module + ".py")).read_text(encoding="utf-8")
            )
            originals = [
                node
                for node in original.body
                if isinstance(node, ast.ClassDef) and node.name == cls
            ]
            self.assertEqual(len(originals), 1)
            original_methods = sorted(
                node.name
                for node in originals[0].body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test")
            )
            self.assertEqual(methods, tuple(original_methods))
            node = wrappers[name]
            self.assertFalse(node.decorator_list)
            self.assertFalse(node.keywords)
            self.assertEqual(len(node.bases), 1)
            self.assertIsInstance(node.bases[0], ast.Attribute)
            self.assertIsInstance(node.bases[0].value, ast.Name)
            self.assertEqual(aliases[node.bases[0].value.id], module)
            self.assertEqual(node.bases[0].attr, cls)
            self.assertEqual(len(node.body), 1)
            self.assertIsInstance(node.body[0], ast.Pass)
            identifiers.extend(
                f"test_current_run_contract.{name}.{method}" for method in methods
            )
        self.assertEqual(len(identifiers), 87)
        self.assertEqual(len(set(identifiers)), 87)
        pinned = (
            "fake_github_http.py",
            "original_finish_interrupt.py",
            "packaging_guard.py",
            "session_fixture_clock.py",
            "test_action_reply_entry.py",
            "test_action_run.py",
            "test_api_work_lifecycle.py",
            "test_api_work_persistence.py",
            "test_api_work_public_review.py",
            "test_review_run.py",
            "test_review_run_rescan.py",
            "test_review_transaction.py",
        )
        self.assertEqual(tuple(name for name, _ in sources), pinned)
        for name, digest in sources:
            canonical = (
                (ROOT / "tests" / name).read_text(encoding="utf-8").encode("utf-8")
            )
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), digest, name)

    def test_parent_cost_is_advisory_and_all_new_class_keys_remain_unmeasured(self):
        result, data, events = self.execute(mode="parent-cost")
        self.assert_accounted(result, data, events)
        self.assertEqual(
            data["uncosted_units"],
            [f"{MODULE}.py::{MODULE}.Wrapped{i}" for i in range(5)],
        )
        self.assertEqual(data["unused_costs"], [])
        # Equal fifths of one parent hint, not measured class times: the first
        # and fifth alphabetical units share worker zero, despite unequal size.
        self.assertEqual(
            [worker["assigned"] for worker in data["workers"]], [26, 34, 19, 8]
        )

    def test_all_87_original_cases_in_five_class_units_four_workers(self):
        self.assert_accounted(*self.execute())

    def test_single_worker_and_idempotent_discovery(self):
        self.assert_accounted(*self.execute(workers=1, repeat=True), workers=1)

    def test_crlf_dependency_checkout_keeps_reviewed_hashes(self):
        self.assert_accounted(*self.execute(mode="crlf"))

    def test_test_skip_retains_complete_execution(self):
        self.assert_accounted(*self.execute(mode="test-skip"))

    def test_test_and_cleanup_failures_preserve_inventory_and_restoration(self):
        for mode in ("test-failure", "class-cleanup-error", "module-cleanup-error"):
            with self.subTest(mode=mode):
                self.assert_accounted(*self.execute(mode=mode), exit_code=1)

    def test_class_skip_and_failure_account_for_unstarted_cases(self):
        for mode, code in (("class-skip", 0), ("class-error", 1)):
            with self.subTest(mode=mode):
                outcome = self.execute(mode=mode)
                if code:
                    self.assert_incomplete_error(*outcome)
                else:
                    self.assert_accounted(*outcome, started=66)

    def test_module_skip_and_failure_restore_guards(self):
        for mode, code in (("module-skip", 0), ("module-error", 1)):
            with self.subTest(mode=mode):
                outcome = self.execute(mode=mode)
                if code:
                    self.assert_incomplete_error(*outcome)
                else:
                    self.assert_accounted(*outcome, started=0)

    def test_source_inventory_and_unsupported_state_fall_back_without_loss(self):
        for mode in (
            "source-drift",
            "default-reference",
            "fixture-binding",
            "helper-code",
            "unknown-hook",
            "missing-id",
            "duplicate-id",
            "class-state",
            "custom-dispatch",
            "custom-suite",
            "shared-configuration",
        ):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(data["hook_mode"], "original-root")
                self.assertEqual(len(data["workers"]), 1)
                self.assertEqual(data["workers"][0]["started"], 87)
                self.assertTrue(data["workers"][0]["execution_complete"])
                self.assertEqual(
                    sum(row[0] == "body" for process in events for row in process), 87
                )

    def test_changed_guard_recipe_and_collaborators_fail_before_execution(self):
        for mode in (
            "guard-recipe",
            "guard-binding",
            "global-guard-drift",
            "stdlib-skip-drift",
            "stdlib-run-code-drift",
            "patch-dict-binding",
            "environment-binding",
            "tests-root-binding",
        ):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Installed suite discovery failed", result.stderr)
                self.assertIsNone(data)
                self.assertFalse(
                    any(row[0] == "body" for process in events for row in process)
                )

    def assert_incomplete_error(self, result, data, events):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(data["complete"])
        self.assertTrue(any(not w["execution_complete"] for w in data["workers"]))
        for process in events:
            self.assertEqual(process[-1][0], "restored")
            self.assertNotIn("UNGUARDED", [row[0] for row in process])

    def test_second_context_entry_skip_or_error_restores_socket_and_environment(self):
        for mode in ("environment-skip", "environment-error"):
            with self.subTest(mode=mode):
                outcome = self.execute(mode=mode)
                if mode == "environment-skip":
                    self.assert_accounted(*outcome, started=0)
                else:
                    self.assert_incomplete_error(*outcome)
                self.assertTrue(
                    any(
                        row[0] == "second-context-failure"
                        for process in outcome[2]
                        for row in process
                    )
                )

    def test_reverse_discovery_order_is_preserved_within_every_class(self):
        result, data, events = self.execute(mode="reverse-order")
        self.assert_accounted(result, data, events)
        for process in events:
            for index in range(5):
                prefix = f"{MODULE}.Wrapped{index}."
                bodies = [
                    row[1]
                    for row in process
                    if row[0] == "body" and row[1].startswith(prefix)
                ]
                self.assertEqual(bodies, sorted(bodies, reverse=True))

    def test_class_shape_drift_fallback_matches_unsharded_original_semantics(self):
        for mode in (
            "injected-skip",
            "deleted-setup",
            "deleted-class-setup",
            "deleted-helper",
            "plain-decorator",
            "class-decorator",
            "literal-attribute",
            "unknown-class",
            "duplicate-class",
        ):
            with self.subTest(mode=mode):
                result, data, events = self.execute(mode=mode)
                control, original, original_events = self.execute(
                    mode=mode, declared=False
                )
                self.assertEqual(
                    data["hook_mode"], "original-root", result.stdout + result.stderr
                )
                self.assertEqual(len(data["workers"]), 1)
                self.assertEqual(result.returncode, control.returncode)
                self.assertEqual(data["discovered"], original["discovered"])
                self.assertEqual(data["complete"], original["complete"])
                for field in (
                    "assigned",
                    "started",
                    "fixture_skipped",
                    "skip_events",
                    "execution_complete",
                    "exit",
                ):
                    self.assertEqual(
                        data["workers"][0][field], original["workers"][0][field], field
                    )

                def bodies(logs):
                    return sorted(
                        row[1]
                        for process in logs
                        for row in process
                        if row[0] == "body"
                    )

                self.assertEqual(bodies(events), bodies(original_events))
                for process in events + original_events:
                    self.assertEqual(process[-1][0], "restored")
                    self.assertNotIn("UNGUARDED", [row[0] for row in process])
                if mode == "injected-skip":
                    self.assertEqual(data["workers"][0]["started"], 87)
                    self.assertEqual(data["workers"][0]["skip_events"], 21)
                    self.assertEqual(len(bodies(events)), 66)
                if mode == "unknown-class":
                    self.assertEqual(data["discovered"], 92)

    def test_worker_configuration_drift_is_rejected_before_any_body(self):
        result, data, events = self.execute(mode="worker-config")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(row[0] == "body" for process in events for row in process))


if __name__ == "__main__":
    unittest.main()
