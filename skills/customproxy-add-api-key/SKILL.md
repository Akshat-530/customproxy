---
name: customproxy-add-api-key
description: >-
  Add an API key to CustomProxy. Use when the user says "I want to add a new
  API key", "add this key", "rotate in this key", etc. Asks for the provider
  if missing, accepts the key from a file path or pasted in chat, validates it
  without spending model tokens, asks replace-vs-add when a key already
  exists, deploys it with a proper key id, and proves rotation works. Part of
  the CustomProxy skill chain: add-provider → add-api-key → add-model.
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

# CustomProxy - Add an API Key

Part of the CustomProxy chain - `customproxy-add-provider` →
**this skill** → `customproxy-add-model`. If the provider doesn't exist yet,
run add-provider first; after the key is in and rotation is proven, run
add-model to expose models.

Whitelist note: this skill does not change model filters. If the user wants
this provider limited to selected models, set `include_models` /
`exclude_models` on its config block (see add-model skill) - filters are
enforced on the catalog and on direct calls.

Deployment: `<customproxy-dir>`
Container: `CustomProxy` (port `127.0.0.1:<port>`).
Secrets live in that project's `.env`; config in `src/config.json`.

**Never print, echo, or log the API key.** Refer to it only by label.

---

## Step 1 - Identify the provider

If the user did not name one, ask:
> Which provider is this key for? (e.g. tokenharbor, nvidia, google, kilo,
> apinex, openrouter, teamorouter, zro, 100milionseek)

Validate it exists:

```powershell
Set-Location "<customproxy-dir>"
$cfg = Get-Content src\config.json -Raw | ConvertFrom-Json
$cfg.providers.PSObject.Properties.Name
```

- Provider exists → note its `keys_env` (e.g. `TOKENHARBOUR_API_KEYS`).
- Provider missing → ask for its `base_url`, then create the provider block in
  `src/config.json` first (`keys_env: "<NAME>_API_KEYS"`, `models: []`,
  `manual_models: []`), and continue. Mention it was created.

## Step 2 - Receive the key (two options, offer both)

> Send me the key one of two ways:
> 1. **File path** - save it to a file and give me the path (e.g.
>    `<path-to-key-file>`). I'll read it and never print it.
> 2. **Paste it here** in chat.

If pasted in chat, warn once: *"Pasted secrets can persist in chat logs -
consider rotating this key later."* Then write it straight to `.env`; do not
repeat it back.

Read from file (no echo):

```powershell
$key = (Get-Content -LiteralPath "<path>" -Raw).Trim()
if (-not $key) { throw "key file is empty" }
"key loaded: length=$($key.Length)"   # length only, never the value
```

Sanity checks (before any network call): no whitespace/newlines inside,
length > 8, no quotes. If it looks like a URL or a placeholder, stop and ask.

## Step 3 - Validate the key (no model tokens)

Use the provider's metadata endpoint only. Costs nothing.

```powershell
$base = $cfg.providers.<provider>.base_url.TrimEnd('/')
$resp = $null
try {
  $resp = Invoke-WebRequest "$base/models" -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 30
  "HTTP $($resp.StatusCode) - key valid"
} catch {
  $code = [int]$_.Exception.Response.StatusCode
  "HTTP $code"
}
```

Interpretation:

| Result | Meaning | Action |
|---|---|---|
| 200 | valid | continue |
| 401 / 403 | invalid or revoked | stop; ask for a correct key |
| 402 | valid but no credit/plan | valid key; warn "billing required" |
| 429 | valid but rate-limited | valid key; warn |
| 404 | endpoint may not exist | try `GET {base}/models` variant or a 1-token test **with approval** |
| network error | provider unreachable | report exact error, stop |

Provider special cases:
- **OpenRouter** - `/models` is public, so it does **not** prove the key.
  Validate with `GET https://openrouter.ai/api/v1/key` + Bearer.
- **Google/Gemini** - base is `.../v1beta/openai`; `/models` validates.
- **NVIDIA** - `/models` validates.
- Any provider that returns 200 without auth → say the key was **not**
  proven, and offer a 1-token chat test (needs approval).

## Step 4 - Existing keys? Ask replace or add

Read the current pool for the provider (label + last-4 only, never full keys):

```powershell
$var = $cfg.providers.<provider>.keys_env
$pools = @{}
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z0-9_]+)=(.*)$') { $pools[$Matches[1]] = $Matches[2].Trim() }
}
$items = @()
foreach ($it in ($pools[$var] -split ',')) {
  $t = $it.Trim(); if (-not $t) { continue }
  if ($t.Contains('=')) { $lab,$k = $t -split '=',2 } else { $lab,$k = 'auto',$t }
  $items += [pscustomobject]@{ label=$lab; last4=("..." + $k.Substring([Math]::Max(0,$k.Length-4))) }
}
$items | Format-Table -AutoSize
```

If keys exist, ask exactly:

> This provider already has **N** key(s): `<labels>`. Do you want to
> 1. **Replace** one (tell me which label), or
> 2. **Add** it to the rotation pool?

Default recommendation: **Add** (rotation is the point of the proxy).
- Replace-all is destructive → require explicit "replace all" wording.
- Never silently delete a key.

## Step 5 - Choose the key id (label)

- Convention: `<provider-prefix>-<next-number>` - e.g. `th-13`, `nv-3`,
  `google-9`, `or-2`.
