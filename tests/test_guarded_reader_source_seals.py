"""Guarded planner readers use their defining source, including installed wrappers."""

import unittest
from unittest.mock import patch

from review_sensei_github.quarantine_evidence import CurrentReadScope
from review_sensei_github.quarantine_holds import QuarantineHoldReader
from review_sensei_github.quarantine_origin import QuarantineOriginReader

from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_instance_source


class GuardedReaderSourceSealTests(unittest.TestCase):
    def test_installed_readers_are_sealed_and_class_or_instance_changes_reject(self):
        for cls, method in (
            (CurrentReadScope, "require"),
            (QuarantineHoldReader, "read"),
            (QuarantineOriginReader, "read"),
        ):
            with self.subTest(reader=cls.__name__):
                instance = object.__new__(cls)
                require_instance_source(instance, methods=(method,))
                with patch.object(cls, method, lambda *args, **kwargs: None):
                    with self.assertRaises(ReviewInputError):
                        require_instance_source(instance, methods=(method,))
                # Frozen dataclass readers cannot be modified normally either;
                # explicit object mutation still cannot change the source proof.
                object.__setattr__(instance, method, lambda *args, **kwargs: None)
                with self.assertRaises(ReviewInputError):
                    require_instance_source(instance, methods=(method,))
                object.__delattr__(instance, method)
                require_instance_source(instance, methods=(method,))
