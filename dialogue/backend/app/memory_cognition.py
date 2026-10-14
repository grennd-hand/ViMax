"""Batched multilingual embeddings and bounded character memory retrieval."""
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Callable

import httpx

from .memory_store import SQLiteMemoryStore

_embedding_unavailable_until: dict[str, float] = {}


async def embed_texts(store: SQLiteMemoryStore, texts: list[str], *, model: str,
                     base_url: str, api_key: str, transport=None) -> tuple[dict[str, list[float]], str]:
    texts = list(dict.fromkeys(text for text in texts if text.strip()))
    vectors = store.cached_vectors(model, texts)
    missing = [text for text in texts if text not in vectors]
    if not missing:
        return vectors, "semantic"
    if not base_url or (transport is None and _embedding_unavailable_until.get(base_url, 0) > time.monotonic()):
        return vectors, "lexical"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=8) as client:
            for offset in range(0, len(missing), 32):
                batch = missing[offset:offset + 32]
                response = await client.post(base_url.rstrip("/") + "/embeddings",
                    headers={"Authorization": "Bearer " + api_key} if api_key else {},
                    json={"model": model, "dimensions": 512, "input": batch})
                response.raise_for_status()
                data = response.json().get("data")
                if not isinstance(data, list) or len(data) != len(batch):
                    raise ValueError("Invalid embedding batch")
                received = {}
                for item in data:
                    index, vector = item.get("index"), item.get("embedding")
                    if (type(index) is not int or not 0 <= index < len(batch) or index in received
                            or not isinstance(vector, list) or len(vector) != 512
                            or not all(type(value) in {int, float} and math.isfinite(value) for value in vector)):
                        raise ValueError("Invalid embedding vector or index")
                    received[index] = vector
                saved = {batch[index]: vector for index, vector in received.items()}
                store.save_vectors(model, saved)
                vectors.update(saved)
        return vectors, "semantic"
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        if transport is None:
            _embedding_unavailable_until[base_url] = time.monotonic() + 60
        return vectors, "lexical"


async def prepare_memory_context(store: SQLiteMemoryStore, run: dict, scene_query: str,
                                 embedder: Callable | None = None) -> dict:
    if not run.get("character_bindings") or not run.get("config", {}).get("memory_enabled"):
        return {"mode": "disabled", "roles": {}, "recalled": 0}
    model = os.getenv("MODEL_EMBEDDING_NAME", "text-embedding-3-small")
    queries = {role: (scene_query + " " + " ".join(str(run["config"]["persona_cards"][role].get(key, ""))
                                                 for key in ("priority", "decision_rule"))).strip()
               for role in run["character_bindings"]}
    rows = store.index_candidates(run, model)
    # A cold character needs no embedding request. Queries start when there is
    # an actual stored reflection to recall; cached phases cost no new request.
    has_memory = bool(rows) or any(profile["reflection_count"] for profile in store.profiles(run["config"]["story_world"]))
    if not has_memory and not run.get("memory_reflections"):
        return {"mode": "empty", "embedding_model": model, "roles": {role: [] for role in queries}, "recalled": 0}
    embedding_call = embedder or embed_texts
    try:
        vectors, mode = await asyncio.wait_for(embedding_call(
            store, list(queries.values()) + [row["insight"] for row in rows], model=model,
            base_url=os.getenv("MODEL_EMBEDDING_BASE_URL") or os.getenv("MODEL_API_BASE_URL", ""),
            api_key=os.getenv("MODEL_EMBEDDING_API_KEY") or os.getenv("MODEL_API_KEY", "")), timeout=25)
    except (TimeoutError, httpx.HTTPError, ValueError):
        vectors, mode = {}, "lexical"
    store.apply_vectors(rows, vectors, model)
    memories = {role: store.search(run, role, query, vectors.get(query), model, limit=3)
                for role, query in queries.items()}
    return {"mode": mode, "embedding_model": model, "roles": memories,
            "recalled": sum(len(items) for items in memories.values())}
