import gc
import io
import ssl
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from urllib.request import Request

from review_sensei.errors import ProviderError
from review_sensei.providers.transport import (
    CancellationSafeHTTPHandler,
    CancellationSafeHTTPSHandler,
    DestinationBoundHTTPSHandler,
    read_bounded_body,
)


class _ChunkedResponse:
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)

    def read(self, size: int):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        return chunk[:size]


class _IgnoringSizeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.done = False

    def read(self, size: int):
        if self.done:
            return b""
        self.done = True
        return self.body


class ReadBoundedBodyTests(unittest.TestCase):
    def test_accepts_exact_maximum(self):
        body = read_bounded_body(_ChunkedResponse([b"abcde"]), 5, label="response")
        self.assertEqual(bytes(body), b"abcde")

    def test_rejects_one_byte_over_maximum_in_a_single_chunk(self):
        with self.assertRaisesRegex(ProviderError, "exceeded the configured size"):
            read_bounded_body(_ChunkedResponse([b"abcdef"]), 5, label="response")

    def test_rejects_one_byte_over_maximum_after_exact_fill(self):
        with self.assertRaisesRegex(ProviderError, "exceeded the configured size"):
            read_bounded_body(_ChunkedResponse([b"abcd", b"ef"]), 5, label="response")

    def test_rejects_two_byte_probe_when_one_byte_remains(self):
        with self.assertRaisesRegex(ProviderError, "exceeded the configured size"):
            read_bounded_body(
                _IgnoringSizeResponse(b"xy"),
                1,
                label="response",
            )

    def test_rejects_response_that_ignores_requested_size(self):
        with self.assertRaisesRegex(ProviderError, "exceeded the configured size"):
            read_bounded_body(_IgnoringSizeResponse(b"x" * 8), 4, label="response")


class ResponseConstructionTests(unittest.TestCase):
    def handlers(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return (
            (CancellationSafeHTTPHandler(), "http"),
            (CancellationSafeHTTPSHandler(context), "https"),
            (
                DestinationBoundHTTPSHandler(
                    context,
                    expected_hostname="synthetic.test",
                    expected_port=443,
                    label="Synthetic",
                ),
                "https",
            ),
        )

    @contextmanager
    def transport(self, *, error=None, body=None):
        class Socket:
            def __init__(self):
                self.file = body or io.BytesIO(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n"
                    b"Connection: close\r\n\r\nabc"
                )
                self.closed = False

            def sendall(self, data):
                pass

            def setsockopt(self, *args):
                pass

            def makefile(self, *args):
                if error is not None:
                    raise error
                return self.file

            def close(self):
                self.closed = True

        sock = Socket()
        with (
            patch("socket.create_connection", return_value=sock),
            patch.object(ssl.SSLContext, "wrap_socket", return_value=sock),
        ):
            yield sock

    def open(self, handler, scheme):
        request = Request(f"{scheme}://synthetic.test/", data=b"synthetic")
        request.timeout = 1
        return getattr(handler, f"{scheme}_open")(request)

    def test_constructor_failures_propagate_without_finalizer_errors(self):
        for handler, scheme in self.handlers():
            for error in (KeyboardInterrupt(), SystemExit(130), OSError("synthetic")):
                unraisable = []
                with (
                    self.subTest(handler=type(handler).__name__, error=type(error)),
                    patch.object(sys, "unraisablehook", unraisable.append),
                    self.transport(error=error) as sock,
                ):
                    with self.assertRaises(type(error)) as caught:
                        self.open(handler, scheme)
                    self.assertIs(caught.exception, error)
                    # Release the interrupted constructor's traceback while the
                    # hook can still observe any IOBase finalization failure.
                    error.__traceback__ = None
                    gc.collect()
                    self.assertEqual(unraisable, [])
                    self.assertTrue(sock.closed)

    def test_initialized_responses_keep_body_and_close_behavior(self):
        for handler, scheme in self.handlers():
            with self.subTest(handler=type(handler).__name__), self.transport() as sock:
                with self.open(handler, scheme) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"abc")
                self.assertTrue(response.closed)
                self.assertTrue(sock.file.closed)
                self.assertTrue(sock.closed)

    def test_initialized_response_read_errors_are_not_masked(self):
        class BrokenBody(io.BytesIO):
            def read(self, size=-1):
                raise OSError("synthetic body failure")

        for handler, scheme in self.handlers():
            body = BrokenBody(
                b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc"
            )
            with (
                self.subTest(handler=type(handler).__name__),
                self.transport(body=body),
                self.open(handler, scheme) as response,
                self.assertRaisesRegex(OSError, "synthetic body failure"),
            ):
                response.read()
            self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
