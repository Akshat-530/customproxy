# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- `filter_free` dropped paid models from the catalog but still forwarded a
  direct call to them upstream, because the dropped ids were never recorded and
  so had no metadata to refuse. Paid ids are now tracked per provider and
  refused with `403 model_filtered`; `manual_models` still bypass the filter.

### Changed
- `customproxy-connect-harness` is the only skill that reads or edits a harness
  configuration. `customproxy-add-provider`, `-add-api-key` and `-add-model`
  point at it instead of touching harness files themselves.

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
- **Free-model auto-discovery** — `filter_free` providers expose *only* free
  models, discovered live from each provider's `/models` on every refresh. No
  sync script and no static list. Enabled by default for `kilo`, `nvidia` and
  `openrouter`; opt in per provider anywhere else. Models with no pricing data
  are kept, since they cannot be proven paid.
- **Tests + CI** — 35 tests (unit + end-to-end against a mocked upstream);
  GitHub Actions workflow compiles `src`, `key_rotate_proxy.py` and
  `healthcheck.py`, then runs pytest on Python 3.12.
