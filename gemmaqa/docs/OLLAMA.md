# Ollama + Gemma local setup

GemmaQA talks to **any OpenAI-compatible** chat endpoint. Ollama is the recommended local runtime.

## Install

1. Install [Ollama](https://ollama.com/download)
2. Pull a model (example for ~16 GB RAM laptops):

```bash
ollama pull gemma3:4b
```

3. Confirm Ollama is serving:

```bash
ollama list
curl http://127.0.0.1:11434/api/tags
```

## Configure GemmaQA

Edit `backend/.env` (variable names are exact):

```env
GEMMA_PROVIDER=openai_compatible
GEMMA_API_BASE_URL=http://127.0.0.1:11434/v1
GEMMA_MODEL_ID=gemma3:4b
GEMMA_API_KEY=
GEMMA_TEMPERATURE=0.1
GEMMA_MAX_OUTPUT_TOKENS=512
GEMMA_TIMEOUT_SECONDS=60
BROWSER_ADAPTER=direct_playwright
```

Do **not** hardcode the model id in application code — change `GEMMA_MODEL_ID` only.

## Restart and verify

```powershell
# Restart the API after .env changes
cd d:\GemmaQA\gemmaqa\backend
.\.venv\Scripts\python.exe run.py
```

Health checks (no secrets returned):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health/gemma
Invoke-RestMethod http://127.0.0.1:8000/api/config/runtime
```

Expect `provider_type=openai_compatible`, `model_id=gemma3:4b`, `is_mock=false`, and ideally `reachable=true`.

## Important

- `GEMMA_PROVIDER=mock` uses heuristics only and will **not** call Ollama.
- Misconfigured `openai_compatible` **does not** silently fall back to mock — fix env or switch provider.
- Reports show **Provider: Gemma via Ollama** when the API base points at port `11434`.
