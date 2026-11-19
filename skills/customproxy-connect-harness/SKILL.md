---
name: customproxy-connect-harness
description: >-
  Connect a coding harness (Kilo Code, Kilo desktop, OpenCode, Claude Code,
  Cursor, Cline, Continue, or any OpenAI-compatible tool) to the running
  CustomProxy so the user can chat with its models directly. Use when the
  user says "connect my harness to CustomProxy", "wire CustomProxy into
  opencode/kilo", "use CustomProxy in my editor", or asks which base URL/key
  to use. Researches the harness's config format, wires it up, and verifies.
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

# CustomProxy - Connect a Harness

Deployment: `<customproxy-dir>` - container `CustomProxy`,
port `127.0.0.1:<port>`, OpenAI-compatible on `/v1` and without the prefix.

**Never print** the harness's existing API keys or the proxy token.

---

## Step 0 - What CustomProxy exposes (constants)

| Value | Where it comes from |
|---|---|
| Base URL | `http://127.0.0.1:<port>/v1` (most OpenAI-compatible clients) |
| API key | `PROXY_AUTH_TOKEN` from `<customproxy-dir>\.env` |
| Model catalog | `GET http://127.0.0.1:<port>/v1/models` (token) - 347 models, `provider/upstream-id` format |
| Kilo/OpenCode-ready JSON | `GET http://127.0.0.1:<port>/.well-known/opencode` - a `{"provider":{"customProxy":{"models":{...}}}}` block, auto-updated |
| Note | Local-only by design: port binds to `127.0.0.1`, never exposed to the internet |

Read the token without printing it:

```powershell
Set-Location "<customproxy-dir>"
$tok = (Get-Content .env | Where-Object { $_ -match '^PROXY_AUTH_TOKEN=' }) -replace '^PROXY_AUTH_TOKEN=',''
"token length: $($tok.Length)"   # never the value
```

## Step 1 - Ask which harness

If not stated, ask: *"Which harness are you connecting? (Kilo Code VS Code,
Kilo desktop CLI, OpenCode, Claude Code, Cursor, Cline, Continue, other…)"*

Then research that harness's **current** config format with the harness's web
tools (formats drift - verify before editing):

- **OpenCode** → `opencode.json` (global: `~/.config/opencode/opencode.json`,
  or project root). Custom OpenAI-compatible block:
  `provider.<id>.npm = "@ai-sdk/openai-compatible"`, `options.baseURL`,
  `options.apiKey` (supports `{env:VAR}`), optional `models` map, default
  `model: "<provider-id>/<model-id>"`.
- **Kilo Code (VS Code)** → Settings → API Provider → **OpenAI Compatible**
  → Base URL + API Key + model picker (auto-fetches `/v1/models`). Or
  `kilo.jsonc` `customProvider` entries (`api`, `npm`,
  `@ai-sdk/openai-compatible`, `options.apiKey/baseURL`, `models` with
  `tool_call`/`limit`).
- **Kilo desktop CLI** → `~/.config/kilo/kilo.jsonc` (JSONC - comments and
  trailing commas allowed). This deployment already ships a compatible
  `/.well-known/opencode` document.
- **Claude Code** → env vars (`ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`)
  for Anthropic-shape traffic; for OpenAI-shape tools use whatever client the
  user runs - confirm with docs first.
- **Cursor / Cline / Continue** → their OpenAI-compatible override fields.

## Step 2 - Inspect the harness's current config (structure only)

Find the config file, then show **structure only** - provider names, base
URLs, model counts. Mask any key value (`first4...last2`). Example JSONC
inspection for Kilo:

```powershell
# JSONC is JSON with comments - strip comments/trailing commas before parsing
```

Report what you find, e.g.:

- `providers: customProxy, kilo, kiloGateway`
- `customProxy.baseURL = http://127.0.0.1:3001/v1` (old proxy?) → flag it
- `customProxy.models: 32` → stale catalog?

## Step 3 - Present the wiring plan (wait for approval)

Show what will change before touching anything. Typical plan for Kilo:

