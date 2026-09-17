import sys
from types import SimpleNamespace

import pytest

from src.modules.matching.rag.embedding import OpenAIEmbeddingAdapter, build_embedding_adapter_from_env
from src.modules.matching.rag.llm import LLMService


class FakeOpenAI:
    def __init__(self, api_key):
        self.api_key = api_key
        self.responses = SimpleNamespace(create=self.create_response)
        self.embeddings = SimpleNamespace(create=self.create_embeddings)
        self.response_calls = []
        self.embedding_calls = []

    def create_response(self, **kwargs):
        self.response_calls.append(kwargs)
        return SimpleNamespace(output_text="OpenAI answer")

    def create_embeddings(self, **kwargs):
        self.embedding_calls.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[0.3, 0.4]),
                SimpleNamespace(index=0, embedding=[0.1, 0.2]),
            ]
        )


@pytest.fixture
def fake_openai(monkeypatch):
    created = []

    def factory(api_key):
        client = FakeOpenAI(api_key)
        created.append(client)
        return client

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
    return created


def test_llm_uses_openai_responses_api(monkeypatch, fake_openai):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-5.4-mini")

    assert LLMService().generate_text("Create a question") == "OpenAI answer"
    assert fake_openai[0].response_calls == [
        {"model": "gpt-5.4-mini", "input": "Create a question", "store": False}
    ]


def test_embedding_uses_openai_with_configured_dimensions(monkeypatch, fake_openai):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")

    adapter = build_embedding_adapter_from_env()

    assert isinstance(adapter, OpenAIEmbeddingAdapter)
    assert adapter.embed_texts(["CV", "JD"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert fake_openai[0].embedding_calls == [
        {"model": "text-embedding-3-small", "input": ["CV", "JD"], "dimensions": 1024}
    ]


def test_embedding_cache_reuses_vectors_across_adapter_instances(tmp_path, monkeypatch, fake_openai):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cache_dir = tmp_path / "embedding-cache"

    first = OpenAIEmbeddingAdapter(
        model_name="text-embedding-3-small",
        dimensions=2,
        cache_dir=cache_dir,
    )
    assert first.embed_texts(["CV", "JD"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert len(fake_openai[0].embedding_calls) == 1

    second = OpenAIEmbeddingAdapter(
        model_name="text-embedding-3-small",
        dimensions=2,
        cache_dir=cache_dir,
    )
    assert second.embed_texts(["CV", "JD"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert len(fake_openai[1].embedding_calls) == 0


def test_bedrock_providers_are_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    with pytest.raises(ValueError, match="Only 'openai' is supported"):
        LLMService()

    monkeypatch.setenv("EMBEDDING_PROVIDER", "bedrock")
    with pytest.raises(ValueError, match="Only 'openai' is supported"):
        build_embedding_adapter_from_env()
