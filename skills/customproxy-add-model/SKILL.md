---
name: customproxy-add-model
description: >-
  Add a model to the CustomProxy catalog. Use when the user says "add this
  <model> by <provider>" or wants to expose a specific model through
  CustomProxy (127.0.0.1:3101). Researches the model, builds an approval
  table, whitelists it, verifies it with a minimal-token call, and reports
  follow-up steps for other harnesses (Kilo, OpenCode, etc.). Part of the
  CustomProxy skill chain: add-provider → add-api-key → add-model.
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

The proxy's host port is published by `compose.yml` (default `3101`).
Discover it at runtime instead of assuming: `docker port CustomProxy
3001/tcp` -> e.g. `127.0.0.1:3101`. Base URL is `http://127.0.0.1:<port>/v1`.

Everything else in this skill is relative to `<customproxy-dir>`: config at
`src/config.json`, secrets at `.env`, catalog at `state/kilo_models.json`,
logs at `logs/requests.jsonl`, compose file at `compose.yml`.

# CustomProxy - Add a Model

Part of the CustomProxy chain - `customproxy-add-provider` →
`customproxy-add-api-key` → **this skill**. If the provider or key doesn't
exist yet, run those skills first, then return here.

Goal: take "add model X by provider Y" and end with X live in CustomProxy,
tested, and whitelisted - with the user approving the details first.

> **Filter matching rules (verified live):**
> - Patterns match the **upstream model id OR the catalog id** - both
>   `deepseek-*` and `tokenharbor/deepseek-*` work.
> - Patterns are glob (`fnmatch`, case-insensitive) or `re:<regex>`.
>   Do **not** backslash-escape; `-` is literal and needs no escape.
> - Filter fields must be JSON **arrays**. A bare string is coerced to one
>   pattern by the proxy, but write arrays: `"exclude_models": ["x"]`.
> - `manual_models` also accepts a bare string or single object.
## Whitelist rules (apply here, and in add-provider / add-api-key)

Model access is controlled per provider in `src/config.json`:

| Key | Effect |
|---|---|
| `models` | exact ids **or globs**; empty `[]` = discover everything |
| `include_models` | model must match one pattern (glob or `re:<regex>`) |
| `exclude_models` | matching models are dropped from the catalog **and** blocked on direct calls (`403 model_filtered`) |
| `max_models` | caps the catalog (free models first); catalog-only, not access |
| `manual_models` | hand-added models; bypass all filters, survive refreshes |

Whitelisting is required only when a filter would hide the model. After any
filter change: restart/recreate, confirm the model appears in `/v1/models`,
confirm it is callable, and confirm a filtered-out model 403s.

## Alternative: pin it via manual_models

If the provider's `/models` does not expose the model (or you want it pinned
regardless of discovery/filters), add it under the provider block instead of
fighting filters:

```json
"manual_models": [
  {"id": "<exact-upstream-id>", "context": 900000, "output": 32000,
   "tool_call": true, "reasoning": true, "free": true,
   "pricing": {"prompt": "0", "completion": "0"}}
]
```

Manual models always appear in `/v1/models`, bypass include/exclude filters,
and survive refresh cycles. This is also the only way to expose a model the
upstream omits from its listing.

## Step 0 - Locate the deployment

1. The standalone deployment lives in `<customproxy-dir>`.
   Config: `src/config.json`. Catalog: `state/kilo_models.json`. Log:
   `logs/requests.jsonl`. Port: `127.0.0.1:3101`.
2. Read the auth token from that project's `.env` (`PROXY_AUTH_TOKEN=`) -
   never print it. Example header: `Authorization: Bearer $env:PAT`.
3. Confirm the container is running:
   `docker ps --filter name=CustomProxy --format "{{.Status}}"`.
   If unhealthy, `docker compose up -d --build` in the project dir and re-check
   `GET /health` before continuing.

## Step 1 - Parse the request

From "add this xx model by xy provider" extract:

- **provider** = `xy` → must match a `providers` key in `src/config.json`
  (e.g. `tokenharbor`, `nvidia`, `google`, `kilo`, `openrouter`, …).
- **model** = `xx` → fuzzy-match against the provider's models. Query:
  `GET http://127.0.0.1:3101/v1/models` (with the token) and filter
  `data` where `owned_by == provider` and `id` contains the user's text.
- If several candidates, show them and ask which one.
- If none: the provider's `/models` may not expose it yet - continue with
  Step 2 anyway (the user may know the exact upstream id); if the provider
  can't serve it, tell the user and stop.

## Step 2 - Research the model (web tools may vary per harness)

Gather, using whatever web tools this harness provides (web search, page
scrape, fetch) - official docs and the provider's own model pages are the
best sources:

- **Exact model id** as the provider's API expects it (e.g.
  `deepseek-v4.1-flash:free`).
- **Context window** (input tokens) and **max output tokens**.
- **Tool calling** yes/no.
- **Free or paid**:
  - If **free**: rate limits - RPD (requests/day), RPM (requests/min),
    token caps per request/day, and any other provider-specific limitation.
  - If **paid**: input price, output price, **cached-input price** (if the
    provider prices cached tokens), per 1M tokens and currency.

