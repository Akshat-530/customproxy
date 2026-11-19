# Configuration Reference

Two files, two jobs:

- **`src/config.json`** — behaviour. No secrets.
- **`.env`** — secrets. Never committed.

See also [.env.example](../.env.example) for a ready-to-copy template.

## Top-level keys (`src/config.json`)

| Key | Default | Meaning |
|---|---|---|
| `proxy_port` | `3001` | Port *inside* the container (host port set by compose) |
| `auth_token` | `""` | Legacy fallback; prefer `PROXY_AUTH_TOKEN` in `.env` |
| `allow_no_auth` | `false` | If `true`, runs without a token (trusted local only) |
| `debug_enabled` | `false` | Enable `/debug` event ring buffer |
| `request_body_log_enabled` | `false` | Log full request bodies (may contain prompts) |
| `max_request_bytes` | `33554432` | Request size limit (413 above) |
| `rate_limit_per_minute` | `0` | Local per-client limit (0 = off) |
| `cors_origins` | `["*"]` | CORS allow-list |
| `catalog_refresh_seconds` | `600` | Catalog refresh interval (min 60) |
| `write_catalog` | `true` | Rewrite the catalog file on refresh |
| `request_log_enabled` | `true` | Write `logs/requests.jsonl` |
| `request_log_entries` | `2000` | In-memory log ring size |
| `request_log_jsonl` | `/logs/requests.jsonl` | Log path (env `REQUEST_LOG_JSONL` overrides) |
| `kilo_config_path` | `/state/kilo_models.json` | Catalog path (env `KILO_CONFIG_PATH` overrides) |
| `provider_filters` | `{include:[],exclude:[]}` | Provider allow/deny (globs or `re:`) |

Environment overrides: `PROXY_PORT`, `PROXY_AUTH_TOKEN`, `KILO_CONFIG_PATH`,
`REQUEST_LOG_JSONL`, `CATALOG_REFRESH_SECONDS`, `WRITE_CATALOG`, `ALLOW_NO_AUTH`.

## Provider block

```json
"myprovider": {
  "enabled": true,
  "base_url": "https://api.example.com/v1",
  "keys_env": "MYPROVIDER_API_KEYS",
  "keys_json_env": "MYPROVIDER_API_KEYS_JSON",
  "models": [],
  "include_models": [],
  "exclude_models": [],
  "max_models": 0,
  "manual_models": [],
  "filter_free": false,
  "reasoning_auto": true,
  "reasoning_default": "medium",
  "reasoning_levels": ["low", "medium", "high"]
}
```

| Field | Meaning |
|---|---|
| `base_url` | Any OpenAI-compatible `/v1` base (a provider, a gateway, or your own proxy) |
| `keys_env` | Name of the `.env` variable holding the key pool |
| `models` | Exact ids **or globs**; empty `[]` = discover everything |
| `include_models` | Kept only if it matches one pattern |
| `exclude_models` | Dropped from catalog **and blocked on calls** (`403 model_filtered`) |
| `max_models` | Catalog cap, free models first (catalog-only, not access) |
| `manual_models` | Hand-added models; bypass filters, survive refreshes |
| `filter_free` | Drop models positively known to be paid |

Patterns are `fnmatch` globs (case-insensitive) or `re:<regex>`, and match the
**upstream id or the catalog id** (`deepseek-*` and `myprovider/deepseek-*`
both work). Filter fields must be JSON arrays.

## Key pools (`.env`)

```env
MYPROVIDER_API_KEYS=main=sk-aaa,backup=sk-bbb
MYPROVIDER_API_KEYS_JSON=[{"label":"main","key":"sk-aaa"}]
```

- `label=key` per item; `label` is the id shown in `/logs` (`api_key_id`).
- The label prefix is stripped before the key is sent upstream.
- Use the JSON form for keys containing `=`.
- Pools are shared with the rotation relays when their env var names match
  (`TOKENHARBOUR_API_KEYS`, `GEMINI_API_KEYS`, `ZRO_API_KEYS`).

## Endpoints

| Path | Auth | Purpose |
|---|---|---|
| `GET /health` | minimal / full with token | liveness + key stats + filters |
| `GET /v1/models` | token | live catalog with metadata |
| `GET /v1/models/{id}` | token | one model's metadata |
| `POST /v1/*` | token | forwarded to the provider in the `model` prefix |
| `GET /logs` | token | request log (`limit`, `outcome`, `provider`, `model`) |
| `POST /refresh`, `POST /catalog/refresh` | token | force a catalog refresh |
| `GET /.well-known/opencode` | open | harness-ready catalog block |

## Deployment

Changing `.env` requires a **recreate** (env is read at container creation):

```bash
docker compose up -d --force-recreate
```

Changing `src/config.json` needs only a restart:

```bash
docker compose restart
```