| Item | Before | After |
|---|---|---|
| `provider.customProxy.options.baseURL` | old value | `http://127.0.0.1:<port>/v1` |
| `provider.customProxy.options.apiKey` | old token | CustomProxy `PROXY_AUTH_TOKEN` (same `hhSK…` value already in use) |
| `provider.customProxy.models` | 32 entries | 347 entries from `/.well-known/opencode` |

For OpenCode:

```json
{
  "provider": {
    "customproxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CustomProxy",
      "options": { "baseURL": "http://127.0.0.1:<port>/v1", "apiKey": "{env:PROXY_AUTH_TOKEN}" },
      "models": { "<model-id>": { "name": "<label>" } }
    }
  },
  "model": "customproxy/<model-id>"
}
```

Warn when relevant:
- **Port conflict:** if the harness already points at another OpenAI-compatible
  endpoint, confirm which one should serve requests. Ask: keep both (add a
  second provider block) or replace?
- **Key continuity:** confirm the token in the harness matches
  `PROXY_AUTH_TOKEN` from this deployment's `.env`.
- **Windows JSONC safety:** editing `kilo.jsonc` strips comments unless the
  tool is comment-aware. Prefer preserving comments; if a rewrite would lose
  them, say so and back up first.
- If a harness only accepts a base URL + key in its UI (VS Code extension),
  give the user the exact two values to paste and the model ids to select -
  do not edit extension-managed files blindly.

## Step 4 - Wire it up (on approval)

Preferred: **edit the config file** on the host.

- Kilo CLI (`~/.config/kilo/kilo.jsonc`): merge the new/updated
  `provider.customProxy` block - keep user comments where possible, atomic
  write, backup first. Simplest robust path: update `options.baseURL` +
  `options.apiKey`, and **replace** the `models` object with the fetched
  catalog (or leave models to the harness if it fetches them itself).
- OpenCode (`~/.config/opencode/opencode.json`): add/replace the
  `customproxy` provider block and the default `model` (ask which model).
- UI-only harnesses: print the exact values for the user to paste
  (base URL, key, model ids) - never write into app-managed stores.

Fetch the fresh catalog for the models block:

```powershell
Invoke-RestMethod "http://127.0.0.1:<port>/.well-known/opencode" -OutFile "$env:TEMP\cp-catalog.json"
# merge $catalog.provider.customProxy.models into the harness config
```

Before every write: back up the harness config to
`<customproxy-dir>\Backups\harness_<name>_<stamp>\`. After writing:
re-parse the file to prove valid syntax, and check it is BOM-free.

## Step 5 - Verify the harness actually works

1. **Proxy side (no harness needed):**
   `GET /health` → `status=ok`; `GET /v1/models` → model count.
2. **Config side:** the harness config parses and contains the right
   `baseURL`, `apiKey` reference, and models.
3. **End-to-end chat:** run the harness's own CLI non-interactively
   (`opencode run "say hi"` style) or ask the user to send one chat message
   and confirm a reply. If the harness logs errors, quote them exactly.
4. **Model reachability:** pick the user's intended model and send a minimal
   request via the proxy directly first (8 tokens) so failures are attributed
   to config, not the model.

Common failure → meaning table:

| Symptom | Likely cause |
|---|---|
| 401 unauthorized | harness key ≠ `PROXY_AUTH_TOKEN` |
| 404 unknown provider | model id missing the `provider/` prefix |
| connection refused | proxy container down (`docker ps`) or wrong port |
| model returns 403 model_filtered | model not whitelisted (add-model skill) |
| 402 / 429 | billing / rate limit on the upstream provider |

## Step 6 - Report

- Harness + config file path edited (or UI values to paste).
- Base URL + which key variable, model default set.
- Catalog size wired in (e.g. 347 models) and where it auto-refreshes.
- Backup location of the pre-change harness config.
- Any follow-ups (restart the harness/app, `/models` command in OpenCode,
  reload VS Code).

## Safety rules

- Never print keys or tokens; masked only.
- Never point a harness at a different deployment without explicit approval
  - confirm which base URL should serve requests first.
- Back up before every harness config edit; rollback = restore + reparse.
- JSONC: preserve comments where possible; if the write loses them, say so.
- BOM-free writes only (`UTF8Encoding($false)`).
- If the harness's config format can't be confirmed from docs, say so and ask
  the user for a sample config rather than guessing.
