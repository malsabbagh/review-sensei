"""The hosted-only exemption proves a closed, unchanged core build graph."""

import base64
import copy
import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import python_release_sources as sources  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENGINE, HOSTED = "a" * 40, "b" * 40
ROOT_TREE, OTHER_TREE, SRC_TREE = "c" * 40, "d" * 40, "e" * 40


class PythonReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.blobs = {}
        entries = [{"path": "src", "type": "tree", "mode": "040000", "sha": SRC_TREE}]
        for name in (
            "pyproject.toml",
            "setup.py",
            "MANIFEST.in",
            "README.md",
            "LICENSE",
        ):
            entries.append(self.blob(name, (ROOT / name).read_bytes()))
        self.root = {"sha": ROOT_TREE, "truncated": False, "tree": entries}
        self.other = {**copy.deepcopy(self.root), "sha": OTHER_TREE}
        self.src = {
            "sha": SRC_TREE,
            "truncated": False,
            "tree": [
                {
                    "path": "review_sensei",
                    "type": "tree",
                    "mode": "040000",
                    "sha": "f" * 40,
                },
                {
                    "path": "review_sensei/__init__.py",
                    "type": "blob",
                    "mode": "100644",
                    "sha": "1" * 40,
                },
            ],
        }
        self.enterContext(patch.object(sources.docs, "api", side_effect=self.api))

    def blob(self, name, raw):
        digest = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        self.blobs[digest] = {
            "sha": digest,
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode(),
        }
        return {"path": name, "type": "blob", "mode": "100644", "sha": digest}

    def api(self, path):
        if path.startswith("git/commits/"):
            source = path.rsplit("/", 1)[-1]
            return {
                "sha": source,
                "tree": {"sha": ROOT_TREE if source == ENGINE else OTHER_TREE},
            }
        if path == "git/trees/" + ROOT_TREE:
            return self.root
        if path == "git/trees/" + OTHER_TREE:
            return self.other
        if path == f"git/trees/{SRC_TREE}?recursive=1":
            return self.src
        if path.startswith("git/blobs/"):
            return self.blobs[path.rsplit("/", 1)[-1]]
        raise AssertionError(path)

    def test_same_source_preserves_existing_paired_path_without_new_recipe_gate(self):
        with patch.object(sources.docs, "api") as read:
            result = sources.core_equivalence(ENGINE, ENGINE)
        self.assertEqual(result["mode"], "same-source")
        read.assert_not_called()

    def test_hosted_only_reuses_core_with_identical_recognized_build_inputs(self):
        result = sources.core_equivalence(ENGINE, HOSTED)
        self.assertEqual(result["mode"], "recognized-build-input-equivalence")
        self.assertEqual(result["inputs"]["src"]["sha"], SRC_TREE)
        self.assertIsNone(result["inputs"]["setup.cfg"])

    def test_changed_core_or_sibling_package_attributes_or_recipe_cannot_hide(self):
        for name in (
            "src",
            "pyproject.toml",
            "setup.py",
            "MANIFEST.in",
            "README.md",
            "LICENSE",
        ):
            with self.subTest(name=name):
                original = copy.deepcopy(self.other)
                next(item for item in self.other["tree"] if item["path"] == name)[
                    "sha"
                ] = "2" * 40
                with self.assertRaisesRegex(ValueError, "changes published core"):
                    sources.core_equivalence(ENGINE, HOSTED)
                self.other = original
        self.other["tree"].append(self.blob(".gitattributes", b"* text=auto\n"))
        with self.assertRaisesRegex(ValueError, "changes published core"):
            sources.core_equivalence(ENGINE, HOSTED)

    def test_missing_required_or_nonregular_build_input_fails(self):
        self.other["tree"] = [
            item for item in self.other["tree"] if item["path"] != "README.md"
        ]
        with self.assertRaisesRegex(ValueError, "missing"):
            sources.core_equivalence(ENGINE, HOSTED)
        self.other = {**copy.deepcopy(self.root), "sha": OTHER_TREE}
        self.other["tree"][1]["mode"] = "120000"
        with self.assertRaisesRegex(ValueError, "regular"):
            sources.core_equivalence(ENGINE, HOSTED)

    def test_truncated_duplicate_or_symlink_src_inventory_fails_closed(self):
        for modification in ("truncated", "duplicate", "symlink", "gitlink"):
            with self.subTest(modification=modification):
                original = copy.deepcopy(self.src)
                if modification == "truncated":
                    self.src["truncated"] = True
                elif modification == "duplicate":
                    self.src["tree"].append(copy.deepcopy(self.src["tree"][0]))
                elif modification == "symlink":
                    self.src["tree"][1]["mode"] = "120000"
                else:
                    self.src["tree"][1].update(mode="160000", type="commit")
                with self.assertRaises(ValueError):
                    sources.core_equivalence(ENGINE, HOSTED)
                self.src = original

    def test_identical_custom_backend_or_setup_does_not_establish_build_closure(self):
        cases = [
            (
                "setup.py",
                b"from setuptools import setup\nfrom helper import build\nsetup(cmdclass=build)\n",
            ),
            (
                "pyproject.toml",
                (ROOT / "pyproject.toml")
                .read_bytes()
                .replace(b"setuptools.build_meta", b"custom.build_meta"),
            ),
        ]
        for name, raw in cases:
            with self.subTest(name=name):
                original = copy.deepcopy(self.root)
                item = self.blob(name, raw)
                self.root["tree"] = [
                    item if old["path"] == name else old for old in self.root["tree"]
                ]
                self.other = {**copy.deepcopy(self.root), "sha": OTHER_TREE}
                with self.assertRaises(ValueError):
                    sources.core_equivalence(ENGINE, HOSTED)
                self.root = original
                self.other = {**copy.deepcopy(self.root), "sha": OTHER_TREE}

    def test_identical_setup_cfg_still_requires_reviewed_build_graph(self):
        entry = self.blob("setup.cfg", b"[options]\npy_modules=outside\n")
        self.root["tree"].append(entry)
        self.other["tree"].append(copy.deepcopy(entry))
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            sources.core_equivalence(ENGINE, HOSTED)

    def test_metadata_dynamic_external_license_and_custom_setuptools_are_refused(self):
        canonical = (ROOT / "pyproject.toml").read_bytes().replace(b"\r\n", b"\n")
        for newline in (b"\n", b"\r\n"):
            original = canonical.replace(b"\n", newline)
            variants = (
                original.replace(
                    b'build-backend = "setuptools.build_meta"',
                    b'build-backend = "setuptools.build_meta"\nbackend-path=["outside"]',
                ),
                original.replace(b"[project]", b'[project]\ndynamic=["version"]'),
                original.replace(b'readme = "README.md"', b'readme = "OUTSIDE.md"'),
                original.replace(b'license = "MIT"', b'license = {file="OUTSIDE"}'),
                original + b'\n[tool.setuptools.data-files]\nroot=["outside"]\n',
            )
            for index, raw in enumerate(variants):
                with self.subTest(newline=newline, index=index):
                    self.assertNotEqual(
                        raw, original, "negative fixture must mutate input"
                    )
                    item = self.blob("pyproject.toml", raw)
                    self.root["tree"] = [
                        item if old["path"] == "pyproject.toml" else old
                        for old in self.root["tree"]
                    ]
                    self.other = {**copy.deepcopy(self.root), "sha": OTHER_TREE}
                    with self.assertRaises(ValueError):
                        sources.core_equivalence(ENGINE, HOSTED)

    def test_root_inventory_truncation_and_duplicate_paths_are_refused(self):
        self.root["truncated"] = True
        with self.assertRaisesRegex(ValueError, "incomplete"):
            sources.core_equivalence(ENGINE, HOSTED)
        self.root["truncated"] = False
        self.root["tree"].append(copy.deepcopy(self.root["tree"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            sources.core_equivalence(ENGINE, HOSTED)


class RegistryCoreContractTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / ".github/workflows/review-sensei-run.yml").read_bytes()

    def test_actual_reviewed_workflow_binds_registry_only_core_contract(self):
        proof = sources.registry_core_contract(self.raw)
        self.assertEqual(proof["mode"], "pypi-exact-v1")
        self.assertEqual(proof["installation_lanes"], 4)
        self.assertEqual(proof["workflow_sha256"], hashlib.sha256(self.raw).hexdigest())
        with patch.object(
            sources, "workflow_source_bytes", return_value=(self.raw, "d" * 40)
        ):
            bound = sources.core_installation(ENGINE, HOSTED)
        self.assertEqual(bound["engine_source"], ENGINE)
        self.assertEqual(bound["workflow_source"], HOSTED)
        self.assertEqual(bound["workflow_blob_sha"], "d" * 40)

    def test_one_source_fallback_or_arbitrary_workflow_change_is_not_admitted(self):
        for raw in (
            self.raw + b"\n# unreviewed byte change\n",
            self.raw.replace(b"\n", b"\r\n"),
            self.raw.replace(
                b'install_source="PyPI"',
                b'install_source="PyPI"\n          pip install git+https://github.com/malsabbagh/review-sensei.git@main',
                1,
            ),
            self.raw.replace(b"pypi-exact-v1", b"caller-asserted-mode"),
            self.raw + b"\n# reviewsensei-core-installation: pypi-exact-v1\n",
        ):
            with (
                self.subTest(digest=hashlib.sha256(raw).hexdigest()),
                self.assertRaises(ValueError),
            ):
                sources.registry_core_contract(raw)

    def test_legacy_without_selector_does_not_claim_new_contract(self):
        self.assertIsNone(sources.registry_core_contract(b"name: old workflow\n"))

    def test_contract_reads_verified_git_blob_from_exact_regular_path(self):
        raw = self.raw
        digest = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        trees = ["c" * 40, "d" * 40, "e" * 40]
        data = {f"git/commits/{HOSTED}": {"sha": HOSTED, "tree": {"sha": trees[0]}}}
        for index, name in enumerate((".github", "workflows", "review-sensei-run.yml")):
            leaf = index == 2
            data[f"git/trees/{trees[index]}"] = {
                "sha": trees[index],
                "truncated": False,
                "tree": [
                    {
                        "path": name,
                        "type": "blob" if leaf else "tree",
                        "mode": "100644" if leaf else "040000",
                        "sha": digest if leaf else trees[index + 1],
                    }
                ],
            }
        data[f"git/blobs/{digest}"] = {
            "sha": digest,
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode(),
        }
        original = copy.deepcopy(data)
        with patch.object(sources.docs, "api", side_effect=lambda path: data[path]):
            proof = sources.core_installation(ENGINE, HOSTED)
            self.assertEqual(proof["workflow_blob_sha"], digest)
            leaf = data[f"git/trees/{trees[2]}"]["tree"][0]
            leaf["mode"] = "120000"
            with self.assertRaises(ValueError):
                sources.core_installation(ENGINE, HOSTED)
            leaf["mode"] = "100644"
            data[f"git/blobs/{digest}"]["content"] = base64.b64encode(
                raw + b"forged"
            ).decode()
            with self.assertRaises(ValueError):
                sources.core_installation(ENGINE, HOSTED)
        for mutation in (
            "commit",
            "tree",
            "truncated",
            "duplicate",
            "gitlink",
            "blob-size",
            "blob-type",
        ):
            data = copy.deepcopy(original)
            if mutation == "commit":
                data[f"git/commits/{HOSTED}"]["tree"] = None
            elif mutation == "tree":
                data[f"git/trees/{trees[0]}"]["sha"] = "f" * 40
            elif mutation == "truncated":
                data[f"git/trees/{trees[0]}"]["truncated"] = True
            elif mutation == "duplicate":
                data[f"git/trees/{trees[0]}"]["tree"] *= 2
            elif mutation == "gitlink":
                data[f"git/trees/{trees[2]}"]["tree"][0].update(
                    type="commit", mode="160000"
                )
            elif mutation == "blob-size":
                data[f"git/blobs/{digest}"]["size"] += 1
            else:
                data[f"git/blobs/{digest}"]["content"] = None
            with (
                self.subTest(mutation=mutation),
                patch.object(sources.docs, "api", side_effect=lambda path: data[path]),
                self.assertRaises(ValueError),
            ):
                sources.core_installation(ENGINE, HOSTED)


if __name__ == "__main__":
    unittest.main()
