---
name: customproxy-connect-harness
description: >-
  The single skill responsible for connecting any coding harness to a running
  CustomProxy. Use when the user says "connect CustomProxy to opencode/kilo",
  "use CustomProxy in my editor", "wire up Claude Code/Cursor/Cline/Continue",
  or asks which base URL, key, and model ids to use. This is the ONLY skill
  that reads or edits harness configuration files; the add-* skills never
  touch them.
---


## Scope: this skill owns harness config, nothing else

CustomProxy itself is configured through `src/config.json` and `.env` by the
`customproxy-add-*` / `customproxy-remove` skills. **Those skills must never
edit a harness file, and this skill must never edit CustomProxy's files.**

If the harness lacks a provider or model, do not fix it here - send the user
to `customproxy-add-provider` / `customproxy-add-model` / `customproxy-add-api-key`,
then come back.

No code in this repository writes to a harness config. Every change below is
made by the agent, on the host, with the user's approval.

## Finding the deployment and its port

Resolve `<customproxy-dir>` at runtime - never assume a drive letter:

1. The folder two levels above this `skills/<skill-name>/` directory.
2. Otherwise ask the user where they cloned the repo.

Resolve the port, don't assume it. In order:

```bash
# 1. what compose actually published (run inside <customproxy-dir>)
docker compose port customproxy 3001    # e.g. 127.0.0.1:3001
# 2. fallback: the .env value
grep -E '^PROXY_HOST_PORT=' <customproxy-dir>/.env
# 3. fallback: the default
#    3001
```

`docker compose port` resolves the *service*, so it works regardless of what
the container is named. `docker port <name>` also works, but the name is
case-sensitive and depends on how the stack was created.

The port inside the container is always `3001`; only the published host port
varies. Use `<port>` everywhere below.

## Step 1 - Identify the harness

If the user did not say, ask which one they are connecting. Then confirm the
current config format from **that harness's own documentation** - formats
change, so verify rather than rely on memory.

| Harness | Config location | Shape |
|---|---|---|
| Kilo desktop CLI | `~/.config/kilo/kilo.jsonc` (`%USERPROFILE%\.config\kilo\kilo.jsonc`) | JSONC, `provider.<id>` blocks |
| Kilo Code (VS Code) | extension settings UI | OpenAI-compatible base URL + key + model |
| OpenCode | `~/.config/opencode/opencode.json` or project root | `provider.<id>` with `npm: "@ai-sdk/openai-compatible"` |
| Claude Code | environment variables | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` (Anthropic shape only) |
| Cursor / Cline / Continue | their OpenAI-compatible override fields | base URL + key |
| Anything else | ask the user for a sample config | OpenAI-compatible base URL + key |

## Step 2 - Verify the proxy is healthy first

Never wire a harness to a proxy that is not serving. Check, in this order:

```bash
curl -s http://127.0.0.1:<port>/health          # expect "status":"ok"
curl -s -o /dev/null -w '%{http_code}\n' \
     -H "Authorization: Bearer $PROXY_AUTH_TOKEN" \
     http://127.0.0.1:<port>/v1/models           # expect 200
```

If `/health` is not `ok`, stop and report the exact error. Do not edit a
harness to work around a broken proxy.

Read the two values you will wire in, without printing the secret:

```bash
# token: read from .env, never echo it
grep -E '^PROXY_AUTH_TOKEN=' <customproxy-dir>/.env
# catalog size (for the report)
curl -s -H "Authorization: Bearer $PROXY_AUTH_TOKEN" \
     http://127.0.0.1:<port>/v1/models | python3 -c \
     "import json,sys;print(len(json.load(sys.stdin)['data']))"
```

## Step 3 - Inspect the existing config (structure only)

Show the shape, never the secrets:

- provider ids present
- each provider's `baseURL` / `base_url`
- model count
- mask every key as `first4...last2`

```powershell
# JSONC: strip // and /* */ comments and trailing commas before parsing
```

Flag anything already pointing at CustomProxy with a **stale port**, a
**stale token**, or a **stale model list**.

## Step 4 - Present the plan, then wait for approval

Show a before/after table, then stop and wait. Editing harness config is a
user-visible change to their tooling.

| Item | Before | After |
|---|---|---|
| `provider.customProxy.options.baseURL` | *(current)* | `http://127.0.0.1:<port>/v1` |
| `provider.customProxy.options.apiKey` | *(current)* | this deployment's `PROXY_AUTH_TOKEN` |
| `provider.customProxy.models` | *(current)* | catalog from `/.well-known/opencode` |

