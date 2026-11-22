# CustomProxy

**One OpenAI-compatible endpoint. One key. Every provider behind it.**

CustomProxy sits in front of every model provider you hold keys for and exposes
them through a single OpenAI-compatible API. Point any client at it, use one
token, and call any model as `provider/model-id`. Keys are pooled per provider
and rotated automatically, the catalog refreshes itself from each provider's
`/models`, and upstream errors reach you unchanged instead of collapsing into a
generic failure.

[![CI](https://github.com/Akshat-530/customproxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Akshat-530/customproxy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12-3776AB.svg)
![Tests](https://img.shields.io/badge/tests-46%20passing-brightgreen.svg)
![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)

```text
   client  (Kilo · OpenCode · LangChain · OpenAI SDK · curl)
      │   Authorization: Bearer <PROXY_AUTH_TOKEN>
      ▼
   CustomProxy  :3001
      ├── provider-a   keys k1..kN   rotated on 401/402/403/429/5xx
      ├── provider-b   keys k1..kN
      └── ...          model id = provider-a/<upstream-model-id>
```

Nothing else is required. The proxy runs fine with a single provider and ships
with no external network or host-path dependencies. It publishes on
`127.0.0.1` only.

## Contents

- [Highlights](#highlights)
- [Quick start](#quick-start)
- [Key format](#key-format)
- [Adding a provider](#adding-a-provider)
- [Model filters](#model-filters)
- [Endpoints](#endpoints)
- [Error model](#error-model)
- [Security](#security)
- [Portability](#portability)
- [Tests and CI](#tests-and-ci)
- [Repository layout](#repository-layout)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Highlights

| Feature | Detail |
|---|---|
| OpenAI-compatible | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, streaming SSE, tool calls, multipart audio and images |
| Any base URL | Works whether the upstream path includes `/v1` or not |
| Key rotation | Per-provider pools, rotated on `401`/`402`/`403`/`429`/`5xx`, escalating cooldown, `Retry-After` honoured |
| Dynamic catalog | Discovers every model from each provider's `/models`, refreshes on a timer, serves `/v1/models` and `/.well-known/opencode` |
| Free-model discovery | `filter_free` providers expose only free models, discovered live. Pre-enabled for `kilo`, `nvidia`, `openrouter` |
| Model filters | `models`, `include_models`, `exclude_models` (globs and `re:` regex), `max_models`, `filter_free`, enforced on the catalog and on direct calls |
| Provider filters | `provider_filters` include/exclude, plus per-provider `enabled: false` |
| Env-defined providers | `CUSTOM_PROVIDERS_JSON` adds providers without editing `config.json` |
| Metadata | Context window, max output, pricing, modalities, category, `free` flag and capability flags |
| Thinking control | Per-provider `reasoning_auto` / `reasoning_default` / `reasoning_levels`, applied only to reasoning-capable models |
| Exact errors | Billing `402`, auth `401`, rate limit `429` with `Retry-After`, context `400`; never a generic "keys exhausted" |
| Request log | In-memory and JSONL: outcome, status, latency, key label, attempts, token usage, exact upstream error |
| Secret safety | Keys live only in `.env`; responses and logs are scrubbed of any configured key |

## Quick start

Two prerequisites: Docker with Compose v2 (the easy path), or Python 3.11+ to
run it directly. You also need at least one provider API key. With no keys the
proxy still starts, but `/v1/models` stays empty and every call returns
`503 no_keys`. There are no fixed paths, no external networks and nothing else
to set up.

```bash
cp .env.example .env          # Windows:  copy .env.example .env
```

Set `PROXY_AUTH_TOKEN` in `.env` to a long random string:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Uncomment the provider key pools you have (a provider with no keys is skipped,
not an error), then start it:

```bash
docker compose up -d --build
curl http://127.0.0.1:3001/health
```

Point any OpenAI client at it:

- base_url: `http://127.0.0.1:3001/v1`
- api_key: the `PROXY_AUTH_TOKEN` you set
- model: `provider/upstream-model-id` (list them with `GET /v1/models`)

The port binds to `127.0.0.1` only. For remote access use an SSH tunnel or a
TLS reverse proxy you control.

### Without Docker

```bash
python -m venv .venv
source .venv/bin/activate          # Windows:  .venv\Scripts\activate
pip install -r requirements.txt
export PROXY_AUTH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python src/server.py
```

The catalog and request log are written to `state/` and `logs/` next to the
project.

### Changing the port

Set `PROXY_HOST_PORT` in `.env` when `3001` is already taken. The proxy still
listens on `3001` inside the container; only the published port changes.

```env
PROXY_HOST_PORT=8080
```

## Key format

One pool per provider, comma separated. Each item is `key` or `label=key`. The
label only identifies the key in `/logs` and is stripped before the request goes
upstream.

```env
TOKENHARBOUR_API_KEYS=th-1=thk_xxx,th-2=thk_yyy
NVIDIA_API_KEYS=nv-1=nvapi-xxx
OPENROUTER_API_KEYS_JSON=[{"label":"or-1","key":"sk-or-xxx=="}]
```

Use the JSON form when a key itself contains `=` (set `keys_json_env` on the
provider in `src/config.json`).

## Adding a provider

In `src/config.json`:

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

Then add `MYPROVIDER_API_KEYS=mp-1=sk-xxx` to `.env`.

Or define it entirely in the environment, with no config edit:

```env
CUSTOM_PROVIDERS_JSON=[{"name":"myprovider","base_url":"https://api.example.com/v1","keys_env":"MYPROVIDER_API_KEYS","models":[]}]
```

The provider id becomes the model prefix: `myprovider/<upstream-model-id>`.

## Model filters

| Key | Meaning |
|---|---|
| `models` | Exact ids or globs; empty discovers everything |
| `include_models` | A model must match one pattern to be kept |
| `exclude_models` | Matching models are dropped from the catalog and refused on direct calls (`403 model_filtered`) |
| `filter_free` | Keeps only models positively known to be free. Paid models are dropped from the catalog and refused on direct calls; unknown-pricing models are kept, but once a provider map has loaded, an id the upstream never listed is refused too |
| `max_models` | Caps the catalog, free models first. Capped-out ids are refused on direct calls as well |

Precedence: `models`, then `include_models`, `exclude_models`, `filter_free`,
`max_models`. `manual_models` bypass every filter, including `filter_free`.

## Endpoints

| Path | Auth | Purpose |
|---|---|---|
| `GET /health` | minimal body, full stats with token | Liveness, key stats, filter summary |
| `GET /v1/models` | token | Live catalog with metadata |
| `GET /v1/models/{id}` | token | Metadata for one model |
| `POST /v1/*` | token | Proxied to the provider named in the model prefix |
| `GET /logs?limit=&outcome=&provider=&model=` | token | Per-request log |
| `POST /refresh`, `POST /catalog/refresh` | token | Force a catalog refresh |
| `GET /.well-known/opencode` | none | Kilo/OpenCode catalog |

## Error model

| Situation | Response |
|---|---|
| Billing or quota | `402 billing_required` with the exact upstream message |
| Invalid or revoked key | `401 auth_error` with the exact message |
| Rate limited | `429 rate_limited` with `Retry-After` |
| All keys cooling | `429 all_keys_cooling` with seconds to the next key |
| Context too long | `400 context_length` with the exact message |
| Unknown provider | `404 unknown_provider` |
| Filtered model | `403 model_filtered` |
| Upstream 5xx after all keys | `502 upstream_error` with the last message |

Every response carries `x-request-id`, the same id you find in `/logs` and
`logs/requests.jsonl`.

## Security

- Runs as unprivileged `app` (uid 10001) with `no-new-privileges`, all
  capabilities dropped except the three the entrypoint needs, a read-only root
  filesystem and a `tmpfs` `/tmp`.
- Refuses to start without `PROXY_AUTH_TOKEN` unless `allow_no_auth: true` is
  set.
- Body size limit `max_request_bytes` (32 MB default) and an optional local rate
  limit. `X-Forwarded-For` is trusted only for peers listed in
  `trusted_proxies`.
- Upstream keys stay in the deployment: injected from `.env`, never written to
  `config.json`, never returned by an endpoint, and scrubbed from logs and error
  messages.

See [SECURITY.md](SECURITY.md) for the key-leak runbook.

## Portability

Nothing here is tied to one machine.

- Catalog and log paths are relative and resolve against the project root,
  detected from the file layout, so a repo checkout and `/app/customproxy`
  inside the image both work. Override with `CUSTOMPROXY_HOME`.
- `compose.yml` is self-contained: no external networks, no sibling projects.
- No fixed host port; `PROXY_HOST_PORT` changes it.
- No provider is required; a provider without keys is skipped at startup.
- Multi-arch base image (`amd64`, `arm64`) and every system dependency installed
  from apt.

## Tests and CI

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

46 tests cover routing, rotation, the catalog, filters, error classification,
the request log, the well-known endpoint and the free filter, across unit and
end-to-end suites that run against a mocked upstream. GitHub Actions compiles
every source file and runs pytest on Python 3.12.

## Repository layout

```text
customproxy/
├── src/server.py          proxy core (FastAPI: routing, rotation, catalog)
├── src/config.json        provider definitions (no secrets, env names only)
├── key_rotate_proxy.py    per-service key-rotation relays for MCP tools
├── run-services.sh        container PID 1: proxy plus relay supervisor
├── entrypoint.sh          fixes mount ownership, then drops to uid 10001
├── compose.yml            hardened standalone stack
├── Dockerfile
├── tests/                 pytest suite (46 tests)
├── docs/                  ARCHITECTURE.md, CONFIGURATION.md
├── skills/                agent skills for provider, key and model management
├── .env.example           every environment variable, placeholders only
└── .github/               CI and issue/PR templates
```

## Documentation

| Document | Covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, request path, catalog refresh, error contract |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every config key, env override, filter semantics, key pools |
| [SECURITY.md](SECURITY.md) | Secret handling, runtime hardening, key-leak runbook |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Local setup, test commands, review rules |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for
setup and the test command. Report vulnerabilities through the private channel
in [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