- Must be unique within that pool. Derive the next free number from the
  existing labels above.
- Labels are what show up in `/logs` (`api_key_id`) and `/health`, so make
  them meaningful.

## Step 6 - Back up, then write to `.env`

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bk = "<customproxy-dir>\Backups\customproxy_env_$stamp"
New-Item -ItemType Directory -Force $bk | Out-Null
Copy-Item .env "$bk\.env" -Force
Copy-Item src\config.json "$bk\config.json" -Force
"backup: $bk"
```

Write the new pool **without BOM**, preserving every other line:

```powershell
$utf8 = New-Object System.Text.UTF8Encoding($false)
$lines = [System.IO.File]::ReadAllLines("$PWD\.env")
$found = $false
for ($i=0; $i -lt $lines.Count; $i++) {
  if ($lines[$i] -match "^$var=") {
    $existing = ($lines[$i] -replace "^$var=","").Trim()
    $newLine = if ($existing) { "$var=$existing,$label=$key" } else { "$var=$label=$key" }
    $lines[$i] = $newLine; $found = $true; break
  }
}
if (-not $found) { $lines += "$var=$label=$key" }
[System.IO.File]::WriteAllText("$PWD\.env", (($lines -join "`n") + "`n"), $utf8)
```

For **replace**, rebuild the pool from the existing items without the
target label, then append `label=key`. For JSON-form pools
(`keys_json_env`), edit that JSON array instead, same rules.

Also confirm `src/config.json` → provider has `"keys_env": "<var>"`. Add it if
missing (BOM-free write), then continue.

## Step 7 - Deploy (recreate, not restart)

`.env` is read by Compose at **container creation**, so `docker compose
restart` will NOT pick it up. Use recreate:

```powershell
Set-Location "<customproxy-dir>"
docker compose up -d --force-recreate
docker ps --filter name=CustomProxy --format "{{.Status}}"
```

Wait for `healthy`, then confirm the label is in the pool (stats only):

```powershell
$tok = (Get-Content .env | Where-Object { $_ -match '^PROXY_AUTH_TOKEN=' }) -replace '^PROXY_AUTH_TOKEN=',''
$hdr = @{ Authorization = "Bearer $tok" }
$h = Invoke-RestMethod "http://127.0.0.1:<port>/health" -Headers $hdr
$h.providers.<provider>.keys | Where-Object { $_.label -eq "<label>" }
```

Expect one row for the new label with `requests=0`.

## Step 8 - Prove rotation works (tiny cost)

Pick the cheapest target:
1. A **free** model for that provider (catalog `free = true`) - preferred.
2. If the provider exposes no pricing (all `free = unknown`, e.g. NVIDIA,
   tokenharbor), fall back to a model id ending in `:free` - many aggregators
   mark free tiers in the id.
3. Otherwise one tiny paid call, **with the user's approval** (say the model
   and that it will cost a few tokens).

Then:

```powershell
$models = Invoke-RestMethod "http://127.0.0.1:<port>/v1/models" -Headers $hdr
($models.data | Where-Object { $_.owned_by -eq '<provider>' -and $_.free -eq $true } |
   Select-Object -First 1).id
```

Send **two or three** minimal requests (rotation advances per request):

```powershell
$body = '{"model":"<provider>/<model>","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
1..3 | ForEach-Object {
  try { Invoke-RestMethod "http://127.0.0.1:<port>/v1/chat/completions" -Headers $hdr -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 120 | Out-Null; "call $_ : ok" }
  catch { "call $_ : HTTP $([int]$_.Exception.Response.StatusCode)" }
}
```

Inspect what actually handled each call:

```powershell
$logs = Invoke-RestMethod "http://127.0.0.1:<port>/logs?limit=10&provider=<provider>" -Headers $hdr
$logs.entries | Select-Object request_id,outcome,model,api_key_id,attempts,duration_ms,http_status,error_type | Format-Table
```

Pass criteria:
- New label appears as `api_key_id` on at least one successful call.
- Across the calls, **more than one distinct** `api_key_id` appears (rotation is
  advancing across the pool).
- All `outcome = ok`.

If the new key never appears or calls fail, show the **exact**
`error.message` / `error_type` from the response or log and stop; do not loop
retries. Roll back with the backup and recreate.

## Step 9 - Report

Reply with a short block:

- Provider + **key id** (label) added (or replaced).
- Validation: HTTP code from Step 3.
- Pool size before → after.
- Rotation proof: distinct `api_key_id`s seen across the test calls, and one
  log line (`outcome`, `model`, `key`, `duration_ms`).
- If a free model was unavailable and a paid test was used, state the token
  usage from `usage` in the log/response.
- Reminder: *the key is stored only in `.env` (gitignored); it is never in
  `config.json`, logs, or a commit.*

## Removal

To undo this later (remove the model / provider / key), follow
**customproxy-remove** - it backs up, edits the correct place, recreates, and
proves the removal (catalog gone + call blocked + relays intact).

## Safety rules

- Never print the key, not even a prefix - label + length only.
- Never write the key to `src/config.json`, logs, memory, or a commit.
- One backup before every `.env` edit; rollback = restore backup + recreate.
- If the user pasted the key in chat, advise rotating it afterwards.
- `.env` must stay BOM-free; use `[System.IO.File]::WriteAllText` with
  `New-Object System.Text.UTF8Encoding($false)`.
- Do not delete the source key file unless the user explicitly asks.
