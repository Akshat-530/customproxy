# Security Policy

## Design guarantees

- **Upstream keys never leave the deployment.** They are stored only in `.env`
  and injected into the container as environment variables; they are never
  written to `config.json`, never returned by any endpoint, and are scrubbed
  from logs and error messages by a secret index + pattern redaction.
- **Loopback only.** `compose.yml` publishes the proxy on `127.0.0.1`; it is not
  reachable from the network. Remote access should go through an SSH tunnel or
  a TLS reverse proxy you control — never a plain port-forward.
- **Authenticated API.** All `/v1/*`, `/logs` and refresh endpoints require the
  bearer token (`PROXY_AUTH_TOKEN`). Startup refuses to run without one unless
  `allow_no_auth: true` is set explicitly. Only `/health` (minimal body) and
  `/.well-known/opencode` are unauthenticated.
- **Least privilege runtime.** Non-root user, read-only root filesystem, all
  capabilities dropped except the three the entrypoint needs to fix mount
  ownership then drop privileges, `no-new-privileges`, `pids_limit`.
- **Bounded input.** `max_request_bytes` (32 MB default) and an optional local
  `rate_limit_per_minute`.

## Reporting a vulnerability

Please open a **private** security advisory on this repository (Security →
Report a vulnerability) or contact the maintainer directly. Do not post
exploitable details in a public issue.

Include: affected version/commit, a reproduction, impact, and any suggested
fix. Expect an acknowledgement within a few days.

## If a key leaks

1. Revoke/rotate the key at the provider.
2. Update `PROXY_AUTH_TOKEN` (if the proxy token leaked) in `.env`.
3. `docker compose up -d --force-recreate` (env changes need a recreate).
4. Check `logs/requests.jsonl` for unexpected use — entries carry the key
   *label*, never the key itself.
