# GemmaQA — Release Checklist

Audit snapshot for hackathon / public submission readiness.  
Date: **2026-07-24**.

GemmaQA autonomously explores authorized web applications and produces structured QA evidence within a controlled test budget. It does not fully test every application.

---

## Setup verified

- [x] Prerequisites documented (Python 3.11+, Node 18+, Playwright Chromium)
- [x] Root / backend / frontend / demo `.env.example` templates (no real secrets)
- [x] `.gitignore` covers `.env`, `.env.*`, `evidence/**`, `node_modules/`, `.venv/`, `*.db`
- [x] Clean startup: `.\start-dev.ps1` / `./start-dev.sh`
- [x] Ports documented: demo **5500**, API **8000**, UI **5173**
- [x] Legal / authorization notice in README and UI

## Backend verified

- [x] FastAPI starts (`GET /health` → ok)
- [x] Pydantic schemas load; app imports cleanly
- [x] Async Playwright lifecycle closes browser in `finally`
- [x] DB sessions are short-lived (`async with` / FastAPI `get_db`)
- [x] Run cancellation via `POST /api/runs/{id}/cancel`
- [x] Safety validation before action-loop execution
- [x] Secrets redacted in events / reports / smoke scan
- [x] Password-type controls blocked outside dedicated login
- [x] Unit suite: **130 passed**

## Frontend verified

- [x] `npm run lint` (tsc `--noEmit`)
- [x] `npm test` — **7 passed**
- [x] `npm run build` — production build OK
- [x] Routes: home, presentation, new run, live run, report, history, settings
- [x] WebSocket client reconnects with backoff
- [x] Empty / error / loading states present
- [x] Mermaid diagrams render in live run / report
- [x] Passwords cleared from React state; not written to `localStorage`
- [x] Fixture UI gated by `VITE_DEMO_MODE` + **Demo** badge

## Demo app verified

- [x] `npm run lint` + `npm run build`
- [x] ServiceFlow on :5500 with `/api/reset`
- [x] Seeded defects documented and detectable (dashboard count, validation gaps)
- [x] Demo credentials are placeholders (`changeme-*`)

## AI provider verified

- [x] Abstraction: `mock` | `openai_compatible` | `transformers`
- [x] `GET /api/ai/health` (no API key leakage)
- [x] Default mock for CI / local without weights
- [x] Parse → validate → retry → safe fallback
- [x] Optional presentation mock fallback only when `PRESENTATION_ALLOW_MOCK_FALLBACK=true`

## Safety verified

- [x] Domain / local-target gates
- [x] Prohibited / sensitive patterns blocked
- [x] Action / page / runtime / screenshot budgets
- [x] Website content treated as untrusted in prompts
- [x] Controlled writes + test-data prefixing
- [x] Known gap (documented): API has **no auth** — bind to localhost for demos

## Test suite verified

- [x] Backend: `python -m pytest tests -q` → **130 passed**
- [x] Frontend: lint + test + build
- [x] Historical ServiceFlow demo-app / smoke verification (that app is no longer in the repo)

## Presentation verified

- [x] Historical presentation-mode audit (those endpoints/env vars are no longer in the product)
- [x] Docs: README and ARCHITECTURE (realistic claims)

## Reports verified

- [x] JSON (`/report`, `/report.json`)
- [x] Markdown (`/report.md`)
- [x] HTML (`/report.html`)
- [x] CSV exports (`bugs.csv`, `tests.csv`)
- [x] Mermaid (`/navigation.mmd`)
- [x] Confirmed vs suspected classification respected
- [x] Coverage language does not claim complete product coverage
- [x] Smoke confirmed no credential leakage in report artifacts

## GitHub readiness verified

- [x] README polished (providers, presentation, security notes)
- [x] Pitch / hackathon docs accurate
- [x] Integration checklist de-certified (no false “all PASS forever” claims)
- [x] Temp evidence logs / run UUID dirs cleaned for packaging
- [x] No real API keys in tree; demo passwords are placeholders
- [x] Suitable for **public hackathon submission** with localhost-only API exposure

---

## Commands used in this audit

```powershell
# Backend
cd gemmaqa\backend
.\.venv\Scripts\python.exe -m pytest ..\tests -q

# Frontend
cd gemmaqa\frontend
npm run lint
npm test
npm run build

```

## Remaining known limitations

1. API control plane is unauthenticated — use `127.0.0.1`, not public bind, for demos.
2. Cancel is cooperative (waits for the current Playwright action to finish).
3. Bootstrap login navigation is outside the per-action validator (dedicated login path).
4. Default/mock provider does not load a live Gemma model until configured.
5. Exploratory budgets intentionally limit coverage; reports must not be read as exhaustive.
6. Workspace may not be initialized as a git remote yet — initialize/push before GitHub submission.
