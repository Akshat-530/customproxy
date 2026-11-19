#!/usr/bin/env python3
"""CustomProxy - lightweight multi-provider / multi-key rotation router.
Runs inside the CustomProxy Docker container. OpenAI-compatible on /v1/*.
Smooth rotation: 429/401/402/403/5xx -> next key, no cooldown timers."""

from __future__ import annotations
import asyncio, fnmatch, hmac, json, logging, os, re, tempfile, time, uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("proxy")
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH, encoding="utf-8") as f: CFG = json.load(f)

def _base_dir() -> Path:
    """Root for relative runtime paths.

    Works in both supported layouts without hardcoded absolute paths:
      repo checkout   <repo>/src/server.py      -> <repo>
      container       /app/customproxy/server.py -> /app/customproxy
    CUSTOMPROXY_HOME overrides both.
    """
    env = os.environ.get("CUSTOMPROXY_HOME")
    if env: return Path(env).expanduser().resolve()
    here = CONFIG_PATH.resolve().parent
    return here.parent if here.name == "src" else here

BASE_DIR = _base_dir()

def _resolve(value: str | None, default: str) -> Path:
    """Absolute paths are used verbatim; relative ones live under BASE_DIR."""
    p = Path(value or default).expanduser()
    return p if p.is_absolute() else (BASE_DIR / p)
PORT = int(os.environ.get("PROXY_PORT", CFG.get("proxy_port", 3001)))
AUTH_TOKEN = os.environ.get("PROXY_AUTH_TOKEN") or CFG.get("auth_token") or None
DEBUG_ENABLED = CFG.get("debug_enabled", True)
REQUEST_BODY_LOG = bool(CFG.get("request_body_log_enabled", False))
DEBUG_MAX_EVENTS = int(CFG.get("debug_max_events", 500))
REQUEST_LOG_MAX = int(CFG.get("request_log_max_bytes", 32 * 1024 * 1024))
MAX_REQUEST_BYTES = int(CFG.get("max_request_bytes", 32 * 1024 * 1024))
RATE_LIMIT_PER_MIN = int(CFG.get("rate_limit_per_minute", 0))
CORS_ORIGINS = CFG.get("cors_origins") or ["*"]

def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None: return default
    return v.strip().lower() in ("1", "true", "yes", "on")
KILO_CONFIG_PATH = _resolve(os.environ.get("KILO_CONFIG_PATH") or CFG.get("kilo_config_path"), "state/kilo_models.json")
client: Optional[httpx.AsyncClient] = None

def _request_token(request: Request) -> str:
    hdr = request.headers.get("authorization", "")
    if hdr.lower().startswith("bearer "):
        return hdr[7:].strip()
    # Azure/Anthropic-style clients use x-api-key / api-key.
    return (request.headers.get("x-api-key") or request.headers.get("api-key") or "").strip()

