# CustomProxy skills

Five skills that let any LLM (Kilo, Kilo Code, OpenCode, Claude Code, Cursor,
custom agents) extend and connect a running CustomProxy deployment. Each is a
standalone `SKILL.md`; they chain:

```
add-provider  ->  add-api-key  ->  add-model  ->  connect-harness  ->  E2E test
      \_______________ any of these can be undone via customproxy-remove ______/
```

| Skill | Trigger examples | Writes |
|---|---|---|
| [`customproxy-add-provider`](customproxy-add-provider/SKILL.md) | "add provider together.ai", "support api.x.ai" | `src/config.json` provider block, `XY_API_KEYS=` in `.env`, `.env.example` |
| [`customproxy-add-api-key`](customproxy-add-api-key/SKILL.md) | "add this API key", "rotate this key in" | `XY_API_KEYS=label=key` in `.env` (backed up) |
| [`customproxy-add-model`](customproxy-add-model/SKILL.md) | "add this \<model\> by \<provider\>" | `models` / `include_models` / `exclude_models` / `max_models` / `manual_models` |
| [`customproxy-connect-harness`](customproxy-connect-harness/SKILL.md) | "connect CustomProxy to opencode/kilo", "use it in my editor" | harness config (`kilo.jsonc`, `opencode.json`, or UI values) |
| [`customproxy-remove`](customproxy-remove/SKILL.md) | "remove \<model\>", "drop provider X", "delete this key" | removes from filters / provider block / `.env` pool (backed up) |

## Shared contract (all five skills)

- **Deployment:** `D:\My files\Docker\work\customproxy`, container
  `CustomProxy` on `127.0.0.1:3101`.
- **Never touch** `D:\My files\Docker\work\model-providers` or the
  `Model_Providers` container.
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

- **Kilo:** copy each folder to `C:\Users\Admin\.config\kilo\skills\`
  (done in this repo's setup).
- **Kilo Code (VS Code), Claude Code, other SKILL.md harnesses:** copy the
  five folders into that harness's skills directory, keeping the `SKILL.md`
  name and frontmatter.
- **Prompt-only harnesses:** point the agent at the skill file paths and tell
  it to follow the step order.

## Connecting a harness to use CustomProxy

Run `customproxy-connect-harness`. Quick reference:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:3101/v1` |
| API key | `PROXY_AUTH_TOKEN` from `customproxy/.env` |
| Model ids | `<provider>/<model-id>` (347 available) |
| Catalog (auto-updating) | `http://127.0.0.1:3101/.well-known/opencode` |
