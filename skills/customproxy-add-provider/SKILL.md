---
name: customproxy-add-provider
description: >-
  Add a whole new model provider to CustomProxy. Use when the user says
  "add provider X", "support <site>", or gives a URL/site name of a provider.
  Researches the provider's OpenAI-compatible API (base URL, auth style,
  model listing, pricing), configures it, then chains into the
  customproxy-add-api-key and customproxy-add-model skills for keys and
  models, and runs an end-to-end test.
---


## Finding the deployment (portability)

All paths in this skill are written as `<customproxy-dir>`. Resolve it at
runtime, in this order:

1. The folder containing this `skills/<skill-name>/` directory (go up two
   levels; the deployment sits beside `skills/`).
2. If not found there, search: `Get-ChildItem -Path . -Recurse -Filter
   "compose.yml" -Depth 4 | Where-Object { $_.DirectoryName -match
   "customproxy" }` from the current working directory.
3. Never assume a fixed drive letter or user path - the deployment may live
   anywhere: Windows, macOS, Linux, or WSL.

The proxy's host port is published by `compose.yml` (default `3001`).
Discover it at runtime instead of assuming: `docker port CustomProxy
3001/tcp`). Base URL is `http://127.0.0.1:<port>/v1`.

Everything else in this skill is relative to `<customproxy-dir>`: config at
`src/config.json`, secrets at `.env`, catalog at `state/kilo_models.json`,
logs at `logs/requests.jsonl`, compose file at `compose.yml`.

# CustomProxy - Add a Provider

Deployment: `<customproxy-dir>`
Container: `CustomProxy` (port `127.0.0.1:<port>`).
Config: `src/config.json`. Secrets: `.env`. Skills to chain:
`customproxy-add-api-key` (keys) and `customproxy-add-model` (models).

**Never print API keys.** Provider id = short lowercase slug used as the
model prefix (`provider/model-id`) and the env var name.

---

## Step 1 - Resolve the provider from URL or name

Input forms: `https://api.example.com/v1`, `example.com`, or a brand name
("OpenRouter", "Together AI").

Normalize:
- strip scheme/paths → registrable domain (e.g. `openrouter.ai`)
- id = short lowercase slug, `a-z0-9_-` only, no leading digit if possible
  (e.g. `openrouter`, `together`, `deepinfra`) - this becomes the model prefix
  and must not collide with existing ids:

```powershell
Set-Location "<customproxy-dir>"
$cfg = Get-Content src\config.json -Raw | ConvertFrom-Json
$cfg.providers.PSObject.Properties.Name
```

- If the provider **already exists**, stop and offer `customproxy-add-api-key`
  instead.

## Step 2 - Research the provider (web tools vary per harness)

Find, from the provider's own docs (API reference / "OpenAI compatibility"
page) - web search + page scrape:

| Field | Needed | Notes |
|---|---|---|
| OpenAI-compatible base URL | required | e.g. `https://api.x.ai/v1`; must end at the version segment (`/v1`, `/v1beta/openai`, `/api/v1`) |
| Auth style | required | `Authorization: Bearer` (preferred), `x-api-key`, or query-param |
| `/models` listing | required | must return `{"data":[{"id":...}]}` - this powers discovery |
| Chat completions | required | `/chat/completions` in OpenAI format |
| Streaming (SSE) | required | `stream: true` support |
| Pricing/model page | for docs | context windows, free tier, rate limits |
| Known quirks | if any | e.g. needs `max_tokens` not `max_completion_tokens`, no tool calling, context-length error format |

If the provider is **not** OpenAI-compatible (no `/chat/completions` shape),
say so plainly and stop - CustomProxy only speaks OpenAI format.

If `/models` is unavailable, the provider can still be added - models will
come from `manual_models` (via the add-model skill) instead of discovery.
Say this explicitly.

Unknown after research → mark `unknown` and ask the user to confirm the
value; never guess a base URL.

## Step 3 - Present the setup table (wait for approval)

| Field | Value | Source |
|---|---|---|
| Provider id (slug) | `xy` | derived |
| Base URL | `https://.../v1` | docs |
| Auth header | `Authorization: Bearer` | docs |
| `/models` discovery | yes / no | docs |
| Chat + streaming | yes / yes | docs |
| Free tier | yes/no/unknown | docs |
| Known quirks | ... or none | docs |
| Keys env var | `XY_API_KEYS` | derived |

Ask: *"Create this provider in CustomProxy?"* - do not write anything before
approval.