def _check_auth(request: Request):
    if not AUTH_TOKEN: return
    if not hmac.compare_digest(_request_token(request), AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")

class DebugLog:
    def __init__(self):
        self.events: deque = deque(maxlen=DEBUG_MAX_EVENTS)
        self.counts: dict[str, int] = {}
        self._seq = 0
    def record(self, event: str, **fields):
        if not DEBUG_ENABLED: return
        try:
            self._seq += 1
            e = {"ts": round(time.time(),3), "seq": self._seq, "event": event}; e.update(fields)
            self.events.append(e); self.counts[event] = self.counts.get(event,0)+1
        except Exception: pass
    def clear(self): self.events.clear(); self.counts = {}
    def summary(self): return {"events_by_type": dict(self.counts), "in_memory": len(self.events)}
debug_log = DebugLog()

class RequestLog:
    """One structured entry per model request: outcome, status, latency, key,
    upstream model, token usage and the exact upstream error message."""
    def __init__(self):
        self.entries: deque = deque(maxlen=int(CFG.get("request_log_entries", 2000)))
        self.enabled = bool(CFG.get("request_log_enabled", True))
        self.path = _resolve(os.environ.get("REQUEST_LOG_JSONL") or CFG.get("request_log_jsonl"), "logs/requests.jsonl")
        self.max_bytes = int(CFG.get("request_log_jsonl_max_bytes", 64 * 1024 * 1024))
    def add(self, **fields):
        if not self.enabled: return
        e = {"ts": round(time.time(), 3), **fields}
        self.entries.append(e)
        line = json.dumps(e, ensure_ascii=False) + "\n"
        # Prefer the configured dir; fall back to the system temp dir if it is
        # not writable (e.g. a bind mount owned by another uid).
        for p in (self.path, Path(tempfile.gettempdir()) / "customproxy_requests.jsonl"):
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.exists() and p.stat().st_size > self.max_bytes:
                    p.write_bytes(b"")
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write(line)
                return
            except Exception:
                continue
    def tail(self, n: int = 100) -> list[dict]:
        return list(self.entries)[-n:]

request_log = RequestLog()

class RateLimiter:
    """Simple per-client sliding-window limiter (0 = disabled)."""
    def __init__(self, per_minute: int):
        self.per_minute = max(0, int(per_minute or 0))
        self.hits: dict[str, deque] = {}
    def allow(self, client: str) -> bool:
        if not self.per_minute: return True
        now = time.time(); window = now - 60
        if len(self.hits) > 10000: self.hits.clear()
        dq = self.hits.setdefault(client or "unknown", deque())
        while dq and dq[0] < window: dq.popleft()
        if len(dq) >= self.per_minute: return False
        dq.append(now); return True

rate_limiter = RateLimiter(RATE_LIMIT_PER_MIN)

# Provider-level filtering (same glob/regex rules as models):
#   "provider_filters": {"include": ["tokenharbor", "re:^nv"], "exclude": ["*legacy*"]}
PROVIDER_FILTERS = CFG.get("provider_filters") or {}

def _provider_enabled(name: str) -> bool:
    inc = PROVIDER_FILTERS.get("include") or []
    exc = PROVIDER_FILTERS.get("exclude") or []
    if inc and not _matches(name, inc): return False
    if exc and _matches(name, exc): return False
    return True

def _load_custom_providers() -> None:
    """Merge providers defined via env (CUSTOM_PROVIDERS_JSON) into the config.

    Each item is a normal provider block plus a "name" field, e.g.
      [{"name":"myprovider","base_url":"https://api.example.com/v1",
        "keys_env":"MYPROVIDER_API_KEYS","models":[]}]
    so users can add providers without editing config.json.
    """
    raw = os.environ.get("CUSTOM_PROVIDERS_JSON")
    if not raw: return
    try: arr = json.loads(raw)
    except Exception:
        log.error("CUSTOM_PROVIDERS_JSON is not valid JSON; ignoring"); return
    if not isinstance(arr, list): return
    for e in arr:
        if not (isinstance(e, dict) and e.get("name") and e.get("base_url")): continue
        name = str(e["name"]).strip()
        cfg = dict(e); cfg.pop("name", None)
        if name in CFG.get("providers", {}):
            log.warning("custom provider %r already exists in config; env entry ignored", name)
            continue
        CFG.setdefault("providers", {})[name] = cfg
        log.info("custom provider loaded from env: %s -> %s", name, cfg.get("base_url"))

def _client_id(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff: return xff
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"

_BILLING_HINTS = ("billing", "payment required", "insufficient credit", "insufficient quota",
                  "insufficient balance", "out of credits", "no credits", "credit balance",
                  "balance", "top up", "top-up", "exceeded your current quota")
_AUTH_HINTS = ("invalid api key", "incorrect api key", "invalid auth key", "api key not valid",
               "unauthenticated", "authentication", "unauthorized", "api_key_invalid")
_RATE_HINTS = ("rate limit", "too many requests", "requests per", "rpm ", "tpm ", "concurrency")
_CONTEXT_HINTS = ("context_length_exceeded", "context length", "maximum context",
                  "max_tokens", "too many tokens", "token limit")

def _extract_error_message(raw: bytes) -> str:
    if not raw: return ""
    try:
        d = json.loads(raw.decode("utf-8", "ignore"))
        if isinstance(d, dict):
            err = d.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("detail") or err.get("code") or json.dumps(err))
            if isinstance(err, str): return err
            for k in ("message", "detail", "error_description"):
                if d.get(k): return str(d[k])
    except Exception:
        pass
    return raw.decode("utf-8", "ignore").strip()[:2000]

def _classify_failure(status: int | None, message: str) -> tuple[str, int]:
    low = (message or "").lower()
    if any(h in low for h in _CONTEXT_HINTS): return "context_length", status or 400
    if status == 402 or any(h in low for h in _BILLING_HINTS): return "billing_required", 402
    if status == 401 or any(h in low for h in _AUTH_HINTS): return "auth_error", 401 if status in (None, 401) else status
    if status == 429 or any(h in low for h in _RATE_HINTS): return "rate_limited", 429
    if status == 403: return "forbidden", 403
    if status is not None and status >= 500: return "upstream_error", 502
    if status == 400: return "invalid_request", 400
    return "upstream_error", status or 502

def _error_response(message: str, kind: str, status: int, request_id: str, **extra) -> JSONResponse:
    err = {"message": _redact(message), "type": kind, "code": kind, "request_id": request_id}
    err.update({k: _redact_obj(v) for k, v in extra.items() if v is not None})
    headers = {"x-request-id": request_id}
    ra = extra.get("retry_after")
    if isinstance(ra, (int, float)):
        headers["Retry-After"] = str(int(ra))
    return JSONResponse({"error": err}, status_code=status, headers=headers)

_SECRETS: set[str] = set()

def _index_secrets() -> None:
    """Remember every configured key value so it can be scrubbed from any
    response or log line, whatever its shape."""
    try:
        for pr in providers.values():
            for k in pr.pool.keys:
                v = str(k.get("key") or "")
                if len(v) >= 8: _SECRETS.add(v)
    except Exception:
        pass
    for pcfg in (CFG.get("providers") or {}).values():
        for k in (pcfg.get("keys") or []):
            v = str((k or {}).get("key") or "")
            if len(v) >= 8: _SECRETS.add(v)


_SECRET_RE = re.compile(r"(sk-|thk_|nvapi-|ctx7sk-|hms-|AQ\.)[A-Za-z0-9_\-.]{8,}")

def _redact_obj(v):
    """Recursively redact credential-shaped strings in JSON-ish values."""
    if isinstance(v, str): return _redact(v)
    if isinstance(v, list): return [_redact_obj(x) for x in v]
    if isinstance(v, dict): return {k: _redact_obj(x) for k, x in v.items()}
    return v


def _redact(text) -> str:
    s = str(text or "")
    for sec in _SECRETS:
        if sec and sec in s:
            s = s.replace(sec, "***")
    return _SECRET_RE.sub(lambda m: m.group(1) + "***", s)[:2000]

def _usage_of(raw: bytes) -> dict:
    try:
        u = (json.loads(raw) or {}).get("usage") or {}
        return {k: u[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens") if u.get(k) is not None}
    except Exception:
        return {}

def _log_request(request_id: str, outcome: str, request: Request, path: str,
                 model_id: str | None, t0: float, **extra) -> None:
    entry = {"request_id": request_id, "outcome": outcome, "method": request.method,
             "path": path, "model": model_id,
             "duration_ms": round((time.perf_counter() - t0) * 1000, 1)}
    entry.update(extra)
    if entry.get("key") is not None and "api_key_id" not in entry:
        entry["api_key_id"] = entry["key"]   # label only, never the secret
    if isinstance(entry.get("error"), str):
        entry["error"] = _redact(entry["error"])
    request_log.add(**entry)

class KeyPool:
    def __init__(self, keys: list[dict]):
        self.keys = []
        seen: dict[str, int] = {}
        for k in keys:
            if not k.get("key"): continue
            label = str(k.get("label") or k["key"][:12])
            n = seen.get(label, 0); seen[label] = n + 1
            if n: label = f"{label}#{n + 1}"
            self.keys.append({"key":k["key"],"label":label})
        self.requests = {k["label"]:0 for k in self.keys}
        self.errors = {k["label"]:0 for k in self.keys}
        self.last_status = {k["label"]:None for k in self.keys}
        self.cooldown_until = {k["label"]:0.0 for k in self.keys}
        self.fail_streak = {k["label"]:0 for k in self.keys}
        self._idx = 0
    def peek_order(self) -> list[dict]:
        n=len(self.keys)
        if n==0: return []
        s=self._idx%n
        return self.keys[s:]+self.keys[:s]
    def order(self) -> list[dict]:
        n=len(self.keys)
        if n==0: return []
        s=self._idx%n; self._idx=(s+1)%n; return self.keys[s:]+self.keys[:s]
    def available(self) -> list[dict]:
        now = time.time()
        return [k for k in self.order() if now >= self.cooldown_until.get(k["label"], 0)]
    def set_cooldown(self, label: str, seconds: float):
        self.cooldown_until[label] = time.time() + seconds
    def clear_cooldown(self, label: str):
        self.cooldown_until[label] = 0.0
    def note_success(self, label: str):
        self.fail_streak[label] = 0
        self.cooldown_until[label] = 0.0
    def note_failure(self, label: str, status: int | None = None, retry_after: float | None = None):
        """Escalating cooldown: repeated failures sideline a key for longer,
        capped at 15 minutes, then it gets retried automatically."""
        n = self.fail_streak.get(label, 0) + 1
        self.fail_streak[label] = n
        if status == 429:
            base = max(1.0, min(float(retry_after or 3.0), 60.0))
        elif status in (401, 402, 403):
            base = 60.0
        elif status is not None and status >= 500:
            base = 15.0
        else:
            base = 10.0
        self.set_cooldown(label, min(base * (2 ** (n - 1)), 900.0))
    def is_cooling(self, label: str) -> bool:
        return time.time() < self.cooldown_until.get(label, 0)
    def stats(self) -> list[dict]:
        return [{"label":k["label"],"requests":self.requests[k["label"]],"errors":self.errors[k["label"]],"last_status":self.last_status[k["label"]],"fail_streak":self.fail_streak[k["label"]],"cooldown":max(0,int(self.cooldown_until.get(k["label"],0)-time.time()))} for k in self.keys]

_THINKING_LEVELS = ("low", "medium", "high")

def _zero_price(v) -> bool:
    try: return float(v) == 0.0
    except Exception: return False

def _is_free_model(m: dict):
    """True/False when the payload says so, None when it cannot be known."""
    if m.get("isFree") is True: return True
    pr = m.get("pricing")
    if isinstance(pr, dict) and pr:
        vals = [pr.get(k) for k in ("prompt", "completion", "request") if pr.get(k) is not None]
        if len(vals) >= 2:
            return all(_zero_price(v) for v in vals)
    return None

def _as_list(v) -> list:
    """Coerce a config value to a list of strings.

    Guards against a single filter written as a bare string
    ("exclude_models": "foo"), which would otherwise iterate characters.
    """
    if v is None: return []
    if isinstance(v, str): return [v] if v.strip() else []
    if isinstance(v, (list, tuple)): return [str(x) for x in v if str(x).strip()]
    return [str(v)]


def _matches(mid: str, patterns) -> bool:
    """Glob match (case-insensitive) or 're:<regex>' pattern."""
    for pat in patterns or []:
        p = str(pat).strip()
        if not p: continue
        if p.startswith("re:"):
            try:
                if re.search(p[3:], mid): return True
            except re.error:
                continue
        elif fnmatch.fnmatchcase(mid.lower(), p.lower()):
            return True
    return False

def _split_mods(v) -> list[str]:
    if not v: return []
    if isinstance(v, str):
        parts = re.split(r"[+/,]", v)
    elif isinstance(v, (list, tuple)):
        parts = [str(x) for x in v]
    else:
        parts = [str(v)]
    return [p.strip().lower() for p in parts if p and p.strip()]

def _modalities(m: dict) -> dict:
    arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
    ins = _split_mods(arch.get("input_modalities") or arch.get("input") or m.get("input_modalities"))
    outs = _split_mods(arch.get("output_modalities") or arch.get("output") or m.get("output_modalities"))
    mod = arch.get("modality") or m.get("modality")
    if isinstance(mod, str) and "->" in mod:
        a, b = mod.split("->", 1)
        ins = ins or _split_mods(a); outs = outs or _split_mods(b)
    return {"input": ins or ["text"], "output": outs or ["text"]}

def _upstream_meta(m: dict) -> dict:
    """Normalize the optional metadata an upstream /models entry may expose."""
    arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
    top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
    lim = m.get("limits") if isinstance(m.get("limits"), dict) else {}
    ctx = m.get("context_length") or arch.get("context_length") or top.get("context_length") or lim.get("context")
    out = (m.get("max_completion_tokens") or top.get("max_completion_tokens")
           or top.get("max_output_tokens") or lim.get("output"))
    pr = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
    mods = _modalities(m)
    caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
    tool = m.get("tool_call", m.get("function_calling", caps.get("tool_call", caps.get("function_calling", True))))
    reasoning = m.get("reasoning", caps.get("reasoning", True))
    return {
        "limit": {"context": int(ctx) if ctx else None, "output": int(out) if out else None},
        "pricing": {k: str(pr[k]) for k in ("prompt", "completion", "request") if pr.get(k) is not None},
        "capabilities": {
            "tool_call": bool(tool),
            "reasoning": bool(reasoning),
            "vision": "image" in mods["input"] or "video" in mods["input"] or bool(caps.get("vision")),
            "audio": "audio" in mods["input"] or "audio" in mods["output"],
            "embedding": "embedding" in mods["output"] or bool(caps.get("embedding")),
        },
        "modalities": mods,
        "free": _is_free_model(m),
    }

def _manual_meta(e: dict) -> dict:
    """Metadata for a hand-added model (config `manual_models`)."""
    lim = e.get("limit") or {}
    ctx = e.get("context") or lim.get("context")
    out = e.get("output") or lim.get("output")
    mods = e.get("modalities") or {"input": ["text"], "output": ["text"]}
    ins = mods.get("input") or []; outs = mods.get("output") or []
    caps = {
        "tool_call": bool(e.get("tool_call", True)),
        "reasoning": bool(e.get("reasoning", True)),
        "vision": bool(e.get("vision", "image" in ins or "video" in ins)),
        "audio": bool(e.get("audio", "audio" in ins or "audio" in outs)),
        "embedding": bool(e.get("embedding", "embedding" in outs)),
    }
    return {"limit": {"context": int(ctx) if ctx else None, "output": int(out) if out else None},
            "pricing": e.get("pricing") or {},
            "capabilities": caps, "modalities": mods,
            "free": e.get("free"), "name": e.get("name")}


def _categorize(caps: dict, mods: dict) -> str:
    """Capability category by modality, not by behaviour flags."""
    ins = set(mods.get("input") or []); outs = set(mods.get("output") or [])
    if caps.get("embedding") or "embedding" in outs: return "embedding"
    if ins & {"image", "video"}: return "vision"
    if "audio" in ins or "audio" in outs: return "audio"
    if "image" in outs: return "image-generation"
    return "text"

def _parse_env_pool(raw: str, prefix: str) -> list[dict]:
    """Parse an env key pool.

    Accepted item formats inside the comma-separated value:
      - ``key``                  -> label auto-generated (prefix-envN)
      - ``label=key``            -> explicit label (key id shown in logs)
      - ``label=sk-...==``       -> split on the FIRST '=' only
    """
    out = []
    for i, item in enumerate((raw or "").split(",")):
        item = item.strip()
        if not item: continue
        if "=" in item:
            label, key = item.split("=", 1)
            label = label.strip() or f"{prefix}-env{i+1}"
            key = key.strip()
        else:
            label, key = f"{prefix}-env{i+1}", item
        if key:
            out.append({"key": key, "label": label})
    return out

class Provider:
    def __init__(self, name: str, cfg: dict):
        self.name=name; self.base_url=cfg["base_url"].rstrip("/")
        keys=list(cfg.get("keys") or [])
        # Env key pools (preferred):
        #   keys_env:       "PROVIDER_API_KEYS"       -> label=key,label=key
        #   keys_json_env:  "PROVIDER_API_KEYS_JSON"  -> [{"label":"..","key":".."}]
        env_name=cfg.get("keys_env")
        if env_name:
            keys += _parse_env_pool(os.environ.get(env_name, ""), name)
        json_name=cfg.get("keys_json_env")
        if json_name:
            try: arr=json.loads(os.environ.get(json_name) or "[]")
            except Exception: arr=[]
            if isinstance(arr, list):
                for i, e in enumerate(arr):
                    if isinstance(e, str) and e.strip():
                        keys.append({"key": e.strip(), "label": f"{name}-json{i+1}"})
                    elif isinstance(e, dict) and e.get("key"):
                        keys.append({"key": str(e["key"]), "label": str(e.get("label") or f"{name}-json{i+1}")})
        # De-duplicate identical key values; keep the first label.
        seen=set(); merged=[]
        for k in keys:
            v=k.get("key")
            if not v or v in seen: continue
            seen.add(v); merged.append(k)
        self.pool=KeyPool(merged)
        self.only_models=_as_list(cfg.get("models"))
        # Model filter: exact list (models), globs/regex (include_models),
        # exclusions (exclude_models) and an optional cap (max_models).
        self.include_models=_as_list(cfg.get("include_models"))
        self.exclude_models=_as_list(cfg.get("exclude_models"))
        self.max_models=int(cfg.get("max_models") or 0)
        self.filter_free=bool(cfg.get("filter_free", False))
        self.reasoning_auto=bool(cfg.get("reasoning_auto", True))
        self.reasoning_default=str(cfg.get("reasoning_default") or "medium").lower()
        self.reasoning_levels=tuple(str(x).lower() for x in (cfg.get("reasoning_levels") or _THINKING_LEVELS))
        # Seed from config so a provider still routes/lists its declared
        # models even when its /models endpoint is down at startup.
        self.alias_to_id: dict[str,str]={m:m for m in self.only_models}
        self.alias_to_id.update({m.replace("/","-"):m for m in self.only_models})
        self.model_ids: list[str]=list(self.only_models)
        self.model_meta: dict[str,dict]={m:{} for m in self.only_models}
        # Hand-added models: always present, always allowed, never filtered out.
        self.manual_ids: list[str]=[]
        self.manual_meta: dict[str,dict]={}
        _manual_src = cfg.get("manual_models")
        if isinstance(_manual_src, (str, dict)): _manual_src = [_manual_src]
        for e in (_manual_src or []):
            if isinstance(e, str): e = {"id": e}
            if not isinstance(e, dict) or not e.get("id"): continue
            mid = str(e["id"])
            if mid in self.manual_meta: continue
            self.manual_ids.append(mid)
            self.manual_meta[mid] = _manual_meta(e)
            self.alias_to_id.setdefault(mid, mid)
            self.alias_to_id.setdefault(mid.replace("/", "-"), mid)
            if mid not in self.model_ids: self.model_ids.append(mid)
            self.model_meta[mid] = self.manual_meta[mid]
        # Model ids known to be paid, recorded even though they are dropped
        # from the catalog, so a direct call to one is refused instead of
        # being forwarded just because it has no metadata.
        self._paid_blocked: set[str]=set()
        self._last_map=0.0; self._last_attempt=0.0; self._map_attempts=0
    def _hit(self, mid: str, patterns) -> bool:
        """Filters accept either the upstream id or the catalog id."""
        return _matches(mid, patterns) or _matches(f"{self.name}/{mid}", patterns)
    def allows(self, mid: str) -> bool:
        """Access check for a model id (must mirror the catalog filters)."""
        if mid in getattr(self, "manual_ids", ()): return True
        if self.only_models and not self._hit(mid, self.only_models): return False
        if self.include_models and not self._hit(mid, self.include_models): return False
        if self.exclude_models and self._hit(mid, self.exclude_models): return False
        if self.filter_free:
            if mid in getattr(self, "_paid_blocked", ()): return False
            meta = self.model_meta.get(mid) or {}
            if meta.get("free") is False: return False
        return True
    async def load_map(self):
        self._last_attempt=time.time(); self._map_attempts+=1
        for entry in self.pool.peek_order():
            try:
                resp = await client.get(f"{self.base_url}/models", headers={"Authorization":f"Bearer {entry['key']}"}, timeout=20)
            except Exception as e:
                debug_log.record("map_fail", provider=self.name, key=entry["label"], error=type(e).__name__); continue
            if resp.status_code != 200:
                debug_log.record("map_fail", provider=self.name, key=entry["label"], status=resp.status_code); continue
            try: payload = resp.json()
            except Exception: payload = None
            models = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                # Never overwrite a good map with an empty/broken one.
                debug_log.record("map_fail", provider=self.name, key=entry["label"], error="bad_payload")
                log.warning("[%s] /models returned a non-list payload; keeping previous map", self.name)
                continue
            mapping={}; model_ids=[]; metas={}; paid=set()
            for m in models:
                if not isinstance(m, dict): continue
                mid=m.get("id","")
                if not mid: continue
                if self.only_models and not self._hit(mid, self.only_models): continue
                if self.include_models and not self._hit(mid, self.include_models): continue
                if self.exclude_models and self._hit(mid, self.exclude_models): continue
                if self.filter_free:
                    free=_is_free_model(m)
                    # Only drop models positively known to be paid; if the
                    # upstream exposes no pricing, keep it instead of hiding it.
                    if free is False:
                        paid.add(mid); paid.add(mid.replace("/","-")); continue
                model_ids.append(mid); mapping[mid]=mid; mapping[mid.replace("/","-")]=mid
                metas[mid]=_upstream_meta(m)
            if self.max_models and len(model_ids) > self.max_models:
                # Deterministic cap: free models first, then alphabetical.
                model_ids = sorted(model_ids, key=lambda x: (metas.get(x, {}).get("free") is not True, x))[:self.max_models]
                mapping = {}
                for x in model_ids:
                    mapping[x] = x; mapping[x.replace("/", "-")] = x
            if not model_ids:
                debug_log.record("map_fail", provider=self.name, key=entry["label"], error="empty_map")
                log.warning("[%s] /models returned no usable models; keeping previous map", self.name)
                continue
            for mid in self.manual_ids:
                if mid not in mapping:
                    mapping[mid]=mid; mapping[mid.replace("/","-")]=mid; model_ids.append(mid)
                metas.setdefault(mid, self.manual_meta.get(mid) or {})
            # Hand-added models bypass every filter, including the free one.
            for mid in self.manual_ids:
                paid.discard(mid); paid.discard(mid.replace("/","-"))
            self._paid_blocked=paid
            self.alias_to_id=mapping; self.model_ids=model_ids; self.model_meta=metas; self._last_map=time.time()
            debug_log.record("map_ok", provider=self.name, models=len(model_ids), key=entry["label"])
            log.info("[%s] routing map loaded: %d model ids", self.name, len(model_ids)); return
        log.warning("[%s] routing map unavailable - aliases forwarded as-is", self.name)

providers: dict[str, Provider] = {}
_map_lock = asyncio.Lock(); _map_refresh_inflight: set[str] = set()

def _parse_model(model_id: str) -> tuple[Optional[str], str]:
    if "/" in model_id:
        head, rest = model_id.split("/", 1)
        if head in providers: return head, rest
    return None, model_id

async def _refresh_maps():
    if not providers: return
    async with _map_lock:
        for p in providers.values(): await p.load_map()

def _lazy_refresh(provider: Provider):
    if provider.name in _map_refresh_inflight: return
    last = max(provider._last_map, provider._last_attempt)
    if last and time.time() - last < 300: return
    _map_refresh_inflight.add(provider.name)
    async def _run():
        try:
            async with _map_lock: await provider.load_map()
        except Exception as ex: log.error("lazy map refresh failed for %s: %s", provider.name, ex)
        finally: _map_refresh_inflight.discard(provider.name)
    try: asyncio.create_task(_run())
    except RuntimeError: _map_refresh_inflight.discard(provider.name)

def _merged_meta(provider: Provider, mid: str) -> dict:
    """Upstream metadata, then catalog overrides, then sane defaults."""
    up = (provider.model_meta or {}).get(mid) or {}
    cat = _read_local_models().get(f"{provider.name}/{mid}") or {}
    cat_lim = cat.get("limit") or {}
    up_lim = up.get("limit") or {}
    ctx = cat_lim.get("context") or up_lim.get("context") or 198000
    out = cat_lim.get("output") or up_lim.get("output") or 65536
    up_caps = up.get("capabilities") or {}
    def pick(key, default):
        if key in cat: return bool(cat[key])
        if key in up_caps: return bool(up_caps[key])
        return default
    mods = cat.get("modalities") or up.get("modalities") or {"input":["text"],"output":["text"]}
    ins = mods.get("input") or []; outs = mods.get("output") or []
    caps = {
        "tool_call": pick("tool_call", True),
        "reasoning": pick("reasoning", True),
        "vision": pick("vision", "image" in ins or "video" in ins),
        "audio": pick("audio", "audio" in ins or "audio" in outs),
        "embedding": pick("embedding", "embedding" in outs),
    }
    free = cat.get("free")
    if free is None: free = up.get("free")
    pricing = cat.get("pricing") or up.get("pricing") or {"prompt":"0","completion":"0","request":"0"}
    source = "catalog" if cat else ("upstream" if up else "default")
    return {
        "metadata": {"context_window": {"max_input_tokens": int(ctx), "max_output_tokens": int(out)},
                     "pricing": pricing, "source": source},
        "capabilities": caps,
        "modalities": mods,
        "category": cat.get("category") or _categorize(caps, mods),
        "free": free,
    }

def _build_catalog() -> dict:
    old = _read_local_models()
    models = {}
    for name, p in providers.items():
        for mid in (p.model_ids or p.only_models):
            full = f"{name}/{mid}"
            if full in models: continue
            meta = _merged_meta(p, mid)
            cw = meta["metadata"]["context_window"]
            entry = {
                "name": full,
                "tool_call": meta["capabilities"]["tool_call"],
                "reasoning": meta["capabilities"]["reasoning"],
                "vision": meta["capabilities"]["vision"],
                "audio": meta["capabilities"]["audio"],
                "embedding": meta["capabilities"]["embedding"],
                "category": meta["category"],
                "free": meta["free"],
                "limit": {"context": cw["max_input_tokens"], "output": cw["max_output_tokens"]},
                "modalities": meta["modalities"],
                "pricing": meta["metadata"]["pricing"],
            }
            if (old.get(full) or {}).get("name"): entry["name"] = old[full]["name"]
            models[full] = entry
    return models

def _write_catalog() -> bool:
    """Write the live catalog. Returns True when written, False when deferred
    (the refresh loop retries shortly instead of waiting a full interval)."""
    if not CFG.get("write_catalog", True): return True
    try:
        # Defer only briefly while a keyed provider's first map is in flight;
        # a provider that is permanently failing (dead/paid keys) must not
        # block the catalog for everyone else.
        now = time.time()
        missing = [p.name for p in providers.values()
                   if p.pool.keys and not p.model_ids and p._map_attempts < 2]
        if missing:
            log.warning("catalog write deferred; initial map pending for: %s", ", ".join(missing))
            return False
        stale = [p.name for p in providers.values() if p.pool.keys and not p.model_ids]
        if stale:
            log.warning("catalog written without models for: %s", ", ".join(stale))
        models = _build_catalog()
        if not models: return True
        payload = {"provider": {"customProxy": {"models": models}}}
        tmp = KILO_CONFIG_PATH.with_name(KILO_CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, KILO_CONFIG_PATH)
        os.chmod(KILO_CONFIG_PATH, 0o664)
        _LOCAL_MODELS_CACHE["mtime"] = None
        log.info("catalog written: %d models -> %s", len(models), KILO_CONFIG_PATH)
        return True
    except Exception as e:
        log.warning("catalog write skipped: %s", e)
        return False

async def _catalog_loop():
    try:
        interval = int(os.environ.get("CATALOG_REFRESH_SECONDS") or CFG.get("catalog_refresh_seconds", 600))
    except (TypeError, ValueError):
        interval = 600
    interval = max(60, interval)
    while True:
        try:
            await _refresh_maps()
            if not _write_catalog():
                # map still in flight: retry in 60s instead of a full interval
                await asyncio.sleep(60)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("catalog refresh failed")
        await asyncio.sleep(interval)

_HOP_BY_HOP = {"connection","keep-alive","proxy-authenticate","proxy-authorization",
               "te","trailer","transfer-encoding","upgrade",
               "content-encoding","content-length"}

def _clean_headers(h) -> dict:
    low = {k.lower(): v for k, v in h.items()}
    conn_tokens = {t.strip().lower() for t in (low.get("connection","") or "").split(",") if t.strip()}
    drop = _HOP_BY_HOP | conn_tokens
    return {k: v for k, v in low.items() if k not in drop}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=180, write=30, pool=10))
    _load_custom_providers()
    for pname, pcfg in CFG.get("providers", {}).items():
        if pcfg.get("enabled") is False or not _provider_enabled(pname):
            log.info("provider %s disabled by config/filter", pname)
            continue
        try:
            providers[pname] = Provider(pname, pcfg)
        except Exception as e:
            log.error("provider %r invalid, skipped: %s", pname, e)
    _index_secrets()
    total_keys = sum(len(p.pool.keys) for p in providers.values())
    log.info("CustomProxy started on :%d | providers=%s | keys=%d", PORT, list(providers), total_keys)
    if not AUTH_TOKEN:
        if CFG.get("allow_no_auth", False) or _env_bool("ALLOW_NO_AUTH", False):
            log.warning("No auth token configured - allow_no_auth=true, endpoints are OPEN")
        else:
            raise RuntimeError(
                "PROXY_AUTH_TOKEN (or config auth_token) is required. "
                "Copy .env.example to .env and set PROXY_AUTH_TOKEN to a long "
                "random string (python -c \"import secrets;print(secrets.token_urlsafe(32))\"). "
                "Set allow_no_auth=true only for a fully trusted local setup."
            )
    elif len(AUTH_TOKEN) < 24:
        log.warning("AUTH token is shorter than 24 characters - use a long random token")
    # Never block startup on upstream network calls: the catalog loop does
    # the first refresh in the background and keeps refreshing on a timer.
    refresh_task = asyncio.create_task(_catalog_loop())
    app.state.map_refresh_task = refresh_task
    try: yield
    finally:
        refresh_task.cancel()
        try: await refresh_task
        except (asyncio.CancelledError, Exception): pass
        if client: await client.aclose()

app = FastAPI(title="CustomProxy", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["x-request-id"])

@app.get("/")
async def root():
    return {"name":"CustomProxy","openai_compatible":True,"base_url":"/v1",
            "models":"/v1/models","health":"/health","logs":"/logs"}

@app.get("/health")
async def health(request: Request):
    # Unauthenticated liveness probe (Docker/monitor) gets a minimal body;
    # detailed key stats still require the token.
    if AUTH_TOKEN:
        authed = hmac.compare_digest(_request_token(request), AUTH_TOKEN)
    else:
        authed = True
    if not authed:
        return {"status":"ok","proxy":"CustomProxy","providers":len(providers)}
    try: catalog_age = round(time.time() - KILO_CONFIG_PATH.stat().st_mtime)
    except OSError: catalog_age = None
    return {"status":"ok","proxy":"CustomProxy","catalog_age_s":catalog_age,"request_log_entries":len(request_log.entries),"providers":{name:{"base_url":p.base_url,"keys":p.pool.stats(),"mapped_models":len(p.model_ids),"map_age_s":round(time.time()-p._last_map) if p._last_map else None,"filter":{"include":p.include_models,"exclude":p.exclude_models,"max":p.max_models,"free_only":p.filter_free}} for name,p in providers.items()}}

@app.get("/debug")
async def debug_events(request: Request, tail: int = 50):
    _check_auth(request); tail=max(1,min(tail,DEBUG_MAX_EVENTS))
    return {"summary": debug_log.summary(), "events": list(debug_log.events)[-tail:]}

@app.post("/debug/clear")
async def debug_clear(request: Request):
    _check_auth(request); debug_log.clear(); return {"status":"ok"}

@app.get("/logs")
async def logs(request: Request, limit: int = 100, outcome: str | None = None,
               provider: str | None = None, model: str | None = None):
    """Structured per-request log (newest last): outcome, status, latency, key,
    upstream model, token usage and the exact upstream error."""
    _check_auth(request)
    limit = max(1, min(limit, 1000))
    items = request_log.tail(limit * 4)
    if outcome: items = [e for e in items if e.get("outcome") == outcome]
    if provider: items = [e for e in items if e.get("provider") == provider]
    if model: items = [e for e in items if e.get("model") == model]
    items = items[-limit:]
    return {"count": len(items), "entries": items}

@app.post("/refresh")
async def refresh(request: Request):
    _check_auth(request); await _refresh_maps(); _write_catalog()
    return {"status":"ok","providers":{n:len(p.model_ids) for n,p in providers.items()}}

@app.post("/catalog/refresh")
async def catalog_refresh(request: Request):
    # Alias of /refresh - ai-framework refresh_catalog() calls this path.
    _check_auth(request); await _refresh_maps(); _write_catalog()
    return {"status":"ok","providers":{n:len(p.model_ids) for n,p in providers.items()}}

def _model_meta_out(provider: Provider, mid: str) -> dict:
    m = _merged_meta(provider, mid)
    return {
        "metadata": m["metadata"],
        "capabilities": m["capabilities"],
        "modalities": m["modalities"],
        "category": m["category"],
        "free": m["free"],
    }

@app.get("/v1/models")
async def list_models(request: Request):
    _check_auth(request)
    data=[]; seen=set()
    for name,p in providers.items():
        for mid in (p.model_ids or p.only_models):
            full=f"{name}/{mid}"
            if full in seen: continue
            seen.add(full)
            meta=_model_meta_out(p, mid)
            entry={"id":full,"object":"model","owned_by":name}
            entry.update(meta)
            data.append(entry)
    return {"object":"list","data":data,"total_count":len(data)}

@app.get("/v1/models/{model_id:path}")
async def get_model_info(request: Request, model_id: str):
    _check_auth(request)
    mid = model_id.lstrip("/")
    provider_name, alias = _parse_model(mid)
    provider = providers.get(provider_name) if provider_name else None
    if provider is None:
        return JSONResponse({"error": {"message": "Unknown provider in model id."}}, status_code=404)
    upstream_model = provider.alias_to_id.get(alias, alias)
    if alias not in provider.alias_to_id:
        _lazy_refresh(provider)
    if not (provider.allows(alias) or provider.allows(upstream_model)):
        rid = uuid.uuid4().hex[:16]
        return _error_response(f"Model '{mid}' is filtered out for provider '{provider_name}'.",
                               "model_filtered", 404, rid, provider=provider_name)
    _meta = _model_meta_out(provider, upstream_model)
    return {
        "id": f"{provider_name}/{upstream_model}" if provider_name else mid,
        "object": "model",
        "owned_by": provider_name or "auto",
        "source": provider_name,
        "bridge_name": mid,
        "upstream_model": upstream_model,
        "metadata": _meta["metadata"],
        "capabilities": _meta["capabilities"],
        "modalities": _meta["modalities"],
        "category": _meta["category"],
        "free": _meta["free"],
    }

_LOCAL_MODELS_CACHE: dict = {"mtime": None, "data": {}}

def _read_local_models() -> dict:
    try: mtime = KILO_CONFIG_PATH.stat().st_mtime
    except OSError: return _LOCAL_MODELS_CACHE["data"]
    if _LOCAL_MODELS_CACHE["mtime"] == mtime: return _LOCAL_MODELS_CACHE["data"]
    try: text = KILO_CONFIG_PATH.read_text(encoding="utf-8-sig")
    except Exception: return _LOCAL_MODELS_CACHE["data"]
    try:
        stripped = re.sub(r"^\s*//.*$","",text,flags=re.M)
        cfg = json.loads(stripped)
        data = cfg.get("provider",{}).get("customProxy",{}).get("models",{}) or {}
    except Exception as e:
        log.warning("kilo_models.json parse failed, keeping previous metadata: %s", e)
        data = _LOCAL_MODELS_CACHE["data"]
    _LOCAL_MODELS_CACHE["mtime"] = mtime
    _LOCAL_MODELS_CACHE["data"] = data
    return data

@app.get("/.well-known/opencode")
async def well_known_config():
    models = _build_catalog()
    source = "live"
    if not models:
        models = _read_local_models(); source = "kilo_models.json"
    return JSONResponse({"$schema":"https://app.kilo.ai/config.json","provider":{"customProxy":{"options":{},"models":models}},"_source":source,"_count":len(models)})

_KEEP_REQUEST_HEADERS = ("HTTP-Referer","X-Title","Accept")


def _kilo_reasoning(raw: bytes, *, stream: bool = False) -> bytes:
    """Emit reasoning in the Kilo-native shape that Kilo's AI SDK displays.

    Kilo (and OpenRouter) recognize two fields for thinking:
      - ``reasoning``           flat string of the thinking text
      - ``reasoning_details``   array of structured reasoning blocks
                                  ``[{type:"reasoning.text", text:"...", index:0}]``

    Upstream providers use various field names for the same thing:
      - OpenAI / Groq / some OpenAI-compat:  ``reasoning``
      - DeepSeek / agnes / vLLM / SGLang:    ``reasoning_content``
      - Kilo Gateway native:                 ``reasoning`` + ``reasoning_details``

    This function normalizes all of them into the Kilo shape in BOTH the
    non-streaming message and the streaming delta, so Kilo's UI shows thinking
    for every provider. It mirrors exactly what the Kilo Gateway itself
    returns (verified: reasoning + reasoning_details in both modes).

    Non-streaming:  message.reasoning_content  -> message.reasoning + reasoning_details
                    message.reasoning          -> (unchanged, add reasoning_details if absent)
    Streaming SSE:  delta.reasoning_content    -> delta.reasoning + delta.reasoning_details
                    delta.reasoning            -> (unchanged, add reasoning_details if absent)
    """
    import json as _json
    try:
        text = raw.decode("utf-8")
    except Exception:
        return raw

    # Fast path: no reasoning fields at all - return untouched.
    if "reasoning" not in text and "reasoning_content" not in text:
        return raw

    def _emit_kilo_shape(msg: dict) -> bool:
        """Rewrite a single message/delta dict to the Kilo shape.
        Returns True if anything changed."""
        changed = False
        # 1. reasoning_content (DeepSeek/agnes/vLLM) -> reasoning
        if "reasoning_content" in msg:
            rc = msg.pop("reasoning_content")
            if "reasoning" not in msg:
                msg["reasoning"] = rc or ""
                changed = True
        # 2. If reasoning is present but reasoning_details is missing, build it
        reasoning_val = msg.get("reasoning")
        if reasoning_val and not msg.get("reasoning_details"):
            msg["reasoning_details"] = [{
                "type": "reasoning.text",
                "text": reasoning_val,
                "format": "unknown",
                "index": 0,
            }]
            changed = True
        return changed

    # Non-streaming JSON
    try:
        parsed = _json.loads(text)
        if isinstance(parsed, dict) and "choices" in parsed:
            changed = False
            for ch in parsed.get("choices", []):
                msg = ch.get("message") or {}
                if _emit_kilo_shape(msg):
                    changed = True
            if changed:
                return _json.dumps(parsed).encode("utf-8")
    except Exception:
        pass

    # Streaming SSE: per data-line
    if stream:
        out = []
        for line in text.split("\n"):
            if line.startswith("data:") and ("reasoning" in line or "reasoning_content" in line):
                data = line[5:].strip()
                try:
                    payload = _json.loads(data)
                    changed = False
                    for ch in payload.get("choices", []):
                        delta = ch.get("delta") or {}
                        if _emit_kilo_shape(delta):
                            changed = True
                    if changed:
                        line = "data: " + _json.dumps(payload, separators=(",", ":"))
                except Exception:
                    pass
            out.append(line)
        return "\n".join(out).encode("utf-8")

    return raw

@app.api_route("/v1/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS"])
async def proxy(request: Request, path: str):
    if request.method == "OPTIONS":
        return Response(status_code=204)
    _check_auth(request)
    request_id = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:64]
    t0 = time.perf_counter()
    if not rate_limiter.allow(_client_id(request)):
        _log_request(request_id, "error", request, path, None, t0, http_status=429,
                     error_type="rate_limited", error="Local rate limit exceeded.")
        return _error_response("Local rate limit exceeded.", "rate_limited", 429, request_id, retry_after=60)
    if path == "models": return await list_models(request)

    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_REQUEST_BYTES:
        _log_request(request_id, "error", request, path, None, t0, http_status=413,
                     error_type="request_too_large", error=f"Content-Length {cl} exceeds limit")
        return _error_response("Request body too large.", "request_too_large", 413, request_id)
    raw_body = await request.body()
    if len(raw_body) > MAX_REQUEST_BYTES:
        _log_request(request_id, "error", request, path, None, t0, http_status=413,
                     error_type="request_too_large", error=f"body {len(raw_body)} bytes exceeds limit")
        return _error_response("Request body too large.", "request_too_large", 413, request_id)
    has_body = request.method in ("POST","PUT","PATCH")
    ctype = request.headers.get("content-type", "")
    is_json_ctype = "application/json" in ctype.lower() or not ctype
    model_id=None; is_stream=False; body_json=None
    if has_body and raw_body:
        if is_json_ctype:
            try:
                body_json = json.loads(raw_body)
            except Exception:
                if "application/json" in ctype.lower():
                    _log_request(request_id, "error", request, path, None, t0,
                                 http_status=400, error_type="invalid_json",
                                 error="Request body is not valid JSON.")
                    return _error_response("Request body is not valid JSON.", "invalid_request", 400, request_id)
        if body_json is not None and not isinstance(body_json, dict):
            return _error_response("Request body must be a JSON object.", "invalid_request", 400, request_id)
        if isinstance(body_json, dict):
            model_id = body_json.get("model"); is_stream = bool(body_json.get("stream", False))
        if not model_id:
            # multipart/form-data and other non-JSON OpenAI payloads
            m = (re.search(rb'name="model"\r?\n\r?\n([^\r\n]+)', raw_body)
                 or re.search(rb'"model"\s*:\s*"([^"]+)"', raw_body))
            if m:
                model_id = m.group(1).decode("utf-8", "ignore").strip()
    if not model_id:
        _log_request(request_id, "error", request, path, None, t0,
                     http_status=400, error_type="missing_model", error="missing 'model'")
        return _error_response("Missing 'model' in request body.", "invalid_request", 400, request_id)
    # Optional full-body debug log (off by default; can contain prompts).
    if REQUEST_BODY_LOG and body_json is not None:
        try:
            _dbg = {"ts": round(time.time(),3), "request_id": request_id, "model": model_id, "stream": is_stream,
                    "has_reasoning_effort": "reasoning_effort" in body_json,
                    "reasoning_effort": body_json.get("reasoning_effort"),
                    "reasoning": body_json.get("reasoning"),
                    "all_keys": list(body_json.keys()),
                    "full_body": body_json}
            _line = json.dumps(_dbg)
            if len(_line) > 65536:
                _dbg.pop("full_body", None)
                _dbg["full_body_omitted_bytes"] = len(_line)
                _line = json.dumps(_dbg)
            _log_path = _resolve(CFG.get("request_log_path"), "logs/request_log.jsonl")
            try:
                if _log_path.exists() and _log_path.stat().st_size > REQUEST_LOG_MAX:
                    _log_path.write_bytes(b"")
            except OSError:
                pass
            with open(_log_path, "a") as _fh: _fh.write(_line + "\n")
        except Exception as e:
            log.debug("request logging failed: %s", e)
    provider_name, alias = _parse_model(model_id) if model_id else (None,None)
    provider = providers.get(provider_name) if provider_name else None
    if provider is None:
        msg = f"Model '{model_id}' must be prefixed with a known provider. Known: {', '.join(providers)}."
        _log_request(request_id, "error", request, path, model_id, t0,
                     http_status=404, error_type="unknown_provider", error=msg)
        return _error_response(msg, "unknown_provider", 404, request_id)
    if not provider.pool.keys:
        msg = f"Provider '{provider.name}' has no keys configured."
        _log_request(request_id, "error", request, path, model_id, t0,
                     http_status=503, provider=provider.name, error_type="no_keys", error=msg)
        return _error_response(msg, "no_keys", 503, request_id, provider=provider.name)
    upstream_model = provider.alias_to_id.get(alias, alias)
    if alias not in provider.alias_to_id: _lazy_refresh(provider)
    if not (provider.allows(alias) or provider.allows(upstream_model)):
        msg = f"Model '{model_id}' is filtered out for provider '{provider.name}'."
        _log_request(request_id, "error", request, path, model_id, t0, http_status=403,
                     provider=provider.name, upstream_model=upstream_model,
                     error_type="model_filtered", error=msg)
        return _error_response(msg, "model_filtered", 403, request_id, provider=provider.name)
    url = f"{provider.base_url}/{path}"
    if request.url.query: url = f"{url}?{request.url.query}"
    last_status=None; last_message=""; last_key=None; last_retry_after=None
    reasoning_stripped=False; passthrough=None; saw_429=False; attempts=0
    queue = provider.pool.available()
    if not queue:
        # Distinguish "cooling down" from "exhausted": never disguise it.
        now = time.time()
        fut = [t for t in provider.pool.cooldown_until.values() if t > now]
        wait = max(0.0, min(fut) - now) if fut else 0.0
        if wait > 30:
            ra = int(wait)
            msg = f"All keys for provider '{provider.name}' are cooling down; next available in {ra}s."
            _log_request(request_id, "error", request, path, model_id, t0, http_status=429,
                         provider=provider.name, error_type="all_keys_cooling", error=msg, retry_after=ra)
            return _error_response(msg, "all_keys_cooling", 429, request_id, provider=provider.name, retry_after=ra)
        if wait > 0:
            await asyncio.sleep(wait)
        queue = provider.pool.available()
        if not queue:
            msg = f"All keys for provider '{provider.name}' are cooling down."
            _log_request(request_id, "error", request, path, model_id, t0, http_status=429,
                         provider=provider.name, error_type="all_keys_cooling", error=msg)
            return _error_response(msg, "all_keys_cooling", 429, request_id, provider=provider.name, retry_after=1)
    while queue:
        entry = queue.pop(0); attempts += 1; last_key = entry["label"]
        headers = {"Authorization":f"Bearer {entry['key']}"}
        if ctype: headers["Content-Type"] = ctype
        for h in _KEEP_REQUEST_HEADERS:
            if h in request.headers: headers[h]=request.headers[h]
        body = raw_body
        if body_json is not None:
            bj=dict(body_json); changed=False
            if model_id and upstream_model != model_id and bj.get("model")==model_id: bj["model"]=upstream_model; changed=True
            if reasoning_stripped:
                for k in ("reasoning_effort","reasoning"):
                    if k in bj: bj.pop(k,None); changed=True
            elif provider.reasoning_auto:
                # Per-model policy: only reasoning-capable models get a default.
                caps=(provider.model_meta.get(upstream_model) or {}).get("capabilities") or {}
                if caps.get("reasoning", True):
                    lvl=bj.get("reasoning_effort")
                    if lvl is None and "reasoning" not in bj:
                        # Client omitted it: apply the provider's default level.
                        bj["reasoning_effort"]=provider.reasoning_default; changed=True
                    elif isinstance(lvl,str) and lvl.lower() not in provider.reasoning_levels:
                        # Clamp unknown levels to the provider's supported default.
                        bj["reasoning_effort"]=provider.reasoning_default; changed=True
            if changed: body=json.dumps(bj).encode()
        provider.pool.requests[entry["label"]]+=1
        try:
            if is_stream:
                up = await client.send(client.build_request(method=request.method,url=url,headers=headers,content=body if has_body else None),stream=True)
                status=up.status_code
            else:
                up = await client.request(method=request.method,url=url,headers=headers,content=body if has_body else None)
                status=up.status_code
        except httpx.HTTPError as e:
            provider.pool.errors[entry["label"]]+=1; provider.pool.last_status[entry["label"]]=type(e).__name__
            provider.pool.note_failure(entry["label"])
            last_status=None; last_message=f"{type(e).__name__}: {e}"
            debug_log.record("upstream",provider=provider.name,key=entry["label"],action="network_error",error=type(e).__name__); continue
        except Exception as e:
            provider.pool.errors[entry["label"]]+=1
            log.exception("unexpected proxy error for provider %s", provider.name)
            _log_request(request_id, "error", request, path, model_id, t0, http_status=500,
                         provider=provider.name, error_type="proxy_error",
                         error=f"{type(e).__name__}: {e}", attempts=attempts, key=last_key)
            return _error_response("Internal proxy error.", "proxy_error", 500, request_id, provider=provider.name)
        provider.pool.last_status[entry["label"]]=status
        if status==200:
            provider.pool.note_success(entry["label"])
            debug_log.record("upstream",provider=provider.name,key=entry["label"],status=200,model=upstream_model,action="ok")
            resp_headers = _clean_headers(up.headers); resp_headers["x-request-id"] = request_id
            if is_stream:
                async def stream_gen(resp=up, rid=request_id, started=t0, label=entry["label"]):
                    buf=b""; total=0; ok=True
                    try:
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            buf += chunk
                            if b"\n" in buf:
                                complete, buf = buf.rsplit(b"\n", 1)
                                yield _kilo_reasoning(complete + b"\n", stream=True)
                            elif len(buf) > 65536:
                                # no newline for a long stretch: flush so the client is not stalled
                                yield _kilo_reasoning(buf, stream=True); buf=b""
                        if buf: yield _kilo_reasoning(buf, stream=True)
                    except Exception as se:
                        ok=False
                        _log_request(rid, "error", request, path, model_id, started, http_status=200,
                                     provider=provider.name, upstream_model=upstream_model, stream=True,
                                     key=label, attempts=attempts, bytes=total,
                                     error_type="stream_error", error=f"{type(se).__name__}: {se}")
                        raise
                    finally:
                        await resp.aclose()
                        if ok:
                            _log_request(rid, "ok", request, path, model_id, started, http_status=200,
                                         provider=provider.name, upstream_model=upstream_model, stream=True,
                                         key=label, attempts=attempts, bytes=total)
                return StreamingResponse(stream_gen(),status_code=200,headers=resp_headers)
            body_out = _kilo_reasoning(up.content, stream=False)
            usage = _usage_of(body_out)
            _log_request(request_id, "ok", request, path, model_id, t0, http_status=200,
                         provider=provider.name, upstream_model=upstream_model, stream=False,
                         key=entry["label"], attempts=attempts, bytes=len(body_out),
                         usage=usage or None)
            return Response(content=body_out,status_code=200,headers=resp_headers)
        up_headers=_clean_headers(up.headers)
        try: err_bytes = await up.aread() if is_stream else up.content
        except Exception: err_bytes=b""
        if is_stream:
            try: await up.aclose()
            except Exception: pass
        message = _extract_error_message(err_bytes or b"")
        emsg = message.lower()
        provider.pool.errors[entry["label"]]+=1
        last_status=status; last_message=message
        if (status==400 and not reasoning_stripped and ("reasoning" in emsg or "variant" in emsg) and body_json and any(k in body_json for k in ("reasoning_effort","reasoning"))):
            reasoning_stripped=True
            debug_log.record("retry_same_key",provider=provider.name,key=entry["label"],reason="reasoning_strip")
            queue.insert(0,entry); continue
        # Rotate on auth/billing/throttle/5xx; keep context-length errors client-side.
        rotate = status in (401,402,403,429) or status>=500
        if any(h in emsg for h in _CONTEXT_HINTS):
            rotate = False
        elif any(h in emsg for h in _BILLING_HINTS) or any(h in emsg for h in _RATE_HINTS):
            rotate = True
        if rotate:
            ra = up.headers.get("retry-after")
            try: ra_f = float(ra) if ra else None
            except (TypeError, ValueError): ra_f = None
            last_retry_after = ra_f
            provider.pool.note_failure(entry["label"], status, ra_f)
            if status == 429: saw_429 = True
        debug_log.record("upstream",provider=provider.name,key=entry["label"],status=status,model=upstream_model,action="rotate" if rotate else "stop",error=(message or "")[:180])
        if rotate: continue
        passthrough=(status,err_bytes,up_headers); break
    if passthrough is not None:
        pstatus,pbody,pheaders=passthrough
        msg = _extract_error_message(pbody) or f"Upstream returned HTTP {pstatus}."
        _log_request(request_id, "error", request, path, model_id, t0, http_status=pstatus,
                     provider=provider.name, upstream_model=upstream_model, key=last_key,
                     attempts=attempts, stream=is_stream, error_type="upstream_rejected", error=msg[:2000])
        pheaders = dict(pheaders); pheaders["x-request-id"] = request_id
        ct = (pheaders.get("content-type") or "").lower()
        if pbody and "json" in ct:
            try:
                pb = json.loads(pbody)
                pbody = json.dumps(_redact_obj(pb), ensure_ascii=False).encode()
                pheaders["content-length"] = str(len(pbody))
            except Exception:
                pass
        if not pbody or "json" not in ct:
            # Keep OpenAI clients happy: always return a JSON error envelope,
            # with the upstream body preserved for debugging.
            return _error_response(
                msg or f"Upstream returned HTTP {pstatus}.", "upstream_rejected", pstatus, request_id,
                provider=provider.name, upstream_model=upstream_model, upstream_body=msg[:500] or None)
        return Response(content=pbody,status_code=pstatus,media_type=ct or "application/json",headers=pheaders)

    kind, out_status = _classify_failure(last_status, last_message)
    if attempts == 0:
        kind, out_status = "all_keys_unavailable", 503
    # Surface the real reason (billing, auth, quota, rate limit) — never a
    # generic "keys exhausted" when the upstream said something specific.
    exact = last_message or f"All {attempts} key(s) for provider '{provider.name}' failed."
    debug_log.record("request_failed",model=model_id,provider=provider.name,status=last_status,error=exact[:180])
    _log_request(request_id, "error", request, path, model_id, t0, http_status=out_status,
                 provider=provider.name, upstream_model=upstream_model, key=last_key,
                 attempts=attempts, stream=is_stream, error_type=kind, error=exact[:2000],
                 last_status=last_status, saw_429=saw_429)
    ra_out = int(last_retry_after) if (kind == "rate_limited" and last_retry_after) else None
    return _error_response(exact, kind, out_status, request_id,
                           provider=provider.name, attempts=attempts, last_status=last_status,
                           retry_after=ra_out)

@app.api_route("/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS"])
async def proxy_unversioned(request: Request, path: str):
    # Same handler for clients whose base_url omits /v1 (e.g. some LangChain
    # or self-hosted setups). Declared last so it never shadows /health etc.
    return await proxy(request, path)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
