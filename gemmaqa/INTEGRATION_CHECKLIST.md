# GemmaQA Integration Checklist

Working notes for local integration. This is **not** a production certification.

Ports:

| Service | URL |
|---|---|
| Demo app | http://127.0.0.1:5500 |
| Backend API | http://127.0.0.1:8000 |
| Frontend UI | http://127.0.0.1:5173 |

## Expected local checks

| # | Check | How to verify |
|---|---|---|
| 1 | Backend starts | `uvicorn` / `.\start-dev.ps1` + `GET /health` |
| 2 | Frontend starts | Vite on :5173 |
| 3 | Demo application starts | ServiceFlow on :5500 |
| 4 | Database initializes | Lifespan `init_db` |
| 5 | API health | `GET /health` |
| 6 | New QA run | UI or `POST /api/runs` |
| 7 | Browser launches | Playwright Chromium |
| 8 | Login vs demo | Smoke authenticated run |
| 9 | Page observation | Pages in run + report |
| 10 | Gemma mock actions | Default `GEMMA_PROVIDER=mock` |
| 11 | Agent explores | Within action/page budgets |
| 12 | Safety blocks prohibited | Unit tests + blocked actions |
| 13 | Screenshots saved | `evidence/<run_id>/` |
| 14 | Console/network capture | Report fields |
| 15 | WebSocket updates | Live run timeline |
| 16 | Application map | `navigation.mmd` |
| 17 | Test scenarios | Report / tests tab |
| 18 | Seeded bug detectable | Dashboard count inconsistency |
| 19 | Bug report | Confirmed vs suspected classification |
| 20 | Final report JSON/MD/HTML | Export endpoints |
| 21 | Frontend displays results | Live run + final report |
| 22 | No credentials in reports | Smoke secret scan |
| 23 | Browser closes | Controller `finally` |
| 24 | Automated tests | `pytest` + frontend `npm test` / `npm run build` |


Re-run verification with:

```powershell
cd gemmaqa
.\start-dev.ps1
backend\.venv\Scripts\python.exe -m pytest tests -q
cd frontend; npm run lint; npm test; npm run build
```

See [RELEASE_CHECKLIST.md](./RELEASE_CHECKLIST.md) for the latest release audit snapshot.
