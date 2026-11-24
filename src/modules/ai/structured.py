"""One structured-output LLM call, shared by the readers that must not trust it.

Every "LLM reads, rules decide" component (matching judges, CV text repair)
needs the same three things: a one-method client that returns the model's
text for a strict JSON schema, an OpenAI implementation of it, and a
content-keyed cache so an unchanged input is never answered twice.

This is the synchronous, exact-schema counterpart of ``facade.generate_text``:
the caller picks the model, temperature is fixed at 0, and a schema the model
refuses is an error rather than an option to drop, because the reader's
verification depends on the shape of the answer.  Other modules import these
names from ``src.modules.ai.facade``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from src.core.pipeline_events import ai_call_timer
from src.modules.ai.cache_store import CacheStore


class StructuredClient(Protocol):
    """One completion call.  Returns the model's text; raises on transport failure."""

    def complete(self, *, instructions: str, input_text: str, output_schema: dict[str, Any]) -> str: ...


class OpenAIStructuredClient:
    """``StructuredClient`` on the OpenAI Responses API with a strict JSON schema.

    ``purpose`` names the call in ``ai_call_logs`` and ``schema_name`` labels
    the output format for the provider; neither affects the answer.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        purpose: str,
        schema_name: str,
        max_output_tokens: int = 600,
        client: object | None = None,
    ) -> None:
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
        self._client = client
        self._model = model
        self._purpose = purpose
        self._schema_name = schema_name
        self._max_output_tokens = max_output_tokens

    def complete(self, *, instructions: str, input_text: str, output_schema: dict[str, Any]) -> str:
        with ai_call_timer(
            self._purpose, provider="openai", model=self._model, input_text=input_text
        ) as call:
            response = self._client.responses.create(  # type: ignore[attr-defined]
                model=self._model,
                instructions=instructions,
                input=input_text,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": self._schema_name,
                        "strict": True,
                        "schema": output_schema,
                    }
                },
                temperature=0.0,
                max_output_tokens=self._max_output_tokens,
                store=False,
            )
            call.done(response.output_text, getattr(response, "usage", None))
        return response.output_text


class CachingStructuredClient:
    """Wrap a ``StructuredClient`` so one distinct input is answered once.

    The model is not deterministic at the margin, and two runs of an unchanged
    input must not disagree because of it.  The key covers the prompt version
    and model as well as the input, so a prompt change never serves an old
    answer.  Where the answers are kept is the store's business.
    """

    def __init__(
        self, inner: StructuredClient, *, model: str, prompt_version: str, store: CacheStore
    ) -> None:
        self._inner = inner
        self._model = model
        self._prompt_version = prompt_version
        self._store = store

    def _key(self, instructions: str, input_text: str) -> str:
        material = json.dumps(
            {
                "prompt_version": self._prompt_version,
                "model": self._model,
                "instructions": instructions,
                "input": input_text,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def complete(self, *, instructions: str, input_text: str, output_schema: dict[str, Any]) -> str:
        key = self._key(instructions, input_text)
        cached = self._store.get(key)
        if cached is not None:
            return cached
        answer = self._inner.complete(
            instructions=instructions, input_text=input_text, output_schema=output_schema
        )
        self._store.put(key, answer)
        return answer
