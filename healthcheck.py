#!/usr/bin/env python3
"""Container healthcheck: proxy liveness + relay reachability.

The relays are optional — a deployment that uses no MCP tools starts none, and
a relay with no keys loaded still answers HTTP. So a relay only has to respond,
it is not required to report `ok: true`.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

TIMEOUT = float(os.environ.get("HEALTHCHECK_TIMEOUT", "5"))
PROXY_PORT = int(os.environ.get("PROXY_PORT", "3001"))

RELAY_PORTS = {
    "tavily": ("TAVILY_PROXY_PORT", 4016),
    "firecrawl": ("FIRECRAWL_PROXY_PORT", 4017),
    "gemini": ("GEMINI_PROXY_PORT", 4019),
    "tokenharbour": ("TOKENHARBOUR_PROXY_PORT", 4021),
    "context7": ("CONTEXT7_PROXY_PORT", 4022),
    "zro": ("ZRO_PROXY_PORT", 4030),
}


def _get(port: int) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}/health"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        raw = resp.read()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        return resp.status, body if isinstance(body, dict) else {}


def main() -> int:
    try:
        status, body = _get(PROXY_PORT)
    except Exception as exc:
        print(f"proxy :{PROXY_PORT} unreachable: {exc}")
        return 1

    if status != 200 or body.get("status") != "ok":
        print(f"proxy :{PROXY_PORT} unhealthy: status={status} body={body}")
        return 1

    services = [s.strip() for s in (os.environ.get("KEY_ROTATE_SERVICES") or "").split(",") if s.strip()]
    checked = 0
    for svc in services:
        entry = RELAY_PORTS.get(svc)
        if not entry:
            continue
        env_name, default_port = entry
        try:
            port = int(os.environ.get(env_name) or default_port)
        except ValueError:
            port = default_port
        try:
            _get(port)
        except Exception as exc:
            print(f"relay {svc} :{port} unreachable: {exc}")
            return 1
        checked += 1

    print(f"ok: proxy :{PROXY_PORT}, relays checked={checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
