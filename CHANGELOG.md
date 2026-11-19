# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-19

### Added
- **CustomProxy** — a single OpenAI-compatible endpoint in front of many
  providers, with per-provider API-key rotation and cooldowns.
- **Dynamic catalog** — full model discovery from each provider's `/models`,
  refreshed on a timer, written to a catalog file and served live at
  `/v1/models` and `/.well-known/opencode`.
- **Per-model metadata** — context window, max output, pricing, capabilities
  (`tool_call`, `reasoning`, `vision`, `audio`, `embedding`), modalities,
  category and `free` flag, merged from upstream → catalog → defaults.
- **Model controls** — `models`, `include_models`, `exclude_models`,
  `max_models`, `manual_models`, `filter_free`; enforced on the catalog and on
  direct calls (`403 model_filtered`).
- **Provider controls** — `provider_filters` (include/exclude) and per-provider
  `enabled`.
- **Thinking control** — `reasoning_auto`, `reasoning_default`,
  `reasoning_levels`, applied only to reasoning-capable models.
- **Structured request log** — one JSONL entry per request: outcome, status,
  latency, key label, attempts, token usage, and the exact upstream error;
  served via `/logs`.
- **Exact error surfacing** — billing → `402 billing_required`, auth → `401`,
  rate limit → `429` (+ `Retry-After`), cooling → `429 all_keys_cooling`,
  context → `400`; never a generic "keys exhausted".
- **Secret safety** — keys only in `.env`; responses and logs are scrubbed of
  any configured key (secret index + pattern redaction).
- **Five agent skills** — `customproxy-add-provider`, `customproxy-add-api-key`,
  `customproxy-add-model`, `customproxy-connect-harness`,
  `customproxy-remove`; portable (no hardcoded paths, runtime discovery).
- **Hardened runtime** — non-root user, read-only root filesystem, dropped
  capabilities, `no-new-privileges`, tmpfs `/tmp`, health-checked relays.
- **Tests + CI** — 18 unit tests; GitHub Actions workflow compiles and tests on
  Python 3.12.
