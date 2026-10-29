"""Source/parent association is required before any narrow native body view."""

import copy
import unittest

from review_sensei_github.native_reader_projection import validate_reader_projection
from review_sensei_github.quarantine_published_tail import execution_manifest_digest

from review_sensei.errors import ReviewInputError


class NativeReaderProjectionTests(unittest.TestCase):
    def surfaces(self):
        return {
            "inline_replies": {"new_objects": 3},
            "reviews": {
                "new_objects": 5,
                "new_wire_bytes": 5120,
                "max_object_wire_bytes": 1024,
            },
        }

    def fixture(self):
        contract = {
            "executions": [
                {"run_id": 10, "route": "published-review", "source": None},
                {"run_id": 11, "route": "discussion", "source": {"root_id": 100}},
            ]
        }
        value = {
            "schema_version": "native-reader-projection-v1",
            "repository_id": 123,
            "pull_request": 7,
            "producer_id": 999,
            "execution_manifest_sha256": execution_manifest_digest(contract),
            "source_contract_sha256": "f" * 64,
            "executions": [
                {
                    "run_id": run_id,
                    "route": route,
                    "writer_source_sha256": "f" * 64,
                    "explicit_review_head_sha": "a" * 40,
                    "check_head_sha": "a" * 40,
                    "reply_parent_root_ids": [100],
                    "resolution_root_ids": [100],
                    "new_thread_roots": 0,
                }
                for run_id, route in ((10, "published-review"), (11, "discussion"))
            ],
            "reply_review_allocation": {
                "explicit_new_objects": 2,
                "explicit_new_wire_bytes": 2048,
                "implicit_new_objects": 3,
                "implicit_new_wire_bytes": 3072,
                "implicit_max_object_wire_bytes": 1024,
                "implicit_head_binding": "api-selected-head",
            },
            "roots": [
                {
                    "root_id": 100,
                    "thread_id": "thread-100",
                    "review_id": 200,
                    "head_sha": "a" * 40,
                }
            ],
        }
        return contract, value

    def test_full_reply_and_legacy_conflict_views_are_never_narrowed(self):
        contract, value = self.fixture()
        projection = validate_reader_projection(
            value,
            contract,
            source_contract_sha256="f" * 64,
            surface_exposure=self.surfaces(),
        )
        self.assertEqual(projection.body_domain("cleanup"), "complete-root-bodies")
        for purpose in ("conversation-replies", "legacy-report-conflicts"):
            self.assertEqual(
                projection.body_domain(purpose), "complete-inline-conversations"
            )
        with self.assertRaises(ReviewInputError):
            projection.body_domain("unknown-optimistic-reader")

    def test_older_parent_origin_is_independent_of_explicit_and_api_selected_heads(
        self,
    ):
        contract, value = self.fixture()
        value["roots"][0]["head_sha"] = "d" * 40
        projection = validate_reader_projection(
            value,
            contract,
            source_contract_sha256="f" * 64,
            surface_exposure=self.surfaces(),
        )
        self.assertEqual(projection.roots[0].head_sha, "d" * 40)
        self.assertEqual(projection.fixed_explicit_review_heads, ("a" * 40,))
        self.assertEqual(projection.implicit_review_objects, 3)

    def test_explicit_review_only_reserve_cannot_cover_reply_associated_reviews(self):
        contract, value = self.fixture()
        for surface, replacement in (
            ("new_objects", 2),
            ("new_wire_bytes", 2048),
            ("max_object_wire_bytes", 512),
        ):
            exposure = self.surfaces()
            exposure["reviews"][surface] = replacement
            with self.subTest(surface=surface), self.assertRaises(ReviewInputError):
                validate_reader_projection(
                    value,
                    contract,
                    source_contract_sha256="f" * 64,
                    surface_exposure=exposure,
                )

    def test_fresh_scope_cessation_and_parent_association_are_all_independent(self):
        contract, value = self.fixture()
        projection = validate_reader_projection(
            value,
            contract,
            source_contract_sha256="f" * 64,
            surface_exposure=self.surfaces(),
        )
        arguments = dict(
            repository_id=123,
            pull_request=7,
            producer_id=999,
            successor_head="b" * 40,
            execution_manifest_sha256=value["execution_manifest_sha256"],
            ceased_census_sha256="c" * 64,
            current_census_sha256="c" * 64,
            rows=({**value["roots"][0], "producer_id": 999},),
        )
        projection.require_fresh_associations(**arguments)
        for key, replacement in (
            ("repository_id", 124),
            ("producer_id", 998),
            ("successor_head", "a" * 40),
            ("execution_manifest_sha256", "d" * 64),
            ("current_census_sha256", "d" * 64),
            ("rows", ()),
            ("rows", ({**arguments["rows"][0], "review_id": 201},)),
        ):
            with self.subTest(key=key), self.assertRaises(ReviewInputError):
                projection.require_fresh_associations(**{**arguments, key: replacement})

    def test_missing_routes_wrong_parents_new_roots_and_unbound_source_refuse(self):
        contract, value = self.fixture()
        changes = [
            lambda v: v["executions"].pop(),
            lambda v: v["executions"][1].update(reply_parent_root_ids=[]),
            lambda v: v["executions"][1].update(new_thread_roots=1),
            lambda v: v["executions"][0].update(writer_source_sha256="e" * 64),
            lambda v: v["roots"][0].update(head_sha="z" * 40),
            lambda v: v["roots"].append(copy.deepcopy(v["roots"][0])),
        ]
        for change in changes:
            altered = copy.deepcopy(value)
            change(altered)
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                validate_reader_projection(
                    altered,
                    contract,
                    source_contract_sha256="f" * 64,
                    surface_exposure=self.surfaces(),
                )
