import unittest

from review_sensei.errors import ProviderError
from review_sensei.providers.transport import read_bounded_body


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


if __name__ == "__main__":
    unittest.main()
