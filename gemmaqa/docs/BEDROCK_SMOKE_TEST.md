# Bedrock provider — first live AWS smoke test

Everything in the Bedrock integration is SDK-verified and mock-verified. **No
request has ever left the machine.** This is the checklist that closes that gap.

Work top to bottom and stop at the first failure — each phase's assumptions are
what the next one builds on.

---

## Phase 0 — before touching AWS

- [ ] `pip install -r backend/requirements-bedrock.txt`
- [ ] `backend/.venv/Scripts/python.exe -c "import boto3; print(boto3.__version__)"` → **≥ 1.35**
- [ ] Full suite green: `backend/.venv/Scripts/python.exe -m pytest tests -q`
- [ ] **Confirm model access is enabled** for your chosen model in your chosen
      region, in the Bedrock console. A model that exists but is not enabled fails
      as `AccessDenied`, which reads like a credentials problem and will send you
      looking in the wrong place.
- [ ] Note your expected per-call cost. The exploration loop makes one model call
      per action; a 70-action run is ~70 calls.

## Phase 1 — configuration, no calls yet

```bash
GEMMA_PROVIDER=bedrock
AWS_REGION=<your region>
BEDROCK_MODEL_ID=<model id, exactly as the console shows it>
AWS_BEARER_TOKEN_BEDROCK=<your Bedrock API key>
```

- [ ] **Restart the backend.** uvicorn has no working `--reload` here — it runs
      whatever was on disk at start. Verify the process start time is later than
      your newest edit.
- [ ] `curl -s http://127.0.0.1:8000/api/health/gemma`

Expect exactly:

| field | expected |
|---|---|
| `status` | `"ok"` |
| `provider_type` | `"bedrock"` |
| `configured` | `true` |
| `reachable` | `null` |
| `connection_status` | `"not_checked"` |
| `region` | your region |
| `model_id` | your model id |
| `auth_configured` | `true` |
| `auth_mode` | `"bearer_token"` |

- [ ] **`auth_mode` is `"bearer_token"`, not `"aws_credential_chain"`.** If it is
      the latter, the token is not reaching `Settings` — check the variable name
      and that `.env` is being loaded.
- [ ] Grep the response for your token. It must not appear.

## Phase 2 — the first real call

The cheapest possible live call, one request:

```bash
backend/.venv/Scripts/python.exe -c "
import sys, asyncio; sys.path.insert(0, 'backend')
from app.gemma import get_gemma_provider
p = get_gemma_provider(force_new=True)
print(asyncio.run(p._generate('Reply with the single word OK.', 'Say OK.')))
print(p.last_model_call_result.telemetry())
"
```

- [ ] It returns text.
- [ ] `telemetry()` shows **non-null `input_tokens` and `output_tokens`** — this is
      the first confirmation that Bedrock's usage block is shaped as expected.
- [ ] `stop_reason` is a value from the declared enum (`end_turn` most likely).
- [ ] `latency_ms` is plausible (hundreds to low thousands).
- [ ] Nothing in the log line contains the prompt text, the response text, or the
      token.

Then re-check health:

- [ ] `reachable` is now `true`, `connection_status` is `"reachable"`.

## Phase 3 — deliberate failures

Each of these has been tested against a mock. This confirms the **real** AWS error
code maps to the kind we expect. Run each, then restore the working config.

- [ ] **Invalid model** — set `BEDROCK_MODEL_ID=not-a-real-model`.
      Expect kind `invalid_model` (from `ResourceNotFoundException`) or
      `validation_error` — **note which**. If AWS returns `ValidationException`
      here rather than `ResourceNotFoundException`, the mapping is still correct
      but the guidance text is less precise, and that is worth recording.
- [ ] **Bad credentials** — set `AWS_BEARER_TOKEN_BEDROCK=obviously-invalid`.
      Expect `invalid_credentials` or `access_denied`. **Record which**; this is
      the single most likely place the real service disagrees with the mock.
- [ ] **Wrong region** — set `AWS_REGION` to a region where the model is not
      enabled. Expect `access_denied` or `invalid_model`.
- [ ] **Short timeout** — set `BEDROCK_READ_TIMEOUT=0.001`. Expect `read_timeout`
      or `connect_timeout`.
- [ ] After each failure, `connection_status` is `"unreachable"` for the transport
      and credential cases, and **unchanged** for `validation_error` — because a
      `ValidationException` proves we reached Bedrock.
- [ ] No error message contains the token.

## Phase 4 — a real exploration run

- [ ] Start a run against a target you are authorised to test, low budget
      (`max_actions` 10–15) to bound cost.
- [ ] Actions are chosen by the model, not by heuristics — check the activity log
      shows varied reasoning rather than the Mock dispatch table's fixed verbs.
- [ ] **Browser observation keeps working during model calls.** This is the
      `asyncio.to_thread` behaviour under real latency: if the UI freezes or
      screenshots stall for seconds at a time, the loop is being blocked.
- [ ] `/api/config/runtime` reports `provider: "Amazon Bedrock"`,
      `run_mode: "Cloud AI (Amazon Bedrock)"`, `capability_mode:
      "real_model_reasoning"`, and the correct `model_id`.
- [ ] Total token usage across the run is plausible against the AWS console's own
      figure. A large discrepancy means the usage mapping is wrong.
- [ ] Grep the whole run's `evidence/<run_id>/activity.jsonl` for your token. It
      must not appear.

## Phase 5 — throttling, only if cheap to provoke

- [ ] Run two or three concurrent runs, or lower your account quota, until
      `throttled` appears.
- [ ] Confirm botocore retried before surfacing it — the failure should take
      noticeably longer than a single call, consistent with
      `BEDROCK_MAX_RETRIES + 1` attempts.
- [ ] Confirm **no second retry loop**: the total attempt count should match
      botocore's budget, not multiply it.

---

## Record before closing out

- Which AWS error code appeared for each Phase-3 case, and the kind it mapped to.
- Whether the Converse response for **your** model included `usage` and `metrics`
  (some models omit `metrics`).
- Measured `latency_ms` versus `service_latency_ms` for a typical call.
- Whether any `stopReason` other than `end_turn` appeared — `max_tokens`
  especially, which means output was truncated and may need
  `GEMMA_MAX_OUTPUT_TOKENS` raised.

## If something disagrees with the mocks

The mocked tests encode assumptions about the service. Where reality differs, the
**test fixture is what is wrong**, and it should be corrected to match observed
behaviour — the same rule this project already applies to page fixtures: a fixture
must never be kinder than the real thing.

Update the affected test with a comment recording the observed AWS behaviour, then
re-run the full suite.
