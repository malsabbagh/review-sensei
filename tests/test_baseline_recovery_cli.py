"""Checkout qualification of invalidated-baseline CLI recovery across processes."""

import unittest

try:
    from baseline_cli_contract import BaselineCLIContract
except ModuleNotFoundError:
    from tests.baseline_cli_contract import BaselineCLIContract


class BaselineRecoveryCLIContractTests(BaselineCLIContract, unittest.TestCase):
    pass
