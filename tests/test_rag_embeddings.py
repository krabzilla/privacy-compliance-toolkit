"""Embedder tests -- FakeEmbedder determinism and SentenceTransformerEmbedder no-load-on-construct."""
from __future__ import annotations

import pytest

from src.rag.embeddings import FakeEmbedder, SentenceTransformerEmbedder


class TestFakeEmbedder:
    def test_same_text_same_vector(self) -> None:
        e = FakeEmbedder(dim=32)
        assert e.embed(["hello world"]) == e.embed(["hello world"])

    def test_different_text_different_vector(self) -> None:
        e = FakeEmbedder(dim=32)
        a = e.embed(["GDPR Article 6"])[0]
        b = e.embed(["NIST CSF GV.OC-01"])[0]
        assert a != b

    @pytest.mark.parametrize("dim", [4, 32, 64, 384])
    def test_dim_respected(self, dim) -> None:
        e = FakeEmbedder(dim=dim)
        v = e.embed(["any text"])[0]
        assert len(v) == dim

    def test_values_in_unit_interval(self) -> None:
        e = FakeEmbedder(dim=64)
        v = e.embed(["sample"])[0]
        assert all(-1.0 <= x <= 1.0 for x in v)

    def test_batch_preserves_order(self) -> None:
        e = FakeEmbedder(dim=8)
        texts = ["a", "b", "c"]
        out = e.embed(texts)
        # Each one matches what single-text embed would have produced.
        for i, t in enumerate(texts):
            assert out[i] == e.embed([t])[0]

    def test_rejects_tiny_dim(self) -> None:
        with pytest.raises(ValueError):
            FakeEmbedder(dim=2)


class TestSentenceTransformerEmbedder:
    def test_construct_does_not_load_model(self) -> None:
        # Building the embedder must NOT pull in sentence-transformers or
        # download a model. Loading is deferred to first embed() call.
        e = SentenceTransformerEmbedder()
        assert e._model is None
        assert e.model_name == "all-MiniLM-L12-v2"
        assert e.dim == 384

    def test_construct_with_custom_model_name(self) -> None:
        e = SentenceTransformerEmbedder(model_name="custom/model")
        assert e.model_name == "custom/model"
        assert e._model is None