> **Filter matching rules (verified live):**
> - Patterns match the **upstream model id OR the catalog id** - both
>   `deepseek-*` and `tokenharbor/deepseek-*` work.
> - Patterns are glob (`fnmatch`, case-insensitive) or `re:<regex>`.
>   Do **not** backslash-escape; `-` is literal and needs no escape.
> - Filter fields must be JSON **arrays**. A bare string is coerced to one
>   pattern by the proxy, but write arrays: `"exclude_models": ["x"]`.
> - `manual_models` also accepts a bare string or single object.
## Step 4 - Create the provider (BOM-free, backed up)

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bk = "<customproxy-dir>\Backups\customproxy_config_$stamp"
New-Item -ItemType Directory -Force $bk | Out-Null
Copy-Item src\config.json "$bk\config.json" -Force
"backup: $bk"
```

Add the block (keep it minimal; filters empty = discover everything):

```powershell
$utf8 = New-Object System.Text.UTF8Encoding($false)
$cfg = Get-Content src\config.json -Raw | ConvertFrom-Json
$cfg.providers | Add-Member -NotePropertyName "xy" -NotePropertyValue ([pscustomobject]@{
  base_url = "https://api.example.com/v1"
  keys_env = "XY_API_KEYS"
  models = @()
  manual_models = @()
  include_models = @()
  exclude_models = @()
  max_models = 0
  filter_free = $false
  reasoning_auto = $true
  reasoning_default = "medium"
  reasoning_levels = @("low","medium","high")
}) -Force
[System.IO.File]::WriteAllText("$PWD\src\config.json", ($cfg | ConvertTo-Json -Depth 12), $utf8)
```

Also append `XY_API_KEYS=` to `.env` (BOM-free) if not present, and add the
provider to `.env.example` with placeholders - the repo should always show
the format for new users:

```env
XY_API_KEYS=xy-1=sk-REPLACE_ME
```

Do **not** restart yet - the provider has no keys and would just log
"routing map unavailable". Chain to Step 5 first.

## Step 5 - Chain: add the API key (skill)

Follow **`customproxy-add-api-key`** end-to-end for this provider:
two-options key handoff → validate against `{base_url}/models` → pool
check → label (`xy-1`) → backup → `.env` write → `docker compose up -d
--force-recreate` → label visible in `/health`.

First deploy note: the first recreate will log
`[xy] routing map unavailable` if `/models` needs a valid key - that is
expected until the key lands; after the key recreate, confirm
`/v1/models` includes `xy/...` ids.

Whitelisting rule (applies here and in both chained skills): if the user
wants only selected models, set `include_models` patterns now; to hide
specific ones use `exclude_models`; both are enforced on the catalog **and**
on direct calls (`403 model_filtered`). Record in the setup table which
patterns were set.

## Step 6 - Chain: add models (skill)

Follow **`customproxy-add-model`** for one or more models:
- Discovery: `GET /v1/models` → filter `owned_by == "xy"`.
- If discovery is empty (no `/models`), add via `manual_models` in
  `src/config.json` (id + context/output/tool_call/free/pricing) - these
  bypass filters and survive refreshes.
- Whitelist per the user's intent (include/exclude/max_models).
- Approval table first; minimal-token test after deploy; report
  `api_key_id`, duration, usage from `/logs`.

## Step 7 - End-to-end test (the full stack)

Run in order; each must pass before the next:

```powershell
$tok = (Get-Content .env | Where-Object { $_ -match '^PROXY_AUTH_TOKEN=' }) -replace '^PROXY_AUTH_TOKEN=',''
$hdr = @{ Authorization = "Bearer $tok" }

# 1. container healthy
docker ps --filter name=CustomProxy --format "{{.Status}}"

# 2. provider present in health
$h = Invoke-RestMethod "http://127.0.0.1:<port>/health" -Headers $hdr
$h.providers.xy

# 3. keys in pool (label visible, requests=0)
$h.providers.xy.keys

# 4. catalog contains the provider
$mods = Invoke-RestMethod "http://127.0.0.1:<port>/v1/models" -Headers $hdr
($mods.data | Where-Object { $_.owned_by -eq "xy" } | Measure-Object).Count

# 5. chat works (free model preferred; else approval)
$body = '{"model":"xy/<model-id>","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":16}'
Invoke-RestMethod "http://127.0.0.1:<port>/v1/chat/completions" -Headers ($hdr + @{"Content-Type"="application/json"}) -Method Post -Body $body -TimeoutSec 120

# 6. rotation proof: 2-3 calls, distinct api_key_id in /logs (if >1 key)
$logs = Invoke-RestMethod "http://127.0.0.1:<port>/logs?limit=10&provider=xy" -Headers $hdr
$logs.entries | Select-Object outcome,api_key_id,attempts,duration_ms,http_status,error_type | Format-Table

# 7. filter enforcement spot check (if include/exclude configured)
#    a filtered id must return 403 model_filtered without hitting upstream
```

Pass = healthy container + provider in health + keys listed + models in
catalog + chat 200 + rotation advancing (or single-key noted) + filters
behaving.

On any failure: show the exact `error.message`/`error_type`, fix, and re-run
only the failed step. Rollback = restore `src/config.json` backup, remove the
`.env` line, recreate.

## Step 8 - Report

Reply with:

- Provider **id**, base URL, auth style, discovery yes/no.
- Keys: count + labels. Models: count + a few catalog ids (with `free`/`category`).
- End-to-end test table: each step pass/fail, plus one log line
  (`outcome`, `model`, `api_key_id`, `duration_ms`).
- Whitelist summary (patterns set, or "open discovery").
- Reminder: keys live in `.env` (gitignored) and never in `config.json`.
  Harness wiring is **not** done here - direct the user to
  **customproxy-connect-harness**, the only skill that edits harness files.

## Removal

To undo this later (remove the model / provider / key), follow
**customproxy-remove** - it backs up, edits the correct place, recreates, and
proves the removal (catalog gone + call blocked + relays intact).

## Safety rules

- Never print API keys; label + last-4 only.
- One backup before every config/`.env` edit; rollback = restore + recreate.
- BOM-free writes only for `.env` and `src/config.json`.
- Provider id must be a safe slug - it becomes a URL path segment and env var
  name; reject spaces/symbols.
- If research can't confirm OpenAI compatibility, stop and say so rather than
  adding a broken provider.
