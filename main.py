"""
main.py
=======
Production-grade LLM Gateway  –  FastAPI application entry-point.

Endpoints (OpenAI-compatible):
  POST /v1/chat/completions  – chat, streaming & non-streaming
  GET  /v1/models            – list available models

Observability:
  GET  /health               – liveness + key pool summary
  GET  /usage                – aggregated per-key usage counters
  GET  /metrics              – detailed latency / error / token metrics
  GET  /router/status        – key-level health scores & cooldown state

Admin:
  POST /admin/cache/clear    – flush the response cache
  POST /admin/keys/reset     – reset cooldowns (not counters)

Run with:
  uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
  (use --workers 1 because shared state lives in-process)
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from router_core import (
    close_http_client,
    log,
    new_completion_id,
    new_request_id,
)
from provider_openrouter import DEFAULT_MODEL, provider


# ─────────────────────────────────────────────────────────────────────────────
# LIFESPAN  (startup / shutdown hooks)
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):                           # noqa: D401
    """Application lifespan: start background tasks, tear down on exit."""
    # ── start ────────────────────────────────────────────────────────────────
    log.info("LLM Gateway starting up")
    task = asyncio.create_task(_background_tasks())
    yield
    # ── shutdown ─────────────────────────────────────────────────────────────
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Flush usage one final time
    provider.store.sync_from_registry(provider.registry)
    await provider.store.save(force=True)
    await close_http_client()
    log.info("LLM Gateway shut down cleanly")


async def _background_tasks() -> None:
    """Periodic maintenance: purge expired cache entries, persist usage."""
    while True:
        await asyncio.sleep(30)
        try:
            evicted = provider.cache.purge_expired()
            if evicted:
                log.debug("Cache GC", extra={"evicted": evicted})
            provider.store.sync_from_registry(provider.registry)
            await provider.store.save()
        except Exception as exc:
            log.error("Background task error", extra={"error": str(exc)})


# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="LLM Gateway",
    description="Production-grade multi-provider LLM gateway (OpenRouter backend)",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── global request-ID injection ──────────────────────────────────────────────

@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request.state.request_id = new_request_id()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


# ─────────────────────────────────────────────────────────────────────────────
# /v1/chat/completions  – PRIMARY ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: Dict[str, Any]):
    """
    OpenAI-compatible chat completions endpoint.

    Accepted fields (subset of OpenAI spec):
      messages        – required
      model           – optional, defaults to "openrouter/auto"
      stream          – optional bool
      temperature     – forwarded to OpenRouter
      max_tokens      – forwarded to OpenRouter
      top_p           – forwarded to OpenRouter
      frequency_penalty, presence_penalty – forwarded
    """
    request_id: str = request.state.request_id
    messages:   List[Dict]   = body.get("messages", [])
    stream:     bool         = bool(body.get("stream", False))
    model:      str          = body.get("model") or DEFAULT_MODEL

    if not messages:
        raise HTTPException(status_code=422, detail="'messages' field is required.")

    # Forward optional generation parameters
    extra: Dict[str, Any] = {}
    for key in ("temperature", "max_tokens", "top_p", "frequency_penalty",
                "presence_penalty", "stop", "seed", "logit_bias"):
        if key in body:
            extra[key] = body[key]

    # Disable cache for non-deterministic generation params
    use_cache = "temperature" not in extra and "seed" not in extra

    log.info(
        "Incoming request",
        extra={
            "request_id": request_id,
            "model":      model,
            "stream":     stream,
            "msg_count":  len(messages),
        },
    )

    # ── STREAMING ────────────────────────────────────────────────────────────
    if stream:
        return StreamingResponse(
            provider.stream(messages, model, extra_params=extra or None, request_id=request_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control":            "no-cache",
                "X-Accel-Buffering":        "no",
                "Transfer-Encoding":        "chunked",
                "X-Request-ID":             request_id,
            },
        )

    # ── NON-STREAMING ────────────────────────────────────────────────────────
    reply = await provider.call(
        messages,
        model,
        extra_params=extra or None,
        request_id=request_id,
        use_cache=use_cache,
    )

    return JSONResponse(
        content={
            "id":      new_completion_id(),
            "object":  "chat.completion",
            "created": int(time.time()),
            "model":   model,
            "choices": [{
                "index":   0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens":     0,   # not tracked per-request
                "completion_tokens": 0,
                "total_tokens":      0,
            },
        },
        headers={"X-Request-ID": request_id},
    )


# ─────────────────────────────────────────────────────────────────────────────
# /v1/models
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models():
    """Proxy OpenRouter's model catalogue."""
    models = await provider.list_models()
    return {"object": "list", "data": models}


