# LLM Gateway — Production-Grade OpenRouter Router

OpenAI-compatible, async, multi-key LLM gateway built on OpenRouter.  
Drop-in replacement for any OpenAI-API client with intelligent routing, real streaming, and full observability.

---

## Features

- ✅ Multiple OpenRouter API keys (up to 20)
- ✅ **Smart weighted key selection** — health-score based (latency + success rate)
- ✅ **Auto-cooldown** — bad keys frozen automatically, self-healing
- ✅ **True SSE streaming** — real token-by-token from OpenRouter (no fake delays)
- ✅ **Response caching** — identical prompts served instantly (TTL-based)
- ✅ **Exponential back-off** — intelligent retry on failures
- ✅ **Structured JSON logging** — every request logged with latency, tokens, key
- ✅ OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- ✅ `/metrics`, `/router/status` — full observability
- ✅ Admin endpoints — cache flush, cooldown reset
- ✅ systemd service support
- ✅ n8n / LangChain / OpenWebUI compatible

---

## Project Structure

```
openrouter-router/
├── main.py                  # FastAPI app — all endpoints
├── router_core.py           # Shared infra: health tracking, cache, logging
├── provider_openrouter.py   # OpenRouter provider — async calls + streaming
├── requirements.txt         # Python dependencies
├── .env                     # API keys (NOT committed to git)
├── .env.example             # Key format reference
├── install-router.sh        # One-command server installer
└── gateway.log              # Structured JSON logs (auto-created)
```

---

## Installation

### On your server (Ubuntu/Debian):

```bash
cd /home/aman
mkdir -p routers
cd routers

curl -O https://raw.githubusercontent.com/technicalboy2023/openrouter-router/main/install-router.sh
chmod +x install-router.sh
sed -i 's/\r$//' install-router.sh

bash install-router.sh openrouter-router 8080
```

---

## Configure API Keys

```bash
nano /home/aman/routers/openrouter-router/.env
```

Format:

```env
OPENROUTER_KEY_1=sk-or-v1-xxxxxxxxxxxxxxxx
OPENROUTER_KEY_2=sk-or-v1-yyyyyyyyyyyyyyyy
OPENROUTER_KEY_3=sk-or-v1-zzzzzzzzzzzzzzzz
```

> Keys are automatically ranked by health score — no manual rotation needed.

Restart after editing:

```bash
sudo systemctl restart openrouter-router
```

---

## API Usage

### Chat Completion (non-streaming)

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openrouter/auto",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Chat Completion (streaming)

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openrouter/auto",
    "stream": true,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### With generation parameters

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta-llama/llama-3.1-8b-instruct",
    "temperature": 0.7,
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "Write a haiku."}]
  }'
```

---

## All Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/chat/completions` | Chat — streaming & non-streaming |
| `GET` | `/v1/models` | List all available models |
| `GET` | `/health` | Liveness check + key pool summary |
| `GET` | `/usage` | Per-key request/token counters |
| `GET` | `/metrics` | Latency, error rates, cache hit rate |
| `GET` | `/router/status` | Ranked key health scores + cooldown state |
| `POST` | `/admin/cache/clear` | Flush response cache |
| `POST` | `/admin/keys/reset-cooldowns` | Unfreeze all cooled-down keys |

---

## Observability Examples

### Health check

```bash
curl http://localhost:8080/health
```

```json
{
  "status": "ok",
  "total_keys": 5,
  "available_keys": 5,
  "total_tokens_all": 48291,
  "cache": {"size": 12, "ttl_seconds": 30, "hits": 34, "hit_rate": 0.68}
}
```

### Router status (key ranking)

```bash
curl http://localhost:8080/router/status
```

```json
{
  "provider": "openrouter",
  "total_keys": 5,
  "available_keys": 4,
  "ranked_keys": [
    {"rank": 1, "key_suffix": "…abc123", "health_score": 0.97, "avg_latency_s": 1.2, "available": true},
    {"rank": 2, "key_suffix": "…def456", "health_score": 0.84, "avg_latency_s": 2.8, "available": true},
    {"rank": 3, "key_suffix": "…ghi789", "health_score": 0.21, "available": false, "cooldown_until": 1720000060}
  ]
}
```

### Metrics

```bash
curl http://localhost:8080/metrics
```

---

## Service Commands

```bash
# Check status
systemctl status openrouter-router

# Restart
sudo systemctl restart openrouter-router

# Stop
sudo systemctl stop openrouter-router

# Live logs
journalctl -u openrouter-router -f
```

---

## n8n / OpenWebUI Integration

Set base URL to:
```
http://localhost:8080
```
No API key needed (key management is internal).

---

## License

MIT
