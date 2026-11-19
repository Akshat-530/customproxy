---
name: Bug report
about: Something in CustomProxy does not behave as documented
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- Observed behaviour. Include the exact error code returned by the proxy. -->

## What you expected

<!-- Expected behaviour. -->

## Steps to reproduce

1.
2.
3.

## Environment

- CustomProxy version / commit:
- Deployment: Docker Compose / bare Python
- Python version (if not Docker):
- Client: Kilo / OpenCode / OpenAI SDK / curl / other

## Request details

```
model:      provider/upstream-model-id
endpoint:   /v1/chat/completions
status:     e.g. 402 billing_required
x-request-id:
```

## Logs

<!-- Relevant lines from `docker compose logs customproxy` and the matching
     entry from `GET /logs` or `logs/requests.jsonl`.

     REDACT: never paste real API keys. Replace them with `sk-REDACTED`. -->

```
```

## Config

<!-- The provider block from src/config.json, with keys removed. Never paste `.env`. -->

```json
```
