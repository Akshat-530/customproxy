---
name: customproxy-remove
description: >-
  Remove a model, a provider, or an API key from CustomProxy. Use when the
  user says "remove <model>", "drop provider X", "delete this key", "take
  this model out", or wants to undo an add. Backs up, edits the right place,
  recreates, and proves the removal (catalog gone + call blocked + relays
  intact).
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
Discover it at runtime instead of assuming:
`docker compose port customproxy 3001`. Resolve it from `<customproxy-dir>`, so it
works whatever the container is named. Base URL is `http://127.0.0.1:<port>/v1`.

Everything else in this skill is relative to `<customproxy-dir>`: config at
`src/config.json`, secrets at `.env`, catalog at `state/kilo_models.json`,
logs at `logs/requests.jsonl`, compose file at `compose.yml`.

# CustomProxy - Remove (model / provider / API key)

Deployment: `<customproxy-dir>` - container `CustomProxy`
on `127.0.0.1:<port>`.
Config `src/config.json`, secrets `.env`, catalog `state/kilo_models.json`
(generated - never hand-edit).

```powershell
$tok = (Get-Content .env | Where-Object { $_ -match '^PROXY_AUTH_TOKEN=' }) -replace '^PROXY_AUTH_TOKEN=',''
$hdr = @{ Authorization = "Bearer $tok" }
```

Always start with:

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bk = "<customproxy-dir>\Backups\customproxy_remove_$stamp"
New-Item -ItemType Directory -Force $bk | Out-Null
Copy-Item src\config.json "$bk\config.json" -Force
Copy-Item .env "$bk\.env" -Force
"backup: $bk"
```

Then follow the section that matches the request. **Ask which one** if the
user just says "remove it" without a target.

---

> **Filter matching rules (verified live):**
> - Patterns match the **upstream model id OR the catalog id** - both
>   `deepseek-*` and `tokenharbor/deepseek-*` work.
> - Patterns are glob (`fnmatch`, case-insensitive) or `re:<regex>`.
>   Do **not** backslash-escape; `-` is literal and needs no escape.
> - Filter fields must be JSON **arrays**. A bare string is coerced to one
>   pattern by the proxy, but write arrays: `"exclude_models": ["x"]`.
> - `manual_models` also accepts a bare string or single object.
## A) Remove a model

**Important semantics - remove vs block:**

- Removing a model from the catalog (`manual_models` entry, or the `models`
  allowlist) only removes it from **listing/metadata**. With no filter
  matching it, a direct call to that id is still proxied upstream (by design,
  so brand-new upstream models work without a refresh).
- To **block access**, the id must be covered by `exclude_models` - that is
  enforced everywhere: catalog listing, `/v1/models/{id}` (404
  `model_filtered`), and `/v1/chat/completions` (403 `model_filtered`).

So "remove this model" = do both when the user wants it gone for real:

Determine how the model is currently exposed:

1. **`manual_models`** (pinned) → delete its entry **and** add a precise
   `exclude_models` pattern for its id (blocks calls + metadata).
2. **Discovered model** → add a precise `exclude_models` pattern (preferred
   over editing `include_models`, which could remove others).
3. **`models` allowlist** → remove that exact id/glob **and**, if the provider
   has open discovery elsewhere, add the `exclude_models` pattern to block it.
4. **`max_models` cap** → if the model is only absent because of the cap, no
   edit needed; confirm intent first.

If the user only wants it hidden from the picker (not blocked), say that
removing the entry alone does that - and that direct calls still work.

Apply, recreate, and prove all three:

```powershell
docker compose up -d --force-recreate
```

```powershell
# 1) gone from the catalog listing
$mods = Invoke-RestMethod "http://127.0.0.1:<port>/v1/models" -Headers $hdr
[bool]($mods.data | Where-Object { $_.id -eq '<provider>/<model-id>' })   # expect False

# 2) metadata blocked (needs the exclude_models pattern)
try { Invoke-RestMethod "http://127.0.0.1:<port>/v1/models/<provider>/<model-id>" -Headers $hdr }
catch { [int]$_.Exception.Response.StatusCode }   # expect 404

