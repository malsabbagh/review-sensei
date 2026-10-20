"""Run identical real-adapter engine journeys in the checkout lane."""

try:
    import provider_common_flow_contract as contract
except ModuleNotFoundError:
    from tests import provider_common_flow_contract as contract


def load_tests(loader, tests, pattern):
    return contract.load_common_flow_tests(loader)
