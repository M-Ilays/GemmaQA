# Playwright MCP browser adapter

GemmaQA execution chain (always):

```text
Gemma → structured decision → safety validator → custom QA executor → BrowserAdapter → browser
```

Gemma never calls Playwright MCP tools directly.

## Modes

| `BROWSER_ADAPTER` | Meaning |
| --- | --- |
| `direct_playwright` (default) | In-process Playwright Chromium — full exploratory runs |
| `playwright_mcp` | Official Playwright MCP as a tool gateway |

Both modes share the same `ActionExecutor` → `BrowserAdapter` path. Direct Playwright remains fully supported; MCP does **not** silently fall back to Direct.

## Direct Playwright (default)

```env
GEMMA_PROVIDER=mock
BROWSER_ADAPTER=direct_playwright
PLAYWRIGHT_HEADLESS=true
```

Install browsers once:

```bash
npx playwright install chromium
# or from the backend venv:
python -m playwright install chromium
```

## Playwright MCP

### Option A — stdio (npx)

```env
GEMMA_PROVIDER=mock
BROWSER_ADAPTER=playwright_mcp
PLAYWRIGHT_MCP_COMMAND=npx
PLAYWRIGHT_MCP_ARGS=-y,@playwright/mcp@latest
```

### Option B — HTTP server

```bash
npx @playwright/mcp@latest --port 8931
```

```env
GEMMA_PROVIDER=mock
BROWSER_ADAPTER=playwright_mcp
PLAYWRIGHT_MCP_URL=http://127.0.0.1:8931/mcp
```

## Mock-provider MCP verification

With the API running and a Playwright MCP server available:

1. Set `GEMMA_PROVIDER=mock` and `BROWSER_ADAPTER=playwright_mcp`.
2. Start a small same-origin fixture (e.g. demo app on port 5500).
3. Start an exploratory run against that fixture.
4. Confirm the run can: start session → navigate → observe → click → fill → capture evidence → record a navigation edge → update canonical memory → generate a report → close session.
5. Check `GET /api/health/browser` for adapter status and capabilities.
6. In the report **Run Environment** section, confirm Browser adapter is **Playwright MCP** and note unsupported evidence features (console/network often unsupported).

## Local Gemma + MCP

After mock verification:

```env
GEMMA_PROVIDER=openai_compatible
GEMMA_API_BASE_URL=http://127.0.0.1:11434/v1
GEMMA_MODEL_ID=gemma3:4b
BROWSER_ADAPTER=playwright_mcp
```

Ensure Ollama is serving `gemma3:4b` and Playwright MCP is reachable, then run as usual.

## Optional live CI/integration test

```powershell
$env:GEMMAQA_LIVE_MCP="1"
pytest tests/test_adapter_execution.py::test_live_mcp_optional -q
```

Ordinary CI uses `FakeMcpTransport` and does **not** require a live MCP server.

## Health

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health/browser
Invoke-RestMethod http://127.0.0.1:8000/api/config/runtime
```

## Capabilities (typical)

| Capability | Direct Playwright | Playwright MCP |
| --- | --- | --- |
| navigate / click / fill / select | yes | yes |
| screenshots | yes | yes (tool-dependent) |
| console events | yes | often unsupported |
| network events | yes | often unsupported |
| tabs | yes | yes (tool-dependent) |
| Credential auto-login helpers | yes | navigate-only (form login via structured actions) |

Cursor IDE MCP (`user-playwright`) is separate — it helps the coding agent in chat and is **not** used by GemmaQA runs.
