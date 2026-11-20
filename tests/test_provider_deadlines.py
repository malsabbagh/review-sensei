"""Run the same provider deadline contract in the checkout lane."""

import socket
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tests.provider_deadline_contract import (
    ProviderDeadlineContractTests,  # noqa: F401
    _NumericLoopbackHTTPServer,
)


class ProviderDeadlineFixtureTests(unittest.TestCase):
    def test_numeric_listener_avoids_synthetic_resolver_delay_and_binds(self):
        lookups = []

        def slow_lookup(host):
            # Account for a synthetic four-second resolver wait without making
            # a timing-sensitive assertion or actually sleeping.
            lookups.append((host, 4))
            return "localhost"

        with patch.object(socket, "getfqdn", side_effect=slow_lookup):
            with ThreadingHTTPServer(
                ("127.0.0.1", 0), BaseHTTPRequestHandler
            ) as control:
                self.assertEqual(control.server_name, "localhost")
            with _NumericLoopbackHTTPServer(
                ("127.0.0.1", 0), BaseHTTPRequestHandler
            ) as listener:
                address = listener.socket.getsockname()
                self.assertEqual(address, (listener.server_name, listener.server_port))
                self.assertEqual(listener.server_name, "127.0.0.1")
                self.assertGreater(listener.server_port, 0)
                with socket.create_connection(address, timeout=1):
                    pass
        self.assertEqual(lookups, [("127.0.0.1", 4)])
