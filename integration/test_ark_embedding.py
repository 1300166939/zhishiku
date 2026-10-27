"""Offline tests for vector-space validation and credential-safe failures."""
import io
import json
import unittest
import urllib.error

from ark_embedding import ArkEmbedding, EmbeddingError


class FakeOpener:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.request = data, error, None

    def open(self, request, timeout):
        self.request = request
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.data).encode())


class AdapterTest(unittest.TestCase):
    def client(self, vector, model="test-model"):
        client = ArkEmbedding("synthetic-secret", "test-model", dimensions=2)
        client._opener = FakeOpener({"data": {"embedding": vector}, "model": model})
        return client

    def test_text_and_image_use_same_model_and_dimension(self):
        client = self.client([0.1, 0.2])
        self.assertEqual(len(client.embed_text("query").vector), 2)
        client.embed_image(b"synthetic", instructions="test instruction")
        body = json.loads(client._opener.request.data)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["dimensions"], 2)
        self.assertEqual(body["input"][0]["type"], "image_url")

    def test_incompatible_or_invalid_vectors_are_rejected(self):
        for vector in ([1], [0, 0], [float("nan"), 1], [True, 1], ["a", 1]):
            with self.subTest(vector=vector), self.assertRaises(EmbeddingError):
                self.client(vector).embed_text("query")
        with self.assertRaises(EmbeddingError):
            self.client([1, 2], model="different-model").embed_text("query")

    def test_http_error_never_displays_raw_secret(self):
        client = self.client([1, 2])
        client._opener = FakeOpener(error=urllib.error.HTTPError(
            client.url, 401, "unauthorized", {}, io.BytesIO(json.dumps({
                "error": {"code": "synthetic-secret", "message": "synthetic-secret"}
            }).encode())))
        with self.assertRaises(EmbeddingError) as caught:
            client.embed_text("query")
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_unverified_api_host_is_rejected(self):
        with self.assertRaises(EmbeddingError):
            ArkEmbedding("synthetic-secret", "test-model", base_url="https://example.com/api/v3")


if __name__ == "__main__":
    unittest.main()