Raise these before writing:

- **Port conflict** - if the harness already points at another
  OpenAI-compatible endpoint, ask whether to add CustomProxy as an extra
  provider (keeping the old one) or replace it. Never overwrite silently.
- **Key continuity** - confirm the token in the harness is the one in this
  deployment's `.env`; a mismatch shows up later as a 401.
- **JSONC comment loss** - a naive rewrite of `kilo.jsonc` drops comments.
  Warn, back up first, and use a comment-aware merge where possible.
- **UI-only harnesses** - hand the user the two values plus model ids to paste;
  never write into app-managed stores.

## Step 5 - Apply, with a backup

Back up the harness config before touching it:

```
<customproxy-dir>/Backups/harness_<name>_<YYYYmmdd_HHMMSS>/
```

Then write. Requirements for every write:

- valid syntax - re-parse the file afterwards
- UTF-8 **without BOM** (`UTF8Encoding($false)` in PowerShell)
- preserve unrelated providers and the user's own settings
- `apiKey` may reference an env var (`{env:PROXY_AUTH_TOKEN}`) where supported
  - prefer that over embedding the literal token

Reference blocks:

**OpenCode** (`opencode.json`)

```json
{
  "provider": {
    "customproxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CustomProxy",
      "options": {
        "baseURL": "http://127.0.0.1:<port>/v1",
        "apiKey": "{env:PROXY_AUTH_TOKEN}"
      },
      "models": { "<model-id>": { "name": "<label>" } }
    }
  },
  "model": "customproxy/<model-id>"
}
```

**Kilo / Kilo Code** (`kilo.jsonc` provider block)

```jsonc
{
  "provider": {
    "customProxy": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:<port>/v1",
        "apiKey": "{env:PROXY_AUTH_TOKEN}"
      }
      // "models": fetched from /.well-known/opencode, or left for the harness
    }
  }
}
```

**Anthropic-shaped tools** (Claude Code)

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:<port>
ANTHROPIC_AUTH_TOKEN=<PROXY_AUTH_TOKEN>
```

Fetch a ready-made model map:

```bash
curl -s http://127.0.0.1:<port>/.well-known/opencode -o catalog.json
# merge catalog.provider.customProxy.models into the harness block
```

## Step 6 - Prove it works end to end

Do all four; a config that parses is not a config that works.

1. **Proxy reachable** - `/health` is `ok`.
2. **Config valid** - the file parses; baseURL, apiKey reference, and model
   default are present.
3. **Direct call through the proxy** - minimal request so a failure is
   attributable to the proxy, not the harness:

```bash
curl -s -X POST http://127.0.0.1:<port>/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<provider>/<model-id>","messages":[{"role":"user","content":"hi"}],"max_tokens":8}'
```

4. **Through the harness** - run its own non-interactive command where it has
   one, or ask the user to send one message and confirm a reply.

## Failure table

| Symptom | Cause | Fix |
|---|---|---|
| 401 unauthorized | harness key ≠ `PROXY_AUTH_TOKEN` | copy the token from `.env` again |
| 404 unknown provider | model id missing `provider/` prefix | use `<provider>/<model>` from `/v1/models` |
| 403 model_filtered | model not whitelisted | `customproxy-add-model` |
| 503 no_keys | provider has no key | `customproxy-add-api-key` |
| 429 all_keys_cooling | every key rate-limited | wait, or add another key |
| connection refused | container down or wrong port | `docker compose ps`, `docker compose port customproxy 3001` |
| empty model list | no provider has a key yet | add a key, then `/refresh` |

## Step 7 - Report

- Harness + config path edited (or the values to paste, for UI-only tools).
- Base URL and port used, and how it was discovered.
- Model count wired in, and whether the harness fetches models itself.
- Backup directory of the pre-change config.
- What the user must restart (the app, a reload, `/models` in OpenCode).

## Safety rules

- Never print a key or token; masked only.
- Back up before every harness edit; rollback = restore + re-parse.
- Never edit CustomProxy's `src/config.json` or `.env` from this skill.
- Never point the harness at a deployment you have not checked with `/health`.
- Never write into an application-managed store you cannot restore.
- If the config format cannot be confirmed from docs, ask the user for a
  sample instead of guessing.
