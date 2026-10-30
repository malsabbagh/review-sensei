"""Exercise the real urllib/HTTPSConnection stack without network I/O."""

import io
import ssl
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch
from urllib.request import Request

from review_sensei.errors import ProviderError
from review_sensei.models import ProviderRequest
from review_sensei.providers.openai_compatible import OpenAICompatibleProvider
from review_sensei.providers.openrouter import (
    OpenRouterProvider,
    OpenRouterRoutingPolicy,
)


class _Socket:
    def __init__(self, response):
        self.response = response
        self.sent = []

    def sendall(self, data):
        self.sent.append(bytes(data))

    def makefile(self, mode, *args):
        return io.BytesIO(self.response)

    def close(self):
        pass

    def setsockopt(self, *args):
        pass


class ProviderHTTPSContractTests(unittest.TestCase):
    @contextmanager
    def transport(self, *, proxy=False, redirect=False, certificate_error=False):
        with ExitStack() as stack:
            yield self._transport(
                stack,
                proxy=proxy,
                redirect=redirect,
                certificate_error=certificate_error,
            )

    def _transport(self, stack, *, proxy, redirect, certificate_error):
        body = b'{"choices":[{"message":{"content":"ok"}}]}'
        response = (
            b"HTTP/1.1 302 Found\r\nLocation: https://evil.example/v1\r\nContent-Length: 0\r\n\r\n"
            if redirect
            else b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        sock = _Socket(response)
        # CONNECT consumes its own response before TLS starts.
        if proxy:
            original_makefile = sock.makefile
            responses = [io.BytesIO(b"HTTP/1.1 200 Connection established\r\n\r\n")]

            def makefile(*args):
                return responses.pop() if responses else original_makefile(*args)

            sock.makefile = makefile
        stack.enter_context(
            patch(
                "urllib.request.getproxies",
                return_value={"https": "http://proxy:password@proxy.test:8080"}
                if proxy
                else {},
            )
        )
        stack.enter_context(patch("urllib.request.proxy_bypass", return_value=False))
        connect = stack.enter_context(
            patch("socket.create_connection", return_value=sock)
        )
        wrap = stack.enter_context(
            patch.object(
                ssl.SSLContext,
                "wrap_socket",
                side_effect=ssl.SSLCertVerificationError("synthetic rejection")
                if certificate_error
                else None,
                return_value=sock,
            )
        )
        return sock, connect, wrap

    def providers(self, base_url):
        if "openrouter.ai" in base_url:
            return OpenRouterProvider(
                base_url=base_url,
                model="test/model",
                api_key="synthetic-key",
                routing_policy=OpenRouterRoutingPolicy("openai"),
            )
        return OpenAICompatibleProvider(
            base_url=base_url,
            api_key="synthetic-key",
            allow_custom_endpoint="api.openai.com" not in base_url,
        )

    def test_default_handlers_parse_default_and_custom_ports(self):
        for url, host, port in (
            ("https://api.openai.com/v1", "api.openai.com", 443),
            ("https://api.openai.com:443/v1", "api.openai.com", 443),
            ("https://custom.example:8443/v1", "custom.example", 8443),
            ("https://custom.example:0/v1", "custom.example", 0),
            ("https://[::1]:8443/v1", "::1", 8443),
            ("https://openrouter.ai/api/v1", "openrouter.ai", 443),
            ("https://openrouter.ai:443/api/v1", "openrouter.ai", 443),
        ):
            with self.subTest(url=url):
                with self.transport() as transport:
                    _, connect, wrap = transport
                    provider = self.providers(url)
                    self.assertEqual(
                        provider.complete(ProviderRequest(prompt="synthetic")).text,
                        "ok",
                    )
                    self.assertEqual(connect.call_args.args[0], (host, port))
                    self.assertEqual(wrap.call_args.kwargs["server_hostname"], host)
                    self.assertTrue(provider._ssl_context.check_hostname)
                    self.assertEqual(
                        provider._ssl_context.verify_mode, ssl.CERT_REQUIRED
                    )

    def test_proxy_connect_verifies_origin_and_separates_credentials(self):
        for url in (
            "https://api.openai.com:443/v1",
            "https://custom.example:8443/v1",
            "https://openrouter.ai:443/api/v1",
        ):
            with self.subTest(url=url), self.transport(proxy=True) as transport:
                sock, connect, wrap = transport
                provider = self.providers(url)
                self.assertEqual(
                    provider.complete(ProviderRequest(prompt="synthetic")).text, "ok"
                )
                self.assertEqual(connect.call_args.args[0], ("proxy.test", 8080))
                host = (
                    "custom.example"
                    if "custom.example" in url
                    else "openrouter.ai"
                    if "openrouter.ai" in url
                    else "api.openai.com"
                )
                self.assertEqual(wrap.call_args.kwargs["server_hostname"], host)
                tunnel, origin = b"".join(sock.sent).split(b"\r\n\r\n", 1)
                self.assertIn(
                    f"CONNECT {host}:{8443 if host == 'custom.example' else 443}".encode(),
                    tunnel,
                )
                self.assertNotIn(b"synthetic-key", tunnel)
                self.assertIn(b"Proxy-Authorization:", tunnel)
                self.assertNotIn(b"Proxy-Authorization:", origin)
                self.assertIn(b"Bearer synthetic-key", origin)

    def test_certificate_failure_has_no_insecure_retry(self):
        _, connect, wrap = self.enterContext(self.transport(certificate_error=True))
        provider = self.providers("https://api.openai.com/v1")
        with self.assertRaises(ProviderError):
            provider.complete(ProviderRequest(prompt="synthetic"))
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(wrap.call_count, 1)

    def test_redirect_refused_without_second_connection(self):
        for url in ("https://api.openai.com/v1", "https://openrouter.ai/api/v1"):
            with self.subTest(url=url), self.transport(redirect=True) as transport:
                _, connect, _ = transport
                provider = self.providers(url)
                with self.assertRaisesRegex(ProviderError, "redirected"):
                    provider.complete(ProviderRequest(prompt="synthetic"))
                self.assertEqual(connect.call_count, 1)

    def test_destination_mutation_refused_before_connection(self):
        _, connect, _ = self.enterContext(self.transport())
        provider = self.providers("https://openrouter.ai/api/v1")
        for url in (
            "https://evil.example/api/v1",
            "https://openrouter.ai:8443/api/v1",
            "http://openrouter.ai/api/v1",
            "https://user@openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1?q=x",
            "https://openrouter.ai/api/v1#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                next(
                    handler
                    for handler in provider._safe_opener.handlers
                    if hasattr(handler, "https_open")
                ).https_open(Request(url))
        request = Request("https://openrouter.ai/api/v1")
        request.host = "evil.example:443"
        with self.assertRaises(ProviderError):
            provider._safe_opener.open(request, timeout=1)
        self.assertEqual(connect.call_count, 0)

    def test_openrouter_disallowed_port_rejected(self):
        with self.assertRaises(ValueError):
            self.providers("https://openrouter.ai:8443/api/v1")

    def test_explicit_zero_port_cannot_inherit_default_port_authority(self):
        _, connect, _ = self.enterContext(self.transport())
        provider = self.providers("https://custom.example:0/v1")
        with self.assertRaisesRegex(ProviderError, "TLS host mismatch"):
            provider._safe_opener.open(
                Request("https://custom.example:443/v1"), timeout=1
            )
        self.assertEqual(connect.call_count, 0)

    def test_tampered_connect_destination_refused_before_connection(self):
        _, connect, _ = self.enterContext(self.transport())
        provider = self.providers("https://api.openai.com/v1")
        request = Request("https://api.openai.com/v1")
        request.set_proxy("proxy.test:8080", "http")
        request._tunnel_host = "evil.example:443"
        with self.assertRaisesRegex(ProviderError, "TLS host mismatch"):
            provider._safe_opener.open(request, timeout=1)
        self.assertEqual(connect.call_count, 0)
