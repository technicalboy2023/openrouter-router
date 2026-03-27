"""
provider_openrouter.py
======================
Production-grade OpenRouter provider for the LLM gateway.

Features:
  • Async (httpx) – zero blocking I/O
  • TRUE API streaming (SSE passthrough from OpenRouter)
  • Weighted key selection based on per-key health scores
  • Multi-layer retry: same-key retries → next key → exhaustion
  • Exponential back-off between same-key retries
  • Auto-cooldown for rate-limited / forbidden keys
  • Token extraction from API response for accurate usage tracking
  • Graceful degradation on partial / invalid JSON
  • Structured logging on every request & error path
  • Model list fetched from OpenRouter's /models endpoint
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from router_core import (
    KeyRegistry,
    ResponseCache,
    UsageStore,
    backoff_sleep,
    estimate_tokens,
    get_http_client,
    log,
    new_completion_id,
    new_request_id,
    MAX_RETRIES,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_ENDPOINT       = f"{OPENROUTER_BASE_URL}/chat/completions"
MODELS_ENDPOINT     = f"{OPENROUTER_BASE_URL}/models"
DEFAULT_MODEL       = "openrouter/auto"

# HTTP status codes that mean "this key is dead for now"
_COOLING_STATUSES   = {429, 402, 403}
# HTTP status codes worth retrying with the same key
_TRANSIENT_STATUSES = {500, 502, 503, 504}


# ─────────────────────────────────────────────────────────────────────────────
# LOAD API KEYS FROM ENVIRONMENT
# ─────────────────────────────────────────────────────────────────────────────

def _load_keys() -> List[str]:
    keys: List[str] = []
    # Support up to 20 numbered keys
    for i in range(1, 21):
        k = os.getenv(f"OPENROUTER_KEY_{i}")
        if k and k.strip():
            keys.append(k.strip())
    # Also accept a comma-separated OPENROUTER_KEYS env var
    bulk = os.getenv("OPENROUTER_KEYS", "")
    if bulk:
        keys.extend(k.strip() for k in bulk.split(",") if k.strip())
    return list(dict.fromkeys(keys))   # deduplicate, preserve order


# ─────────────────────────────────────────────────────────────────────────────
# OPENROUTER PROVIDER CLASS
# ─────────────────────────────────────────────────────────────────────────────

class OpenRouterProvider:
    """
    Singleton-style provider object.  Instantiated once at application startup
    and reused across all requests.
    """

    def __init__(self) -> None:
        raw_keys = _load_keys()
        self.registry  = KeyRegistry(raw_keys)
        self.cache     = ResponseCache(ttl=30, max_size=512)
        self.store     = UsageStore()
        self._load_persisted()
        log.info(
            "OpenRouterProvider ready",
            extra={"keys_loaded": len(raw_keys), "keys_valid": len(self.registry)},
        )

    # ── boot ────────────────────────────────────────────────────────────────

    def _load_persisted(self) -> None:
        seed = self.store.get_registry_seed(self.registry)
        if seed:
            self.registry.load_persisted(seed)
            log.info("Restored usage counters from disk", extra={"keys": len(seed)})

    # ── private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _build_headers(key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
            "X-Title":       "LLM-Gateway",
        }

    @staticmethod
    def _extract_tokens(data: Dict[str, Any]) -> int:
        """Pull token count out of a standard OpenAI-style response."""
        try:
            return data.get("usage", {}).get("total_tokens", 0)
        except Exception:
            return 0

    @staticmethod
    def _safe_content(data: Dict[str, Any]) -> str:
        """Safely extract assistant content from a completion response."""
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""

    # ── non-streaming call ───────────────────────────────────────────────────

    async def call(
        self,
        messages: List[Dict],
        model: str,
        extra_params: Optional[Dict] = None,
        request_id: Optional[str] = None,
        use_cache: bool = True,
    ) -> str:
        """
        Execute a non-streaming chat completion.

        Retry strategy:
          For each available key (best-health-score first):
            → up to MAX_RETRIES attempts with exponential back-off
            → on 429/403: freeze key, move to next immediately
            → on 5xx:     retry same key with back-off
            → on success: update registry + persist usage
        """
        request_id = request_id or new_request_id()

        # ── cache check ──────────────────────────────────────────────────────
        if use_cache:
            cached = self.cache.get(messages, model)
            if cached is not None:
                log.info(
                    "Cache hit",
                    extra={"request_id": request_id, "model": model},
                )
                return cached

        payload: Dict[str, Any] = {
            "model":    model,
            "messages": messages,
            "stream":   False,
        }
        if extra_params:
            payload.update(extra_params)

        client = get_http_client()
        keys   = self.registry.ranked_keys()

        if not keys:
            log.error("No API keys configured", extra={"request_id": request_id})
            return "Error: No OpenRouter API keys configured."

        for key in keys:
            for attempt in range(MAX_RETRIES):
                t0 = time.perf_counter()
                try:
                    response = await client.post(
                        CHAT_ENDPOINT,
                        headers=self._build_headers(key),
                        json=payload,
                    )
                    latency = time.perf_counter() - t0

                    # ── success ──────────────────────────────────────────────
                    if response.status_code == 200:
                        try:
                            data    = response.json()
                            text    = self._safe_content(data)
                            tokens  = self._extract_tokens(data)
                        except Exception as parse_exc:
                            log.warning(
                                "JSON parse error on success response",
                                extra={"request_id": request_id, "error": str(parse_exc)},
                            )
                            text   = response.text[:2000]
                            tokens = estimate_tokens(text)

                        await self.registry.on_success(key, latency, tokens)
                        self.store.sync_from_registry(self.registry)
                        await self.store.save()

                        log.info(
                            "OpenRouter success",
                            extra={
                                "request_id": request_id,
                                "model":      model,
                                "latency_s":  round(latency, 3),
                                "tokens":     tokens,
                                "key_suffix": f"…{key[-6:]}",
                                "attempt":    attempt + 1,
                            },
                        )

                        if use_cache and text:
                            self.cache.set(messages, model, text)
                        return text

                    # ── rate-limited / forbidden → cool key, try next ─────────
                    if response.status_code in _COOLING_STATUSES:
                        log.warning(
                            "Key cooling",
                            extra={
                                "request_id": request_id,
                                "status":     response.status_code,
                                "key_suffix": f"…{key[-6:]}",
                            },
                        )
                        await self.registry.on_error(key, force_cooldown=True)
                        break    # next key

                    # ── transient server error → retry same key ───────────────
                    if response.status_code in _TRANSIENT_STATUSES:
                        log.warning(
                            "Transient server error, retrying",
                            extra={
                                "request_id": request_id,
                                "status":     response.status_code,
                                "attempt":    attempt + 1,
                                "key_suffix": f"…{key[-6:]}",
                            },
                        )
                        await self.registry.on_error(key)
                        await backoff_sleep(attempt)
                        continue

                    # ── unexpected status ─────────────────────────────────────
                    log.error(
                        "Unexpected HTTP status",
                        extra={
                            "request_id": request_id,
                            "status":     response.status_code,
                            "body":       response.text[:500],
                            "key_suffix": f"…{key[-6:]}",
                        },
                    )
                    await self.registry.on_error(key)
                    await backoff_sleep(attempt)

                except httpx.TimeoutException as exc:
                    latency = time.perf_counter() - t0
                    log.warning(
                        "Request timeout",
                        extra={
                            "request_id": request_id,
                            "latency_s":  round(latency, 3),
                            "attempt":    attempt + 1,
                            "key_suffix": f"…{key[-6:]}",
                            "error":      str(exc),
                        },
                    )
                    await self.registry.on_error(key)
                    await backoff_sleep(attempt)

                except httpx.RequestError as exc:
                    log.error(
                        "HTTP request error",
                        extra={
                            "request_id": request_id,
                            "error":      str(exc),
                            "key_suffix": f"…{key[-6:]}",
                        },
                    )
                    await self.registry.on_error(key)
                    await backoff_sleep(attempt)

                except Exception as exc:
                    log.error(
                        "Unexpected error in call()",
                        extra={
                            "request_id": request_id,
                            "error":      str(exc),
                            "key_suffix": f"…{key[-6:]}",
                        },
                        exc_info=True,
                    )
                    await self.registry.on_error(key)
                    await backoff_sleep(attempt)

        log.error(
            "All OpenRouter keys exhausted",
            extra={"request_id": request_id, "model": model, "keys_tried": len(keys)},
        )
        return "Error: All OpenRouter API keys exhausted. Please try again later."

    # ── REAL STREAMING call ─────────────────────────────────────────────────

    async def stream(
        self,
        messages: List[Dict],
        model: str,
        extra_params: Optional[Dict] = None,
        request_id: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """
        True server-sent-event streaming from OpenRouter.

        The raw SSE bytes are yielded directly to the FastAPI StreamingResponse,
        so the client receives genuine token-by-token chunks as OpenRouter
        produces them — not a fake word-delay simulation.

        On failure the generator yields a synthetic error chunk then [DONE].
        """
        request_id = request_id or new_request_id()

        payload: Dict[str, Any] = {
            "model":    model,
            "messages": messages,
            "stream":   True,
        }
        if extra_params:
            payload.update(extra_params)

        client = get_http_client()
        keys   = self.registry.ranked_keys()

        for key in keys:
            for attempt in range(MAX_RETRIES):
                t0 = time.perf_counter()
                try:
                    async with client.stream(
                        "POST",
                        CHAT_ENDPOINT,
                        headers=self._build_headers(key),
                        json=payload,
                    ) as resp:

                        if resp.status_code in _COOLING_STATUSES:
                            await self.registry.on_error(key, force_cooldown=True)
                            break   # next key

                        if resp.status_code in _TRANSIENT_STATUSES:
                            await self.registry.on_error(key)
                            await backoff_sleep(attempt)
                            continue

                        if resp.status_code != 200:
                            await self.registry.on_error(key)
                            await backoff_sleep(attempt)
                            continue

                        # ── stream body ──────────────────────────────────────
                        accumulated_tokens = 0
                        async for raw_line in resp.aiter_lines():
                            if not raw_line:
                                continue
                            if raw_line.startswith("data:"):
                                data_str = raw_line[5:].strip()
                                if data_str == "[DONE]":
                                    yield b"data: [DONE]\n\n"
                                    break
                                # Count tokens opportunistically
                                try:
                                    chunk_data = json.loads(data_str)
                                    delta = (
                                        chunk_data
                                        .get("choices", [{}])[0]
                                        .get("delta", {})
                                        .get("content", "") or ""
                                    )
                                    accumulated_tokens += estimate_tokens(delta)
                                except Exception:
                                    pass
                                yield (raw_line + "\n\n").encode()
                            else:
                                yield (raw_line + "\n\n").encode()

                        latency = time.perf_counter() - t0
                        await self.registry.on_success(key, latency, accumulated_tokens)
                        self.store.sync_from_registry(self.registry)
                        await self.store.save()

                        log.info(
                            "OpenRouter stream completed",
                            extra={
                                "request_id":   request_id,
                                "model":        model,
                                "latency_s":    round(latency, 3),
                                "est_tokens":   accumulated_tokens,
                                "key_suffix":   f"…{key[-6:]}",
                            },
                        )
                        return  # success – stop iterating keys

                except httpx.TimeoutException as exc:
                    latency = time.perf_counter() - t0
                    log.warning(
                        "Stream timeout",
                        extra={
                            "request_id": request_id,
                            "latency_s":  round(latency, 3),
                            "attempt":    attempt + 1,
                            "key_suffix": f"…{key[-6:]}",
                            "error":      str(exc),
                        },
                    )
                    await self.registry.on_error(key)
                    await backoff_sleep(attempt)

                except Exception as exc:
                    log.error(
                        "Stream error",
                        extra={
                            "request_id": request_id,
                            "error":      str(exc),
                            "key_suffix": f"…{key[-6:]}",
                        },
                        exc_info=True,
                    )
                    await self.registry.on_error(key)
                    await backoff_sleep(attempt)

        # ── all keys exhausted: emit a synthetic error chunk ─────────────────
        log.error(
            "All keys exhausted for streaming",
            extra={"request_id": request_id, "model": model},
        )
        error_chunk = {
            "id":      new_completion_id(),
            "object":  "chat.completion.chunk",
            "created": int(time.time()),
            "model":   model,
            "choices": [{
                "index":         0,
                "delta":         {"content": "Error: All OpenRouter keys exhausted."},
                "finish_reason": "stop",
            }],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    # ── models endpoint ──────────────────────────────────────────────────────

    async def list_models(self) -> List[Dict[str, Any]]:
        """Fetch and normalise the model list from OpenRouter."""
        keys = self.registry.ranked_keys()
        if not keys:
            return []
        client = get_http_client()
        for key in keys[:3]:       # try top-3 healthiest keys
            try:
                r = await client.get(
                    MODELS_ENDPOINT,
                    headers=self._build_headers(key),
                )
                if r.status_code == 200:
                    data = r.json()
                    return [
                        {
                            "id":       m["id"],
                            "object":   "model",
                            "owned_by": "openrouter",
                            "context_length": m.get("context_length"),
                            "pricing":        m.get("pricing"),
                        }
                        for m in data.get("data", [])
                    ]
            except Exception as exc:
                log.warning(
                    "Model list fetch failed",
                    extra={"key_suffix": f"…{key[-6:]}", "error": str(exc)},
                )
        return []


# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL SINGLETON  (imported by main.py)
# ─────────────────────────────────────────────────────────────────────────────

provider = OpenRouterProvider()