Any field that cannot be confirmed → **`unknown`** (never guess a number;
say "unknown - search manually" in the table).

Also read what the proxy already knows (if the model is discoverable):

```powershell
curl.exe -s http://127.0.0.1:3101/v1/models/<provider>/<model-id> -H "Authorization: Bearer $env:PAT"
```

Note whether this is catalog data (`metadata.source`) or provider data.
If the provider caps a feature the base model supports (e.g. tool calling
disabled on the free tier), mark it as `yes (provider-capped)`.

## Step 3 - Present the approval table (REQUIRED, wait for approval)

Show **one table** and ask: *"Add this model to the CustomProxy catalog?"*

| Field | Value | Source |
|---|---|---|
| Provider | xy | request |
| Exact model id | `...` | provider API |
| Catalog id | `provider/model-id` | derived |
| Context window (input) | n or unknown | docs / provider |
| Max output tokens | n or unknown | docs / provider |
| Tool calling | yes / no / yes (provider-capped) | docs |
| Tier | Free / Paid | provider |
| Input price | x / 1M tokens or unknown | provider pricing |
| Output price | x / 1M tokens or unknown | provider |
| Cached input price | x / 1M or unknown or n/a | provider |
| RPD / RPM | n / n (free tier) or n/a | provider |
| Token cap | n or unknown | provider |
| Notes | any provider caps, quirks | research |

Do **not** touch any file until the user approves. If the user says no,
stop and ask what to change.

## Step 4 - Whitelist the model (on approval)

Whitelisting is needed only when the provider restricts models. Check
`src/config.json` → `providers.<provider>`:

- `models: []` (empty) → discovery is open; nothing to whitelist, but still
  apply the steps below so filters/caps don't hide it.
- `include_models` non-empty → the model must match a pattern; add an entry
  (exact id or glob, e.g. `deepseek-*`).
- `exclude_models` matches it → remove/adjust the pattern or the model will
  403 (`model_filtered`) on every call.
- `max_models` set → make sure the cap doesn't push it out of the catalog
  (raise it or use `exclude_models` to free space).

Edit `src/config.json` on the host (it is read-only inside the container).
Keep JSON valid and UTF-8 **without BOM**. Then apply:

```powershell
Set-Location "<customproxy-dir>"
docker compose up -d --build      # or: docker compose restart
```

Wait for the healthcheck, then confirm the model appears:

```powershell
curl.exe -s http://127.0.0.1:3101/v1/models -H "Authorization: Bearer $env:PAT" | findstr /i "<model-id>"
```

Also confirm it is **callable** (filters are enforced on direct calls -
a whitelisted model returns metadata, a filtered one returns
`403 model_filtered`).

## Step 5 - Test with minimal token usage

One call, tiny output, no tools:

```powershell
$body = '{"model":"<provider>/<model-id>","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":16}'
curl.exe -s -X POST http://127.0.0.1:3101/v1/chat/completions `
  -H "Authorization: Bearer $env:PAT" -H "Content-Type: application/json" `
  -d $body
```

Success = HTTP 200, content "OK", tiny usage. Then read the log line:

```powershell
Get-Content logs\requests.jsonl -Tail 1 | ConvertFrom-Json
```

Check: `outcome=ok`, `model`, `api_key_id` (which key handled it),
`duration_ms`, `usage`.

If the call fails, show the **exact** `error.message` from the response
(CustomProxy always returns the real upstream error - billing → 402,
auth → 401, rate limit → 429, context → 400) and fix accordingly
(wrong model id, dead key, filter, etc.). Never retest in a loop; one
16-token attempt, fix, one retest.

## Step 6 - Report to the user

Reply with:

1. **"Added"** + the catalog id, and the test result (OK + token usage from
   the log line, e.g. `usage: 14 tokens`).
2. The approval table's key facts (context, tier, pricing/limits) as a short
   summary.
3. **Harness follow-up warning**, verbatim:

> The model is now live in CustomProxy. If you use it through another harness
> (Kilo, OpenCode, etc.) you may also need to add it to that harness's own
> config - CustomProxy's catalog at
> `http://127.0.0.1:3101/.well-known/opencode` can be consumed by those tools,
> but each harness keeps its own model list/selection.

4. If `max_models` was raised or an exclude pattern was changed, mention it.

## Removal

To undo this later (remove the model / provider / key), follow
**customproxy-remove** - it backs up, edits the correct place, recreates, and
proves the removal (catalog gone + call blocked + relays intact).

## Notes / gotchas

- **Never** put the token in the output; read it from `.env` only.
- `src/config.json` must stay BOM-free - if editing with PowerShell, write
  with `[System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))`.
- `state/kilo_models.json` is generated by the proxy - do not hand-edit; it
  refreshes on its own.
- The `-research` step's "unknown" policy is strict: a wrong price in the
  table is worse than `unknown`.
- Free-tier research sources differ per provider (Kilo gateway shows
  `isFree`/pricing in `/models`; OpenRouter has full pricing; NVIDIA rarely
  exposes pricing - expect `unknown` there).
- If the user asks to remove a model later: delete/adjust its filter entries,
  restart, confirm it 403s, then confirm it disappeared from `/v1/models`.
