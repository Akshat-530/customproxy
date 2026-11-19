# Contributing

Thanks for taking a look. This is a small, focused project — issues and PRs
that keep it that way are welcome.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

## Running the stack locally

```bash
cp .env.example .env             # then add your own keys
docker compose up -d --build
curl http://127.0.0.1:3001/health
```

The proxy binds to `127.0.0.1` only. Never expose it directly to the internet.

## Ground rules

- **Never commit secrets.** Keys live in `.env` (gitignored). `config.json`
  references env vars (`keys_env`), it never contains key material.
- **BOM-free UTF-8** for `.env` and `src/config.json` — a UTF-8 BOM breaks
  `json.load` on some runtimes.
- **Tests required** for behaviour changes; run `pytest` before opening a PR.
- **No new runtime host dependency** without discussion — the container is
  intentionally minimal.
- **Skills** (`skills/**/SKILL.md`) must stay path-independent: use
  `<customproxy-dir>` and discover the port at runtime.

## Commit / PR style

- Imperative subject line, ~72 chars (`add provider filter`, not `added`).
- Explain *why* in the body; link the issue if there is one.
- One logical change per commit.

## Reporting bugs

Include: what you ran, what you expected, what happened, the exact error text
from the response or `logs/requests.jsonl` (redact keys — they are scrubbed
automatically, but check before pasting), and `docker compose ps` status.

## Security issues

Do **not** open a public issue — see [SECURITY.md](SECURITY.md).
