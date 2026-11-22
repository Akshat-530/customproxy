# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-19

First public release.

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
- **Tests + CI** — 46 tests (unit + end-to-end against a mocked upstream);
  GitHub Actions workflow compiles `src`, `key_rotate_proxy.py` and
  `healthcheck.py`, then runs pytest on Python 3.12.

### Fixed
- `filter_free` dropped paid models from the catalog but still forwarded a
  direct call to them upstream, because the dropped ids were never recorded and
  so had no metadata to refuse. Paid ids are now tracked per provider and
  refused with `403 model_filtered`; `manual_models` still bypass the filter.
- Under `filter_free`, a model the upstream never listed (a typo, or a paid id
  the filter had dropped) was passed through and failed upstream after spending
  a key. Such ids are now refused locally with `403 model_filtered`.
- Error classification let message text override the status code, so a `402`
  whose body mentioned `max_tokens` was filed as `context_length` and rotation
  stopped when a fresh key was what was needed. Status codes now win, and
  rotation is derived from the same kind the client sees.
- Client-side faults (`404`, `405`, `409`, `413`, `415`, `422`) were treated as
  `upstream_error` and rotated every key, although no key can change the
  outcome. They now map to `invalid_request` and stop.
- An exhausted balance reported as `403 "Insufficient user quota"` was labelled
  `forbidden`; a billing phrase now outranks the bare `forbidden` reading.
- Models with no pricing data were published with pricing `"0"`, claiming they
  were free. Unknown pricing is now empty and `free` stays `null`.
- Declared `models` that the upstream omitted still passed `allows()` but were
  missing from the catalog, so a model could be callable yet invisible. The
  catalog and the access check now agree.
- `max_models` only trimmed the list; the capped-out ids stayed callable. They
  are now refused on direct calls as well.
- An empty catalog left the previous file on disk, so a withdrawn model kept
  being advertised. The stale file is now removed.
- A failed `chmod` after the atomic catalog replace aborted the write and
  retried every refresh; it is now logged and ignored.
- `X-Forwarded-For` was believed from any caller, allowing rate-limit
  identities to be spoofed. It is honoured only for peers in `trusted_proxies`
  (`TRUSTED_PROXIES` env overrides), and ignored otherwise.
- Auth failures returned `{"detail": ...}` instead of the OpenAI-shaped
  `{"error": {...}}` body; they now carry `message`, `type`, `code` and
  `request_id` like every other error.
- Upstream paths are validated before a request is built: `..` segments,
  absolute URLs and protocol-relative paths are rejected with
  `400 invalid_request` (`invalid_path`), closing a path-traversal gap.

### Changed
- `customproxy-connect-harness` is the only skill that reads or edits a harness
  configuration. `customproxy-add-provider`, `-add-api-key` and `-add-model`
  point at it instead of touching harness files themselves.
