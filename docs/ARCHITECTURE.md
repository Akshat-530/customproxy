# Architecture

```
                    ┌──────────────────────────────────────────┐
   clients          │            CustomProxy container          │
   (Kilo, OpenCode, │            (unprivileged, read-only)       │
    SDKs, curl)     │                                           │
        │           │  :3001  FastAPI (src/server.py)           │
        │  one key  │    ├─ /v1/*            → provider upstream │
        └──────────▶│    ├─ /v1/models       ← live catalog       │
                    │    ├─ /logs            ← request log        │
                    │    └─ /health          ← liveness           │
                    │                                           │
                    │  :4016-4030  rotation relays               │
                    │    (key_rotate_proxy.py, one per MCP)      │
                    └───────────────┬───────────────────────────┘
                                    │ rotated keys
        ┌────────────┬──────────────┼──────────────┬────────────┐
        ▼            ▼              ▼              ▼            ▼
   provider A   provider B     provider C     provider D   OpenRouter
   (multi-key)  (multi-key)    (multi-key)    (manual)     (free-only)
```

## Components

| File | Role |
|---|---|
| `src/server.py` | The proxy: routing, key rotation, catalog, filters, logging, health |
| `key_rotate_proxy.py` | Standalone per-service key-rotation relays (MCP tool keys) |
| `src/config.json` | Providers, filters, thinking policy, runtime toggles (**no secrets**) |
| `.env` | All secrets: `PROXY_AUTH_TOKEN` + one pool per provider (**gitignored**) |
| `state/kilo_models.json` | Generated catalog, rewritten on each refresh |
| `logs/requests.jsonl` | Generated per-request log |
| `compose.yml` | Single-service deployment, hardened, loopback-only |
| `skills/*/SKILL.md` | Agent skills that add providers/keys/models, connect harnesses, remove |

## Request path

1. Client sends an OpenAI request with `model = "<provider>/<upstream-id>"`.
2. `_parse_model` resolves the provider; `Provider.allows()` applies filters
   (`models` / `include_models` / `exclude_models` / `free`), and a filtered
   model is rejected with `403 model_filtered` **before** any upstream call.
3. `KeyPool.available()` returns the key queue (round-robin), skipping keys in
   cooldown.
4. The request is forwarded with the selected key; reasoning defaults are
   injected only when the model is reasoning-capable and the client omitted
   them.
5. On 401/402/403/429/5xx the key is cooled (escalating, `Retry-After` aware)
   and the next key is tried.
6. Success is streamed through (SSE preserved; reasoning normalised).
7. A structured log entry is written and the response carries `x-request-id`.

## Catalog refresh

A background loop (`CATALOG_REFRESH_SECONDS`, default 600s) fetches each
provider's `/models`, normalises metadata, applies filters, merges
`manual_models`, and atomically writes `state/kilo_models.json`. The same data
is served live from `/v1/models` and `/.well-known/opencode`. A provider whose
first map attempt has not completed defers the write briefly; a permanently
failing provider is logged and excluded rather than blocking the catalog.

## Error contract

Everything is returned in OpenAI's envelope with the real reason:

| Upstream reality | Response |
|---|---|
| Billing / no credit / quota | `402 billing_required` + exact upstream message |
| Bad/revoked key | `401 auth_error` + exact message |
| Rate limited | `429 rate_limited` + `Retry-After` |
| All keys cooling | `429 all_keys_cooling` + seconds |
| Context too long | `400 context_length` + exact message |
| Filtered model | `403 model_filtered` |
| Upstream 5xx after retries | `502 upstream_error` + last message |
