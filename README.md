OpenRouter AI Router

OpenAI-compatible router for OpenRouter models.

Allows using OpenRouter models with tools like n8n or OpenAI SDKs.

---

Features

- Multiple API keys
- Key rotation
- Cooldown handling
- Usage tracking
- Streaming support
- OpenAI compatible API

---

Installation

git clone https://github.com/technicalboy2023/openrouter-router.git
cd openrouter-router

python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn requests python-dotenv

---

Environment Variables

Create ".env" file:

OPENROUTER_KEY_1=sk-or-xxxx
OPENROUTER_KEY_2=sk-or-xxxx
OPENROUTER_KEY_3=
OPENROUTER_KEY_4=
OPENROUTER_KEY_5=

---

Run

uvicorn router:app --host 0.0.0.0 --port 8080

---

API

Chat

POST /v1/chat/completions

Example:

{
"model":"openrouter/auto",
"messages":[{"role":"user","content":"hello"}]
}

---

Models

GET /v1/models

---

Health

GET /health

---

Usage

GET /usage

---

n8n Setup

Base URL: http://VPS_IP:8080/v1

---

License

MIT