# 3) direct call blocked (needs the exclude_models pattern)
try { Invoke-RestMethod "http://127.0.0.1:<port>/v1/chat/completions" -Headers ($hdr + @{"Content-Type"="application/json"}) -Method Post -Body '{"model":"<provider>/<model-id>","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' }
catch { [int]$_.Exception.Response.StatusCode; ($_.ErrorDetails.Message | ConvertFrom-Json).error.type }  # expect 403 model_filtered
```

```powershell
# 4) catalog file refreshed without it (after one refresh cycle)
docker compose logs --tail 5 | Select-String "catalog written"
```

## B) Remove a provider

Order matters: **models → keys → provider block → env var → recreate**.

1. Confirm impact: the provider currently exposes N models and has K keys.
   Show them (labels only) and ask: *"Remove the whole provider, or just
   its keys?"*
2. Remove the `providers.<id>` block from `src/config.json`.
3. **Shared env pools:** `TOKENHARBOUR_API_KEYS`, `GEMINI_API_KEYS`,
   `ZRO_API_KEYS`, `TAVILY_API_KEYS`, `FIRECRAWL_API_KEYS`,
   `CONTEXT7_API_KEYS` also feed the **rotation relays** (ports 4016–4030).
   Deleting those variables breaks the MCP relays. If the pool is shared:
   - keep the env var, or
   - blank it **and** remove the matching service from
     `KEY_ROTATE_SERVICES` in `compose.yml`, or
   - warn the user explicitly and get confirmation.
   For a CustomProxy-only pool (`NVIDIA_API_KEYS`, `AGNES_API_KEYS`, …),
   remove the line.
4. Recreate and verify:

```powershell
docker compose up -d --force-recreate
$h = Invoke-RestMethod "http://127.0.0.1:<port>/health" -Headers $hdr
$h.providers.PSObject.Properties.Name            # provider must be absent
$h.providers.PSObject.Properties.Count
```

```powershell
# models gone too
$mods = Invoke-RestMethod "http://127.0.0.1:<port>/v1/models" -Headers $hdr
($mods.data | Where-Object { $_.owned_by -eq '<id>' } | Measure-Object).Count   # expect 0

# relays still healthy (they share pools - prove nothing broke)
docker compose ps --format "{{.Name}} | {{.Status}}"                            # expect (healthy)
```

5. Optional cleanup: remove the `XY_API_KEYS` line from `.env.example` if the
   provider was documented there.

## C) Remove an API key

1. Read the pool (labels + last-4 only) and confirm which label to remove.
2. **Refuse to remove the last key** if the provider would become keyless -
   ask first; then either remove the provider (B) or keep a placeholder.
3. Remove the `label=key` item from `VAR=` in `.env` (rebuild the
   comma-separated list without it, preserving other labels; BOM-free write).
4. If the pool is shared with relays (see list in B), warn: removing a key
   also reduces relay capacity, and removing the **last** one breaks that
   relay's health (`ok=false`) → container unhealthy.
5. Recreate and verify:

```powershell
docker compose up -d --force-recreate
$h = Invoke-RestMethod "http://127.0.0.1:<port>/health" -Headers $hdr
$h.providers.<provider>.keys | Select-Object label,requests,errors   # label must be gone
```

```powershell
# rotation still works with the remaining keys (2-3 tiny calls, free model)
$mods = Invoke-RestMethod "http://127.0.0.1:<port>/v1/models" -Headers $hdr
($mods.data | Where-Object { $_.owned_by -eq '<provider>' -and $_.id -like '*:free' } | Select-Object -First 1).id
# then /logs -> distinct api_key_id, all outcome=ok
```

Also confirm the removed key is not still in `src/config.json` (it never
should be - keys live only in `.env`).

## Rollback

Restore the files from the backup taken at the top, then recreate:

```powershell
Copy-Item "$bk\config.json" src\config.json -Force
Copy-Item "$bk\.env" .env -Force
docker compose up -d --force-recreate
```

## Report

- What was removed (model id / provider id / key label).
- Where it was removed from (manual_models / exclude_models / provider block /
  env pool).
- Verification results: catalog count before→after, direct-call status, health
  status, relays status.
- Backup path.

## Safety rules

- Never remove the last key of a provider without explicit confirmation.
- Never delete a shared pool used by relays without warning + confirmation.
- Never hand-edit `state/kilo_models.json`.
- BOM-free writes only; re-parse `src/config.json` after editing.
- If removal verification fails, restore the backup immediately and report
  the exact error instead of retrying blindly.
