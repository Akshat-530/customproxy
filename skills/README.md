# CustomProxy skills

Five skills that let an LLM agent (Kilo, Kilo Code, OpenCode, Claude Code,
Cursor, custom agents) extend and connect a running CustomProxy deployment.
Each is a standalone `SKILL.md`; they chain:

```
add-provider  ->  add-api-key  ->  add-model  ->  connect-harness  ->  E2E test
      \_______________ any of these can be undone via customproxy-remove ______/
```

| Skill | Trigger examples | Writes |
|---|---|---|
| [`customproxy-add-provider`](customproxy-add-provider/SKILL.md) | "add provider together.ai", "support api.x.ai" | `src/config.json` provider block, `XY_API_KEYS=` in `.env`, `.env.example` |
| [`customproxy-add-api-key`](customproxy-add-api-key/SKILL.md) | "add this API key", "rotate this key in" | `XY_API_KEYS=label=key` in `.env` (backed up) |
| [`customproxy-add-model`](customproxy-add-model/SKILL.md) | "add this \<model\> by \<provider\>" | `models` / `include_models` / `exclude_models` / `max_models` / `manual_models` |
| [`customproxy-connect-harness`](customproxy-connect-harness/SKILL.md) | "connect CustomProxy to opencode/kilo", "use it in my editor" | **only** skill allowed to read/edit a harness config (`kilo.jsonc`, `opencode.json`, or UI values) |
| [`customproxy-remove`](customproxy-remove/SKILL.md) | "remove \<model\>", "drop provider X", "delete this key" | removes from filters / provider block / `.env` pool (backed up) |

## Ownership: who may write what

The skills divide cleanly so two of them can never fight over the same file:

| Target | Only this skill may edit it |
|---|---|
| `src/config.json` (providers, filters) | `add-provider`, `add-model`, `remove` |
| `.env` / `.env.example` (keys, token, ports) | `add-api-key`, `add-provider`, `remove` |
| Generated catalog + logs | **no skill** - the server writes these itself |
| A harness config (`kilo.jsonc`, `opencode.json`, UI settings) | **`customproxy-connect-harness` only** |
| Anything else in the repo | no skill writes it |

`add-provider` / `add-api-key` / `add-model` therefore end by *pointing* the
user at `customproxy-connect-harness` if their harness needs wiring; they
never touch a harness file themselves.

## Shared contract (all five skills)

- **Deployment:** wherever you cloned this repo. Skills refer to it as
  `<customproxy-dir>` and resolve it at runtime — never a fixed drive letter
  or user path.
- **Port:** published by `compose.yml` as `${PROXY_HOST_PORT:-3001}`. Discover
  it with `docker compose port customproxy 3001` rather than assuming.
- **Container name:** `customproxy`.
- **Secrets:** keys/tokens only in `.env` (gitignored); never printed, never
  in `config.json`, logs, or memory — masked as `first4...last2`.
- **Writes:** BOM-free UTF-8; back up before editing; rollback = restore +
  recreate.
- **Deploy:** `docker compose up -d --force-recreate` (env_file is read at
  container creation; `restart` does not reload it).
- **Verify:** `/health` (providers, key labels, catalog age), `/v1/models`
  (catalog + metadata), `/logs` (outcome, `api_key_id`, latency, usage, exact
  upstream error), `/.well-known/opencode` (harness-ready catalog).
- **Whitelist:** `models` (exact/glob, empty = all), `include_models`,
  `exclude_models`, `max_models`, `manual_models`. Enforced on the catalog
  **and** on direct calls (`403 model_filtered`).
- **Removal is first-class:** every add has a documented undo in
  `customproxy-remove`, including the shared-pool caveat (some key pools also
  feed the rotation relays).

## Install into a harness

Copy each skill folder into that harness's skills directory, keeping the
`SKILL.md` name and frontmatter:

| Harness | Skills directory |
|---|---|
| Kilo | `~/.config/kilo/skills/` (`%USERPROFILE%\.config\kilo\skills\` on Windows) |
| Kilo Code (VS Code) | the extension's configured skills folder |
| Claude Code | `~/.claude/skills/` |
| Prompt-only harness | point the agent at the skill file paths and tell it to follow the step order |

## Connecting a harness to use CustomProxy

Run `customproxy-connect-harness`. Quick reference:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:<port>/v1` (default port `3001`) |
| API key | `PROXY_AUTH_TOKEN` from `.env` |
| Model ids | `<provider>/<model-id>` (see `/v1/models`) |
| Catalog (auto-updating) | `http://127.0.0.1:<port>/.well-known/opencode` |

## Free models need no setup

`kilo`, `nvidia`, and `openrouter` ship with `filter_free: true`, so the proxy
discovers and lists **only free models** for them on every catalog refresh.
There is no sync script and no static list to maintain: add one key and the
free catalog appears.

For any other provider that exposes free models, set `filter_free: true` on
its block (that is what `customproxy-add-provider` writes when you ask for a
free-only provider). Paid models are then dropped from the catalog *and*
refused on direct calls; models with no pricing data are kept, because they
cannot be proven paid.
