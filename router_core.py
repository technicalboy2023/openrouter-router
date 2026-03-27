"""
router_core.py
==============
Shared infrastructure for the multi-provider LLM gateway.

Covers:
  • Structured JSON logging
  • Per-key health tracking (latency, success-rate, health-score)
  • KeyRegistry  – weighted key selection + async telemetry update
  • ResponseCache – TTL-based in-memory caching
  • UsageStore    – atomic-write persistent JSON usage storage
  • Shared async HTTP client (httpx, HTTP/2, connection-pooled)
  • Exponential back-off helper
  • Token estimator, request-ID generator
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# STRUCTURED LOGGER
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_LOG_KEYS = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "id", "levelname", "levelno", "lineno", "message", "module", "msecs",
    "msg", "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "taskName", "thread", "threadName",
})


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _SKIP_LOG_KEYS:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def build_logger(name: str = "llm_gateway") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler("gateway.log")
    fh.setFormatter(_JSONFormatter())
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    ch.setLevel(logging.INFO)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = build_logger()


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

MAX_RETRIES: int         = 4
INITIAL_BACKOFF: float   = 0.5    # seconds
BACKOFF_FACTOR: float    = 2.0
COOLDOWN_WINDOW: int     = 60     # seconds a key is frozen after repeated errors
ERROR_THRESHOLD: int     = 3      # consecutive errors before auto-cooldown
LATENCY_WINDOW: int      = 20     # last N requests used for rolling avg latency
CACHE_TTL: int           = 30     # seconds – identical prompt cache TTL
USAGE_FILE: str          = "usage.json"
SAVE_INTERVAL: int       = 30     # seconds between disk flushes
TIMEOUT_CONNECT: float   = 10.0
TIMEOUT_READ: float      = 120.0


# ─────────────────────────────────────────────────────────────────────────────
# PER-KEY HEALTH RECORD
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class KeyHealth:
    """Runtime telemetry for one API key. Used for smart routing decisions."""

    key: str
    total_requests: int   = 0
    success_count: int    = 0
    error_count: int      = 0
    consecutive_errors: int = 0
    total_tokens: int     = 0
    cooldown_until: float = 0.0
    latencies: deque      = field(default_factory=lambda: deque(maxlen=LATENCY_WINDOW))

    # ── derived properties ──────────────────────────────────────────────────

    @property
    def avg_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 9_999.0

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_requests if self.total_requests else 1.0

    @property
    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    @property
    def health_score(self) -> float:
        """
        Composite ∈ [0, 1].  Higher = healthier = preferred.
          60 % weight → success_rate
          40 % weight → inverse-normalised avg latency (capped at 30 s)
        """
        latency_score = max(0.0, 1.0 - self.avg_latency / 30.0)
        return round(0.6 * self.success_rate + 0.4 * latency_score, 6)

    # ── mutation helpers ────────────────────────────────────────────────────

    def record_success(self, latency: float, tokens: int = 0) -> None:
        self.total_requests    += 1
        self.success_count     += 1
        self.consecutive_errors = 0
        self.total_tokens      += tokens
        self.latencies.append(latency)

    def record_error(self, force_cooldown: bool = False) -> None:
        self.total_requests    += 1
        self.error_count       += 1
        self.consecutive_errors += 1
        if force_cooldown or self.consecutive_errors >= ERROR_THRESHOLD:
            self.cooldown_until = time.time() + COOLDOWN_WINDOW
            log.warning(
                "Key placed on cooldown",
                extra={"key_suffix": f"…{self.key[-6:]}", "until": self.cooldown_until},
            )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key_suffix":       f"…{self.key[-6:]}",
            "total_requests":   self.total_requests,
            "success_count":    self.success_count,
            "error_count":      self.error_count,
            "total_tokens":     self.total_tokens,
            "avg_latency_s":    round(self.avg_latency, 3),
            "success_rate":     round(self.success_rate, 4),
            "health_score":     self.health_score,
            "available":        self.is_available,
            "cooldown_until":   self.cooldown_until,
        }


# ─────────────────────────────────────────────────────────────────────────────
# KEY REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class KeyRegistry:
    """
    Manages a pool of API keys for one provider.

    Key selection is health-score weighted: best key first, cooled-down keys
    excluded (unless the entire pool is in cooldown, in which case we return
    all so the caller can still attempt).
    """

    def __init__(self, keys: List[str]) -> None:
        self._registry: Dict[str, KeyHealth] = {
            k: KeyHealth(key=k) for k in keys if k
        }
        self._lock = asyncio.Lock()

    # ── persistence ─────────────────────────────────────────────────────────

    def load_persisted(self, data: Dict[str, Any]) -> None:
        """Restore counters from a saved JSON blob (best-effort)."""
        for key, kh in self._registry.items():
            if key in data:
                d = data[key]
                kh.total_requests = d.get("total_requests", 0)
                kh.success_count  = d.get("success_count",  0)
                kh.error_count    = d.get("error_count",    0)
                kh.total_tokens   = d.get("total_tokens",   0)

    def serialise(self) -> Dict[str, Any]:
        return {
            k: {
                "total_requests": kh.total_requests,
                "success_count":  kh.success_count,
                "error_count":    kh.error_count,
                "total_tokens":   kh.total_tokens,
            }
            for k, kh in self._registry.items()
        }

    # ── selection ───────────────────────────────────────────────────────────

    def ranked_keys(self) -> List[str]:
        """Available keys ordered by health_score descending."""
        available = [kh for kh in self._registry.values() if kh.is_available]
        if not available:
            available = list(self._registry.values())          # last-resort
        available.sort(key=lambda kh: kh.health_score, reverse=True)
        return [kh.key for kh in available]

    # ── async telemetry updates ──────────────────────────────────────────────

    async def on_success(self, key: str, latency: float, tokens: int = 0) -> None:
        async with self._lock:
            if key in self._registry:
                self._registry[key].record_success(latency, tokens)

    async def on_error(self, key: str, force_cooldown: bool = False) -> None:
        async with self._lock:
            if key in self._registry:
                self._registry[key].record_error(force_cooldown)

    # ── introspection ────────────────────────────────────────────────────────

    def status(self) -> List[Dict[str, Any]]:
        return [kh.as_dict() for kh in self._registry.values()]

    def __len__(self) -> int:
        return len(self._registry)

    def available_count(self) -> int:
        return sum(1 for kh in self._registry.values() if kh.is_available)

    def total_tokens(self) -> int:
        return sum(kh.total_tokens for kh in self._registry.values())


# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE CACHE
# ─────────────────────────────────────────────────────────────────────────────

class ResponseCache:
    """
    In-memory TTL cache keyed on SHA-256(model + messages).
    Only used for non-streaming, deterministic requests.
    Safe for asyncio (single event loop – no lock needed for dict ops in CPython).
    """

    def __init__(self, ttl: int = CACHE_TTL, max_size: int = 512) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._ttl = ttl
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(messages: List[Dict], model: str) -> str:
        raw = json.dumps({"model": model, "messages": messages}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, messages: List[Dict], model: str) -> Optional[Any]:
        k = self._key(messages, model)
        entry = self._store.get(k)
        if entry is None:
            self._misses += 1
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            del self._store[k]
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, messages: List[Dict], model: str, value: Any) -> None:
        if len(self._store) >= self._max_size:
            oldest_key = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest_key]
        self._store[self._key(messages, model)] = (time.time(), value)

    def purge_expired(self) -> int:
        now = time.time()
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]
        return len(expired)

    def stats(self) -> Dict[str, Any]:
        return {
            "size":        len(self._store),
            "ttl_seconds": self._ttl,
            "hits":        self._hits,
            "misses":      self._misses,
            "hit_rate":    round(self._hits / max(1, self._hits + self._misses), 4),
        }


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENT USAGE STORE
# ─────────────────────────────────────────────────────────────────────────────

class UsageStore:
    """
    Async-safe, atomically-written persistent JSON usage storage.
    Writes at most every SAVE_INTERVAL seconds to keep I/O negligible.
    """

    def __init__(self, path: str = USAGE_FILE) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._last_save: float = 0.0
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path) as fh:
                    self._data = json.load(fh)
            except Exception as exc:
                log.warning("Usage load failed", extra={"error": str(exc)})
                self._data = {}

    async def save(self, force: bool = False) -> None:
        if not force and time.time() - self._last_save < SAVE_INTERVAL:
            return
        async with self._lock:
            try:
                tmp = self._path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(self._data, fh, indent=2)
                os.replace(tmp, self._path)          # atomic rename on POSIX
                self._last_save = time.time()
            except Exception as exc:
                log.error("Usage save failed", extra={"error": str(exc)})

    def sync_from_registry(self, registry: KeyRegistry) -> None:
        self._data.update(registry.serialise())

    def raw(self) -> Dict[str, Any]:
        return dict(self._data)

    def get_registry_seed(self, registry: KeyRegistry) -> Dict[str, Any]:
        return {k: v for k, v in self._data.items() if k in registry._registry}


# ─────────────────────────────────────────────────────────────────────────────
# SHARED ASYNC HTTP CLIENT
# ─────────────────────────────────────────────────────────────────────────────

_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=TIMEOUT_CONNECT,
                read=TIMEOUT_READ,
                write=30.0,
                pool=5.0,
            ),
            limits=httpx.Limits(
                max_connections=300,
                max_keepalive_connections=80,
                keepalive_expiry=30.0,
            ),
            http2=True,
            follow_redirects=True,
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def backoff_sleep(attempt: int) -> None:
    """Exponential back-off capped at 16 s."""
    delay = min(INITIAL_BACKOFF * (BACKOFF_FACTOR ** attempt), 16.0)
    await asyncio.sleep(delay)


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]


def new_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex[:12]


def estimate_tokens(text: str) -> int:
    """Rough approximation: ~4 chars per token (no tiktoken dependency)."""
    return max(1, len(text) // 4)