# ─────────────────────────────────────────────────────────────────────────────
# /health
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness probe + high-level key pool summary."""
    reg = provider.registry
    return {
        "status":           "ok",
        "total_keys":       len(reg),
        "available_keys":   reg.available_count(),
        "total_tokens_all": reg.total_tokens(),
        "cache":            provider.cache.stats(),
        "uptime_note":      "Use /metrics or /router/status for detailed telemetry.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# /usage
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/usage")
async def usage():
    """
    Aggregated per-key usage counters (anonymised: only last-6 chars shown).
    """
    provider.store.sync_from_registry(provider.registry)
    # Return the registry's live view (more accurate than the persisted file)
    return {
        f"…{k[-6:]}": {
            "total_requests": kh.total_requests,
            "success_count":  kh.success_count,
            "error_count":    kh.error_count,
            "total_tokens":   kh.total_tokens,
        }
        for k, kh in provider.registry._registry.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# /metrics  – DETAILED TELEMETRY
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/metrics")
async def metrics():
    """
    Detailed per-key and aggregate metrics.

    Returns:
      aggregate  – totals across all keys
      per_key    – per-key latency, success-rate, health-score, token count
      cache      – hit/miss rates, current cache size
    """
    reg = provider.registry
    keys_data = reg.status()

    total_req    = sum(d["total_requests"] for d in keys_data)
    total_ok     = sum(d["success_count"]  for d in keys_data)
    total_err    = sum(d["error_count"]    for d in keys_data)
    total_tokens = sum(d["total_tokens"]   for d in keys_data)
    avg_latency  = (
        sum(d["avg_latency_s"] for d in keys_data if d["avg_latency_s"] < 9_000)
        / max(1, sum(1 for d in keys_data if d["avg_latency_s"] < 9_000))
    )

    return {
        "aggregate": {
            "total_requests":   total_req,
            "total_successes":  total_ok,
            "total_errors":     total_err,
            "total_tokens":     total_tokens,
            "overall_success_rate": round(total_ok / max(1, total_req), 4),
            "avg_latency_s":    round(avg_latency, 3),
        },
        "per_key": keys_data,
        "cache":   provider.cache.stats(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# /router/status  – KEY-LEVEL HEALTH DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/router/status")
async def router_status():
    """
    Detailed key-level health dashboard.

    Shows each key's health_score, availability, cooldown state,
    and ranked position in the selection queue.
    """
    ranked   = provider.registry.ranked_keys()
    all_data = {kh.key: kh.as_dict() for kh in provider.registry._registry.values()}

    ranked_view = []
    for rank, key in enumerate(ranked, start=1):
        entry = dict(all_data[key])
        entry["rank"] = rank
        ranked_view.append(entry)

    return {
        "provider":         "openrouter",
        "total_keys":       len(provider.registry),
        "available_keys":   provider.registry.available_count(),
        "ranked_keys":      ranked_view,
        "default_model":    DEFAULT_MODEL,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/admin/cache/clear")
async def admin_cache_clear():
    """Flush the entire response cache."""
    provider.cache._store.clear()
    log.info("Response cache cleared via admin endpoint")
    return {"status": "ok", "message": "Cache cleared."}


@app.post("/admin/keys/reset-cooldowns")
async def admin_reset_cooldowns():
    """
    Reset cooldowns on all keys (useful after resolving a rate-limit issue).
    Does NOT reset usage counters or error counts.
    """
    reset_count = 0
    for kh in provider.registry._registry.values():
        if kh.cooldown_until > time.time():
            kh.cooldown_until = 0.0
            reset_count += 1
    log.info("Cooldowns reset via admin endpoint", extra={"count": reset_count})
    return {"status": "ok", "reset_count": reset_count}


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL EXCEPTION HANDLER
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    log.error(
        "Unhandled exception",
        extra={"request_id": request_id, "path": request.url.path, "error": str(exc)},
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        workers=1,           # single worker – shared in-process state
        log_level="info",
        access_log=True,
    )
