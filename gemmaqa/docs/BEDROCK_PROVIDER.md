# Amazon Bedrock provider

GemmaQA's fourth provider, alongside `mock`, `openai_compatible` and
`transformers`. Selecting it switches every model-backed capability in the
application; no other module changes.

---

## Quick start

```bash
pip install -r backend/requirements.txt
pip install -r backend/requirements-bedrock.txt     # boto3, not in the base install
```

```bash
GEMMA_PROVIDER=bedrock
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=eu.anthropic.claude-sonnet-4-20250514-v1:0
AWS_BEARER_TOKEN_BEDROCK=<your Bedrock API key>
```

Verify without spending a model call:

```bash
curl -s http://127.0.0.1:8000/api/health/gemma
```

Expect `"configured": true`, `"connection_status": "not_checked"`. See
[Health reporting](#health-reporting) for why it is not `"reachable"` yet.

---

## Where it sits

```
Planner ──► GemmaProvider.generate_action()      ← unchanged, provider-agnostic
                     │
                     ├── prepare_generation_request()   the shared CONTEXT pipeline
                     │     aggregate → project → retrieve → render → reduce
                     │     produces GenerationRequest (system, user, images, …)
                     │
                     └── _generate(system, user, …) -> str   ← the ONE abstract method
                                    │
                     ┌──────────────┼──────────────┬─────────────────┐
                  Mock      OpenAI-compatible   Transformers      Bedrock
                                                                     │
                                              ModelCallRequest ──► Converse API
                                              ModelCallResult  ◄── response
```

`GemmaProvider` has exactly one abstract method. Bedrock implements it and
inherits every capability — action generation, page analysis, goal ranking,
workflow extraction, visual analysis — without touching Planner, Observer,
Executor, Browser Manager, Reporting, or the context pipeline.

### Two layers, deliberately separate

| | |
|---|---|
| `GenerationRequest` (`app/gemma/base.py`) | **Context.** What the call should *say*: rendered prompts, evidence registry, projection and reduction records. Produced by `prepare_generation_request()`. |
| `ModelCallRequest` / `ModelCallResult` (`app/gemma/model_call.py`) | **Transport.** How the call is *made* and what came back: model id, decoding knobs, token counts, latency, stop reason. |

These are not the same object under two names. Bedrock takes a
`GenerationRequest`'s prompts and carries them in a `ModelCallRequest`.
`app/gemma/model_call.py` imports no SDK, so it stays importable with no
provider backend installed at all.

---

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GEMMA_PROVIDER` | yes | `mock` | `bedrock`; aliases `aws`, `aws_bedrock`, `amazon_bedrock` |
| `AWS_REGION` | yes | — | Factory refuses without it |
| `BEDROCK_MODEL_ID` | yes | — | Never falls back to `GEMMA_MODEL_ID` |
| `AWS_BEARER_TOKEN_BEDROCK` | no | — | Bedrock API key. Absent ⇒ standard AWS credential chain (SigV4) |
| `BEDROCK_MAX_RETRIES` | no | `3` | botocore `max_attempts = N + 1` |
| `BEDROCK_CONNECT_TIMEOUT` | no | `10.0` | seconds |
| `BEDROCK_READ_TIMEOUT` | no | `GEMMA_TIMEOUT_SECONDS` | inherits the global timeout unless set |
| `GEMMA_TEMPERATURE` | no | `0.1` | always sent |
| `GEMMA_MAX_OUTPUT_TOKENS` | no | `512` | → `maxTokens` |
| `GEMMA_TOP_P` | no | unset | **omitted** when unset, not sent as `0.0` |
| `GEMMA_STOP_SEQUENCES` | no | unset | comma-separated |
| `GEMMA_SUPPORTS_IMAGES` | no | `false` | enables screenshot attachment |

**`BEDROCK_MODEL_ID` deliberately has no fallback to `GEMMA_MODEL_ID`.** A
Bedrock model id and an Ollama tag look nothing alike; borrowing one for the
other turns a clear "BEDROCK_MODEL_ID is not configured" into an opaque AWS
`ValidationException`.

**Unset optional parameters are omitted, not zeroed.** `topP=0.0` means
"consider only the single most likely token" — a real instruction, not a synonym
for "unconfigured".

---

## Authentication

Verified against **botocore 1.43.63** by inspecting a signed request, not
assumed:

| setup | resulting `Authorization` header |
|---|---|
| `AWS_BEARER_TOKEN_BEDROCK` in `os.environ` | `Bearer <token>` |
| no token | `AWS4-HMAC-SHA256` (SigV4 credential chain) |
| token **+** `Config(signature_version=…)` | `AWS4-HMAC-SHA256` — **bearer silently disabled** |

Supporting facts from the SDK: `bedrock-runtime`'s service model declares
`signingName: bedrock` and `auth: ['aws.auth#sigv4', 'smithy.api#httpBearerAuth']`;
`get_bearer_auth_supported_services()` returns `{'bedrock'}`; and
`botocore.utils.get_token_from_environment()` reads
`AWS_BEARER_TOKEN_<SIGNING_NAME>` from `os.environ`.

Three consequences that matter:

1. **No `Authorization` header is ever built by hand.** The SDK owns signing.
2. **The client `Config` must never set `signature_version` or
   `auth_scheme_preference`.** `_should_prefer_bearer_auth()` bails out when
   either is set in code, so an innocuous-looking kwarg reverts a Bedrock API key
   to SigV4 and the failure presents as a permissions error. A test walks the AST
   of the `Config(...)` call to prevent it.
3. **`os.environ` is bridged once.** botocore reads only the process
   environment, while pydantic-settings loads `.env` into `Settings` only — so a
   token set solely in `.env` would never reach the SDK.
   `_export_bearer_token()` closes that gap, never overwrites an existing value,
   and reads the secret through `effective_bedrock_bearer_token`, the single
   sanctioned unwrap.

### Secret handling

`AWS_BEARER_TOKEN_BEDROCK` is a pydantic `SecretStr`, so a `repr`, an f-string,
`model_dump()`, and pydantic validation errors all render `**********`.

- Exactly **two** call sites unwrap it (`config.bedrock_auth_configured`,
  `config.effective_bedrock_bearer_token`); a test asserts the count and scans
  every module under `app/` to prove nothing else touches the field.
- The token is never stored on the provider instance — only `auth_configured`, a
  bool.
- Provider-supplied error text is passed through `_redact()`, which replaces the
  token with `[REDACTED]`. Defence in depth: `Error.Message` is the one string in
  a normalized error that this code does not author.
- Health responses expose `auth_configured` (bool) and `auth_mode` (a mechanism
  name) — nothing derived from the token's value, not even a length or a mask.

---

## Request mapping

| `ModelCallRequest` | Converse |
|---|---|
| `system_prompt` | `system: [{"text": …}]` — top-level, **not** a `role="system"` message |
| `prompt` | `messages: [{"role": "user", "content": [{"text": …}]}]` |
| `model` | `modelId` |
| `temperature` | `inferenceConfig.temperature` (always) |
| `max_tokens` | `inferenceConfig.maxTokens` — omitted if unset |
| `top_p` | `inferenceConfig.topP` — omitted if unset |
| `stop_sequences` | `inferenceConfig.stopSequences` — omitted if empty |
| images | `content: [{"image": {"format", "source": {"bytes"}}}]` |

### Images

Supported, because the mapping is total rather than guessed:
`prepare_image_data_url` emits only `image/png|jpeg|webp`, and Converse's
`ImageFormat` enum is `png|jpeg|gif|webp`. The data URL carries its mime
explicitly, so `format` is read, never inferred. An unmapped mime drops the image
and logs it; the call proceeds text-only.

Reusing that shared helper also keeps the existing size caps, downscaling, and
the `should_skip_screenshot` refusal to send a `login_failed` screenshot.

---

## Response mapping

`output.message.content[].text` concatenated (non-text blocks such as
`reasoningContent` ignored) → `usage.inputTokens` / `outputTokens` →
`stopReason` → measured wall-clock latency.

```python
ModelCallResult(
    text=…, model=…, provider="bedrock",
    input_tokens=…, output_tokens=…,      # None when Bedrock omits usage
    stop_reason=…,                        # "" when omitted
    latency_ms=…,                         # OUR wall clock, network included
    raw_response={                        # safe metadata ONLY
        "stop_reason", "usage", "service_latency_ms", "http_status",
    },
)
```

Retained on the provider as `last_model_call_result`.

**`raw_response` deliberately excludes the payload.** `output.message` is model
text, and model text can quote the prompt, which can quote credentials the run
typed in.

**Unreported ≠ zero.** A token count Bedrock did not report is `None`; `0` means
it genuinely reported zero. `usage_reported` and `total_tokens` distinguish them,
so a cost report cannot silently read "unknown" as "free".

**`latency_ms` and `service_latency_ms` are separate facts.** The first is what
the caller experienced (network included); the second is Bedrock's own figure.
Reporting one as the other would hide time spent outside the model.

An empty completion raises `malformed_response` rather than returning `""` —
failing where the cause is visible beats failing later in the parser.

---

## Error handling

Every failure normalizes to one `BedrockCallError` with a `kind` from a closed
16-value vocabulary, so callers branch on a value rather than on SDK message
substrings. Each kind is paired with operator guidance, and a test asserts the
two sets are identical — a new kind cannot be added without an answer to "and
then what?".

| AWS error code | kind |
|---|---|
| `AccessDeniedException` | `access_denied` |
| `UnrecognizedClientException`, `InvalidSignatureException` | `invalid_credentials` |
| `ExpiredTokenException`, `ExpiredToken` | `expired_credentials` |
| `ValidationException` | `validation_error` |
| `ResourceNotFoundException` | `invalid_model` |
| `ThrottlingException`, `TooManyRequestsException` | `throttled` |
| `ModelTimeoutException` | `read_timeout` |
| `ServiceUnavailableException`, `InternalServerException`, `ModelNotReadyException` | `service_unavailable` |
| `ModelErrorException` | `model_error` |
| anything newer | `unknown` |

| botocore exception | kind |
|---|---|
| `ConnectTimeoutError` | `connect_timeout` |
| `ReadTimeoutError` | `read_timeout` |
| `EndpointConnectionError`, `ConnectionClosedError` | `network_error` |
| `NoCredentialsError`, `PartialCredentialsError` | `missing_credentials` |

Plus `not_installed` (Bedrock extra missing), `not_configured` (region/model
absent), and `malformed_response`.

AWS messages are capped at 200 characters: useful for diagnosis, but
prompt- and attacker-influenced strings.

### Retries

botocore's only — `retries={"max_attempts": BEDROCK_MAX_RETRIES + 1, "mode":
"standard"}`. **There is no second retry loop.** Two nested retry budgets
multiply into a stall nobody sized. Note the `+1`: standard mode counts the first
attempt, so `BEDROCK_MAX_RETRIES=4` is 5 attempts.

This diverges from `OpenAICompatibleGemmaProvider`, which wraps its own single
retry; that predates the decision and was left alone.

---

## Concurrency

boto3 is synchronous. `_generate` runs the call via `asyncio.to_thread`, because
the event loop it would otherwise block is the **same loop that drives the
browser** — an inline call would freeze page observation for the duration of
every model call. Proven behaviourally: a concurrent coroutine keeps ticking
during a blocking call, and the SDK executes on a different thread.

The Bedrock Runtime client is built once, lazily, on first use, and reused.

---

## Health reporting

`GET /api/health/gemma`:

```json
{
  "status": "ok", "provider_type": "bedrock", "configured": true,
  "reachable": null,
  "connection_status": "not_checked",
  "model_id": "eu.anthropic.claude-sonnet-4-…",
  "api_base_url": null,
  "region": "us-east-1",
  "auth_configured": true,
  "auth_mode": "bearer_token",
  "multimodal_support": false, "last_error_summary": null,
  "consecutive_failures": 0, "is_mock": false
}
```

`region`, `auth_configured`, `auth_mode` and `connection_status` are additive;
all pre-existing keys keep their names and meanings. `api_base_url` is **not**
repurposed — Bedrock has no operator-set base URL, so it stays `null`.

### `reachable` is a tri-state

| | `reachable` | `connection_status` |
|---|---|---|
| factory refused | `false` (legacy) | `misconfigured` |
| configured, never called | `null` | `not_checked` |
| a Converse call completed | `true` | `reachable` |
| transport/credential failure | `false` | `unreachable` |
| `ValidationException`, throttle, invalid model | **unchanged** | unchanged |

**Bedrock Runtime has no free liveness endpoint** — the cheapest probe is a
billable Converse call — so `health_check()` validates configuration only and
`reachable` stays `null` until a real inference call settles it. The provider
declares this via `supports_liveness_probe = False`; the endpoint then refuses to
promote a config check into evidence of reachability.

That last row matters: a `ValidationException` *proves* we reached Bedrock, so
marking it "unreachable" would assert a connectivity problem the evidence
contradicts. Only the ten kinds in `UNREACHABLE_FAILURE_KINDS` set
`reachable = False`, and no failure ever sets it `True`.

`connection_status` exists because a client treating a falsy `null` as a failure
would show a correctly configured provider as broken.

---

## Logging

Structured, via `ModelCallRequest.telemetry()` and
`ModelCallResult.telemetry()`, both **allowlists** — they name the safe fields,
so a newly added sensitive field cannot silently join the log. A test asserts the
exact key sets.

```
INFO  Bedrock converse request {'model': …, 'temperature': 0.1, 'max_tokens': 512,
      'top_p': None, 'stop_sequences': 0, 'prompt_chars': 8214,
      'system_prompt_chars': 1902, 'metadata_keys': ['provider']}
INFO  Bedrock converse ok {'provider': 'bedrock', 'model': …, 'input_tokens': 1200,
      'output_tokens': 64, 'total_tokens': 1264, 'usage_reported': True,
      'stop_reason': 'end_turn', 'latency_ms': 812, 'response_chars': 214}
ERROR Bedrock converse failed kind=throttled aws_code=ThrottlingException
      model=… latency_ms=31022
```

Prompt and response **sizes** are logged, never their text. Metadata **keys**,
never values. The token appears nowhere.

---

## Switching providers and models

```bash
GEMMA_PROVIDER=bedrock             # Amazon Bedrock
GEMMA_PROVIDER=mock                # deterministic heuristics, no model
GEMMA_PROVIDER=openai_compatible   # Ollama / LM Studio / vLLM / gateway
GEMMA_PROVIDER=transformers        # local weights
```

Change the model with `BEDROCK_MODEL_ID` alone. Nothing else is model-specific.

**No silent fallback, ever.** A misconfigured provider raises at construction —
`mock` heuristics answering for a broken Bedrock config would produce a report
that looks like a successful test run.

---

## Adding another provider

1. `app/gemma/<name>_provider.py` — subclass `GemmaProvider`, implement
   `_generate(system, user, *, images, temperature) -> str`. Import any SDK
   **lazily**, inside the method that needs it.
2. `config.py` — settings plus a branch in `normalized_gemma_provider`.
3. `app/gemma/__init__.py` — a factory branch that refuses incomplete config with
   the variable name to fix, and an `__all__` entry.
4. `runtime_info.py` — `provider_display_name`, `run_mode_label`, and
   `reported_model_id` if the model id lives in a new setting.
5. Set `supports_liveness_probe = False` if the only reachability probe costs
   money.
6. Tests, stubbed at the SDK boundary.

There is intentionally **no provider registry**: four explicit branches are
easier to read than a registration mechanism, and each provider's configuration
requirements differ enough that a uniform interface would hide them.

---

## Tests

| file | count | covers |
|---|---|---|
| `tests/test_model_call.py` | 20 | transport DTOs, telemetry allowlists, unreported-vs-zero |
| `tests/test_bedrock_config.py` | 27 | env loading, aliases, secret containment |
| `tests/test_bedrock_provider.py` | 80 | request/response mapping, all 18 failure kinds, retries, event loop, secrets |
| `tests/test_bedrock_factory.py` | 23 | registration, no-silent-fallback, runtime reporting, scope |
| `tests/test_bedrock_health.py` | 28 | connection status, state transitions, no billable probe |

**178 tests, all mocked at the SDK boundary.** No live AWS, no credentials, no
account. Sections needing botocore's exception classes `importorskip` it, so the
suite passes with the Bedrock extra uninstalled.

```bash
backend/.venv/Scripts/python.exe -m pytest tests -q
```

---

## Known limitations

- **No live AWS call has ever been made.** Everything above is SDK-verified and
  mock-verified. The auth mechanism was proven by inspecting a signed request,
  but nothing has left the machine. See the smoke-test checklist.
- **`ModelCallResult.from_text()` is unused by production code.** It is the
  approved migration seam for giving the other three providers normalized
  results; nothing needs one yet.
- **Model routing is not implemented.** `ModelCallRequest.metadata` carries a
  `task` hint and nothing reads it. Per-task model selection (Planner → Claude,
  Vision → Nova) remains future work.
- **The other three providers still return `str`.** Only Bedrock produces a real
  `ModelCallResult`.
- **`reachable` never returns to `null`.** Once a call has settled it, only
  another call changes it; there is no staleness window.
- **The health endpoint is not rendered anywhere.** The frontend calls only
  `/health`; `connection_status` is ready for a UI that does not yet exist.
