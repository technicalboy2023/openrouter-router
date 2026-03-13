OpenRouter AI Router

OpenAI-compatible router for OpenRouter models.

Supports hundreds of AI models such as:

- Llama
- Mistral
- Claude
- Mixtral
- Gemini via OpenRouter

---

Features

- Multiple OpenRouter API keys
- Automatic key rotation
- Usage tracking
- Streaming support
- OpenAI compatible API
- n8n AI Agent integration
- systemd service support

---

Installation

cd /home/aman
mkdir -p routers
cd routers

curl -O https://raw.githubusercontent.com/technicalboy2023/openrouter-router/main/install-router.sh
chmod +x install-router.sh

bash install-router.sh openrouter-router 8080

---

Configure API Keys

nano /home/aman/routers/openrouter-router/.env

Example:

OPENROUTER_KEY_1=sk-or-xxxx
OPENROUTER_KEY_2=sk-or-xxxx
OPENROUTER_KEY_3=

Restart router:

sudo systemctl restart openrouter-router

---

API Usage

Chat

POST /v1/chat/completions

Example:

curl http://localhost:8080/v1/chat/completions \
-H "Content-Type: application/json" \
-d '{
"model":"openrouter/auto",
"messages":[{"role":"user","content":"hello"}]
}'

---

Models

GET /v1/models

---

Health

GET /health

---

Service Commands

Restart router:

sudo systemctl restart openrouter-router

Stop router:

sudo systemctl stop openrouter-router

Check status:

systemctl status openrouter-router

---

License

MIT
