# CustomProxy

A single OpenAI-compatible endpoint in front of many model providers, with
transparent API-key rotation, a self-updating model catalog, per-model
metadata, and exact upstream error surfacing.

**One base URL. One API key. Every provider behind it.**

```
client (Kilo / OpenCode / LangChain / OpenAI SDK / curl)
   |  Authorization: Bearer <PROXY_AUTH_TOKEN>
   v
CustomProxy :3001  ──►  provider 1 (keys k1..kN, rotated)
                   ──►  provider 2 (keys k1..kN, rotated)
                   ──►  ...
```

## What it does

| Feature | Detail |
|---|---|
| OpenAI-compatible | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, streaming SSE, tools, multipart (audio/images) |
| Any base URL | Works with `/v1` or without it |
| Key rotation | Per-provider pools, rotate on 401/402/403/429/5xx, escalating cooldown, `Retry-After` honoured |
| Dynamic catalog | Discovers all models from each provider's `/models`, refreshes every `CATALOG_REFRESH_SECONDS`, writes `kilo_models.json` |
| Model filters | `models` / `include_models` / `exclude_models` (globs + `re:` regex) / `max_models` / `filter_free`, enforced on the catalog **and** direct calls |
| Provider filters | `provider_filters` + per-provider `enabled: false` |
| Env-defined providers | `CUSTOM_PROVIDERS_JSON` lets users add providers without editing config |
| Metadata | Context window, pricing, modalities, `category` (text/vision/audio/embedding/image-generation), `free` |
| Thinking control | Per-provider `reasoning_auto` / `reasoning_default` / `reasoning_levels`, clamped and only injected for reasoning-capable models |
| Exact errors | Billing → `402`, auth → `401`, rate limit → `429` + `Retry-After`, context → `400`; never a generic "keys exhausted" |
| Request log | In-memory + JSONL: outcome, latency, status, key id, attempts, token usage, exact upstream error |
| Secrets | All upstream keys in `.env` (never in config, never sent to clients), labels as key ids in logs |

## Quick start

```powershell
copy .env.example .env      # then fill in your keys
docker compose up -d --build
curl http://127.0.0.1:3001/health
```

Point any OpenAI client at:

- **base_url:** `http://127.0.0.1:3001/v1` (or `http://127.0.0.1:3001`)
- **api_key:** the value of `PROXY_AUTH_TOKEN` from `.env`
- **model:** `provider/upstream-model-id` (see `/v1/models`)

The port is published on `127.0.0.1` only — the proxy and its upstream keys
are never reachable from the internet. For remote access use an SSH tunnel or
your own TLS reverse proxy; never expose the port directly.

## Key format (.env)

```env
TOKENHARBOUR_API_KEYS=th-1=thk_xxx,th-2=thk_yyy      # label=key (label = key id in /logs)
NVIDIA_API_KEYS=nv-1=nvapi-xxx                        # label optional (auto: provider-envN)
OPENROUTER_API_KEYS_JSON=[{"label":"or-1","key":"sk-or-xxx=="}]   # keys containing '='
```

The `label=` prefix is stripped before the key is sent upstream; it exists only
so you can identify which key handled a request in `/logs`.

## Adding a provider

**In `src/config.json`**:

```json
"myprovider": {
  "enabled": true,
  "base_url": "https://api.example.com/v1",
  "keys_env": "MYPROVIDER_API_KEYS",
  "models": [],
  "include_models": ["deepseek-*"],
  "exclude_models": ["*embed*"],
  "max_models": 50,
  "reasoning_auto": true,
  "reasoning_default": "medium"
}
```

Then in `.env`: `MYPROVIDER_API_KEYS=mp-1=sk-xxx`.

**Or purely via env** (no config edit):

```env
CUSTOM_PROVIDERS_JSON=[{"name":"myprovider","base_url":"https://api.example.com/v1","keys_env":"MYPROVIDER_API_KEYS","models":[]}]
```

## Model filters

| Key | Meaning |
|---|---|
| `models` | exact ids or globs; empty = discover everything |
| `include_models` | must match one pattern to be kept |
| `exclude_models` | matching models are dropped (catalog + calls → `403 model_filtered`) |
| `filter_free` | drop models positively known to be paid |
| `max_models` | cap the catalog (free models first, then alphabetical) |

Precedence: `models` → `include_models` → `exclude_models` → `filter_free` → `max_models`.
`max_models` caps the catalog only; a matching model beyond the cap is still callable.

## Endpoints

| Path | Auth | Purpose |
|---|---|---|
| `GET /health` | minimal (full stats with token) | liveness + key stats + filter summary |
| `GET /v1/models` | token | aggregated live catalog with metadata |
| `GET /v1/models/{id}` | token | one model's metadata |
| `POST /v1/*` | token | proxied to the provider chosen by the `model` prefix |
| `GET /logs?limit=&outcome=&provider=&model=` | token | per-request log |
| `POST /refresh`, `POST /catalog/refresh` | token | force catalog refresh |
| `GET /.well-known/opencode` | open | Kilo/OpenCode catalog |

## Error model

| Situation | Returned |
|---|---|
| Billing / no credit / quota | `402 billing_required` + exact upstream message |
| Invalid/revoked key | `401 auth_error` + exact message |
| Rate limited | `429 rate_limited` + `Retry-After` |
| All keys cooling | `429 all_keys_cooling` + seconds to next key |
| Context too long | `400 context_length` + exact message |
| Unknown provider | `404 unknown_provider` |
| Filtered model | `403 model_filtered` |
| Upstream 5xx after all keys | `502 upstream_error` + last message |

Every response carries `x-request-id`; the same id is in `/logs` and
`logs/requests.jsonl` for tracing.

## Production notes

- Runs unprivileged (`app`, uid 10001) with `no-new-privileges`, dropped caps, read-only root FS, `tmpfs /tmp`.
- Startup **refuses to run** without `PROXY_AUTH_TOKEN` unless `allow_no_auth: true`.
- Body size limit: `max_request_bytes` (32 MB default). Optional local rate limit: `rate_limit_per_minute` (0 = off).
- Config toggles: `debug_enabled`, `request_body_log_enabled` (full prompts; off by default), `write_catalog`, `catalog_refresh_seconds`.
- The entrypoint needs `CHOWN/SETUID/SETGID` caps only to fix mount ownership, then drops to `app`.

## Tests & CI

```powershell
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

GitHub Actions (`.github/workflows/ci.yml`) compiles all sources and runs the suite on Python 3.12.

## License

MIT — see [LICENSE](LICENSE).
