"""End-to-end tests: provider -> model -> API through a mock upstream.

The provider is real code (Provider, KeyPool, filtering, routing); only the
upstream HTTP server is mocked, so these cover the path a client actually
takes: prefix parsing, key selection, model translation, upstream call,
response passthrough, error classification and request logging.
"""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

TOKEN = "e2e-token-0123456789abcdef"


def _handler(calls):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "fast-model", "pricing": {"prompt": "0", "completion": "0"},
                 "context_length": 32000},
                {"id": "paid-model", "pricing": {"prompt": "0.001", "completion": "0.002"},
                 "context_length": 8000},
            ]})
        if "/chat/completions" in request.url.path:
            body = json.loads(request.content)
            calls.append({"auth": request.headers.get("authorization"), "body": body})
            if body.get("model") == "boom":
                return httpx.Response(500, json={"error": {"message": "upstream exploded"}})
            return httpx.Response(200, json={
                "id": "chatcmpl-e2e", "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": "pong"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            })
        return httpx.Response(404, json={"error": "not found"})
    return handle


@pytest.fixture()
def proxy(tmp_path, monkeypatch):
    """A fully wired server against a mock upstream, isolated per test."""
    monkeypatch.setenv("PROXY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("KILO_CONFIG_PATH", str(tmp_path / "catalog.json"))
    monkeypatch.setenv("REQUEST_LOG_JSONL", str(tmp_path / "requests.jsonl"))
    monkeypatch.setenv("CATALOG_REFRESH_SECONDS", "3600")

    sys.modules.pop("server", None)
    import server as cp

    # Load no real provider from config: only the mock is registered below.
    cp.PROVIDER_FILTERS.clear()
    cp.PROVIDER_FILTERS["include"] = ["__only_mock__"]

    calls = []
    with TestClient(cp.app) as c:
        # lifespan created a real client; swap in the mock for the test.
        cp.client = httpx.AsyncClient(transport=httpx.MockTransport(_handler(calls)))

        cp.providers.clear()
        prov = cp.Provider("mock", {
            "base_url": "https://mock.test/v1",
            "keys_env": "MOCK_API_KEYS",
            "keys": [], "models": [], "include_models": [],
            "exclude_models": [], "max_models": 0, "manual_models": [],
        })
        prov.pool = cp.KeyPool([
            {"label": "k1", "key": "sk-first"},
            {"label": "k2", "key": "sk-second"},
        ])
        cp.providers["mock"] = prov
        # load the routing map through the real code path
        c.post("/refresh", headers=_auth())
        yield c, cp, calls

    cp.providers.clear()


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_authenticated(proxy):
    c, _, _ = proxy
    r = c.get("/health", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "mock" in body["providers"]


def test_health_unauthenticated_is_minimal(proxy):
    c, _, _ = proxy
    body = c.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["providers"], int)  # count only, no key stats


def test_requires_auth(proxy):
    c, _, _ = proxy
    assert c.get("/v1/models").status_code == 401
    assert c.post("/v1/chat/completions",
                  json={"model": "mock/fast-model"}).status_code == 401


def test_catalog_comes_from_upstream(proxy):
    c, _, _ = proxy
    r = c.get("/v1/models", headers=_auth())
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert "mock/fast-model" in ids
    assert "mock/paid-model" in ids


def test_chat_completion_reaches_upstream(proxy):
    c, _, calls = proxy
    r = c.post("/v1/chat/completions", headers=_auth(), json={
        "model": "mock/fast-model",
        "messages": [{"role": "user", "content": "ping"}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "pong"
    assert len(calls) == 1
    assert calls[0]["auth"].startswith("Bearer sk-")
    # the provider prefix is stripped before it goes upstream
    assert calls[0]["body"]["model"] == "fast-model"


def test_unknown_provider_is_404(proxy):
    c, _, _ = proxy
    r = c.post("/v1/chat/completions", headers=_auth(),
               json={"model": "nope/x", "messages": []})
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "unknown_provider"


def test_provider_without_keys_is_503(proxy):
    c, cp, _ = proxy
    cp.providers["mock"].pool = cp.KeyPool([])
    r = c.post("/v1/chat/completions", headers=_auth(),
               json={"model": "mock/fast-model", "messages": []})
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "no_keys"


def test_missing_model_is_400(proxy):
    c, _, _ = proxy
    r = c.post("/v1/chat/completions", headers=_auth(), json={"messages": []})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request"


def test_upstream_error_is_classified(proxy):
    c, _, _ = proxy
    r = c.post("/v1/chat/completions", headers=_auth(),
               json={"model": "mock/boom", "messages": []})
    assert r.status_code >= 500
    assert r.json()["error"]["type"].startswith("upstream")
    assert r.headers.get("x-request-id")


def test_keys_rotate_across_pool(proxy):
    c, _, calls = proxy
    for _ in range(2):
        r = c.post("/v1/chat/completions", headers=_auth(),
                   json={"model": "mock/fast-model", "messages": []})
        assert r.status_code == 200, r.text
    used = {x["auth"] for x in calls}
    assert len(used) == 2, f"expected both keys to be used, saw {used}"


def test_request_log_records_outcome(proxy):
    c, _, _ = proxy
    c.post("/v1/chat/completions", headers=_auth(),
           json={"model": "mock/fast-model", "messages": []})
    r = c.get("/logs", headers=_auth())
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert entries, "request log is empty"
    entry = entries[0]
    assert entry["model"] == "mock/fast-model"
    assert "duration_ms" in entry
    assert entry.get("api_key_id") in {"k1", "k2"}


def test_well_known_catalog_is_open(proxy):
    c, _, _ = proxy
    r = c.get("/.well-known/opencode")
    assert r.status_code == 200
    body = r.json()
    assert "customProxy" in body["provider"]


@pytest.fixture()
def free_proxy(tmp_path, monkeypatch):
    """Same wiring, but the provider keeps only free models."""
    monkeypatch.setenv("PROXY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("KILO_CONFIG_PATH", str(tmp_path / "catalog.json"))
    monkeypatch.setenv("REQUEST_LOG_JSONL", str(tmp_path / "requests.jsonl"))
    monkeypatch.setenv("CATALOG_REFRESH_SECONDS", "3600")

    sys.modules.pop("server", None)
    import server as cp

    cp.PROVIDER_FILTERS.clear()
    cp.PROVIDER_FILTERS["include"] = ["__only_mock__"]

    calls = []
    with TestClient(cp.app) as c:
        cp.client = httpx.AsyncClient(transport=httpx.MockTransport(_handler(calls)))
        cp.providers.clear()
        prov = cp.Provider("mock", {
            "base_url": "https://mock.test/v1",
            "keys_env": "MOCK_API_KEYS",
            "keys": [], "models": [], "include_models": [],
            "exclude_models": [], "max_models": 0, "manual_models": [],
            "filter_free": True,
        })
        prov.pool = cp.KeyPool([{"label": "k1", "key": "sk-first"}])
        cp.providers["mock"] = prov
        c.post("/refresh", headers=_auth())
        yield c, cp, calls

    cp.providers.clear()


def test_free_filter_keeps_only_free_in_catalog(free_proxy):
    c, _, _ = free_proxy
    ids = {m["id"] for m in c.get("/v1/models", headers=_auth()).json()["data"]}
    assert "mock/fast-model" in ids          # pricing 0/0 -> free
    assert "mock/paid-model" not in ids      # pricing 0.001/0.002 -> dropped


def test_free_filter_blocks_paid_model_call(free_proxy):
    c, _, calls = free_proxy
    r = c.post("/v1/chat/completions", headers=_auth(),
               json={"model": "mock/paid-model", "messages": []})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "model_filtered"
    assert calls == [], "a filtered model must never reach the upstream"


def test_free_filter_allows_free_model_call(free_proxy):
    c, _, calls = free_proxy
    r = c.post("/v1/chat/completions", headers=_auth(),
               json={"model": "mock/fast-model", "messages": []})
    assert r.status_code == 200
    assert len(calls) == 1


def test_unknown_pricing_is_kept_not_dropped(tmp_path, monkeypatch):
    """nvidia-style payloads carry no pricing; they must not be filtered out."""
    sys.modules.pop("server", None)
    import server as cp

    assert cp._is_free_model({"id": "x"}) is None
    assert cp._is_free_model({"id": "y", "pricing": {"prompt": "0", "completion": "0"}}) is True
    assert cp._is_free_model({"id": "z", "pricing": {"prompt": "0.1", "completion": "0"}}) is False


def test_manual_model_bypasses_free_filter(tmp_path, monkeypatch):
    """manual_models are hand-added, so the free filter must not block them."""
    monkeypatch.setenv("PROXY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("KILO_CONFIG_PATH", str(tmp_path / "catalog.json"))
    monkeypatch.setenv("REQUEST_LOG_JSONL", str(tmp_path / "requests.jsonl"))
    monkeypatch.setenv("CATALOG_REFRESH_SECONDS", "3600")

    sys.modules.pop("server", None)
    import server as cp

    cp.PROVIDER_FILTERS.clear()
    cp.PROVIDER_FILTERS["include"] = ["__only_mock__"]

    calls = []
    with TestClient(cp.app) as c:
        cp.client = httpx.AsyncClient(transport=httpx.MockTransport(_handler(calls)))
        cp.providers.clear()
        prov = cp.Provider("mock", {
            "base_url": "https://mock.test/v1",
            "keys_env": "MOCK_API_KEYS",
            "keys": [], "models": [], "include_models": [],
            "exclude_models": [], "max_models": 0,
            "manual_models": ["paid-model"],
            "filter_free": True,
        })
        prov.pool = cp.KeyPool([{"label": "k1", "key": "sk-first"}])
        cp.providers["mock"] = prov
        c.post("/refresh", headers=_auth())

        ids = {m["id"] for m in c.get("/v1/models", headers=_auth()).json()["data"]}
        assert "mock/paid-model" in ids, "manual model must stay in the catalog"

        r = c.post("/v1/chat/completions", headers=_auth(),
                   json={"model": "mock/paid-model", "messages": []})
        assert r.status_code == 200
        assert len(calls) == 1

    cp.providers.clear()


# ---------------------------------------------------------------------------
# Regression tests for the logic-fix pass. Each one failed before the fix.
# ---------------------------------------------------------------------------

def test_billing_402_with_context_words_still_rotates():
    """A 402 must stay billing even when the message mentions max_tokens.

    Before the fix, context hints were checked first, so a billing error that
    happened to contain 'max_tokens' was classed context_length, and rotation
    stopped instead of trying the next key.
    """
    sys.modules.pop("server", None)
    import server as cp

    kind, status = cp._classify_failure(402, "You exceeded your max_tokens billing quota")
    assert (kind, status) == ("billing_required", 402)
    assert kind in cp._ROTATE_KINDS

    # A genuine context error still stops.
    ctx, _ = cp._classify_failure(400, "maximum context length is 8192 tokens")
    assert ctx == "context_length"
    assert ctx not in cp._ROTATE_KINDS


def test_unknown_pricing_is_not_reported_as_zero():
    """No pricing data must read as unknown, not as free."""
    sys.modules.pop("server", None)
    import server as cp

    p = cp.Provider("p", {"base_url": "http://x/v1", "keys": [{"label": "k", "key": "k"}]})
    p.model_ids = ["m1"]
    p.model_meta = {"m1": {}}
    meta = cp._merged_meta(p, "m1")
    assert meta["metadata"]["pricing"] == {}
    assert meta["free"] is None


async def _load_with(prov, payload):
    """Run a real load_map() against a canned /models payload."""
    import server as cp

    class _Resp:
        status_code = 200
        def __init__(self, data): self._data = data
        def json(self): return self._data

    class _Client:
        def __init__(self, data): self._data = data
        async def get(self, *a, **k): return _Resp(self._data)

    saved = cp.client
    cp.client = _Client(payload)
    try:
        await prov.load_map()
    finally:
        cp.client = saved
    return prov


def test_declared_models_stay_listed_after_live_map():
    """A declared model missing upstream must not vanish from the catalog.

    allows() already permitted it, so dropping it from model_ids made a model
    callable but invisible.
    """
    sys.modules.pop("server", None)
    import asyncio
    import server as cp

    prov = cp.Provider("p", {"base_url": "http://x/v1", "keys": [{"label": "k", "key": "k"}],
                             "models": ["declared-a", "declared-b", "declared-c"]})
    asyncio.run(_load_with(prov, {"data": [
        {"id": "declared-a", "pricing": {"prompt": "0", "completion": "0"}},
    ]}))
    assert set(prov.model_ids) == {"declared-a", "declared-b", "declared-c"}
    assert all(prov.allows(m) for m in prov.model_ids)


def test_max_models_blocks_overflow_on_direct_call():
    """max_models is an access rule, not just a listing cap."""
    sys.modules.pop("server", None)
    import asyncio
    import server as cp

    prov = cp.Provider("p", {"base_url": "http://x/v1", "keys": [{"label": "k", "key": "k"}],
                             "max_models": 1})
    asyncio.run(_load_with(prov, {"data": [
        {"id": "a-model", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "z-model", "pricing": {"prompt": "0", "completion": "0"}},
    ]}))
    assert prov.model_ids == ["a-model"]
    assert prov.allows("a-model")
    assert not prov.allows("z-model"), "capped-out model must not be callable"


def test_safe_upstream_path_rejects_traversal():
    sys.modules.pop("server", None)
    import server as cp

    assert cp._safe_upstream_path("../../admin/secrets") is None
    assert cp._safe_upstream_path("https://evil.example.com/x") is None
    assert cp._safe_upstream_path("//evil.example.com/x") is None
    assert cp._safe_upstream_path("a/../b") is None
    assert cp._safe_upstream_path("chat/completions") == "chat/completions"
    assert cp._safe_upstream_path("./models") == "models"


def test_auth_error_uses_openai_envelope(monkeypatch, tmp_path):
    """401 must look like every other error so clients find error.message."""
    monkeypatch.setenv("PROXY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("KILO_CONFIG_PATH", str(tmp_path / "catalog.json"))
    sys.modules.pop("server", None)
    import server as cp

    with TestClient(cp.app) as c:
        r = c.get("/v1/models", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["type"] == "unauthorized"
        assert body["error"]["message"]
        assert body["error"]["request_id"]


def test_xff_ignored_unless_proxy_trusted(monkeypatch):
    """X-Forwarded-For must not let a caller pick its own rate-limit identity."""
    monkeypatch.setenv("PROXY_AUTH_TOKEN", TOKEN)
    sys.modules.pop("server", None)
    import server as cp

    class FakeReq:
        def __init__(self, xff, host="10.0.0.5"):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": host})()

    cp.TRUSTED_PROXIES = []
    assert cp._client_id(FakeReq("1.1.1.1")) == "10.0.0.5"
    assert cp._client_id(FakeReq("2.2.2.2")) == "10.0.0.5"

    cp.TRUSTED_PROXIES = ["10.0.0.5"]
    assert cp._client_id(FakeReq("1.1.1.1")) == "1.1.1.1"


def test_empty_catalog_removes_stale_file(monkeypatch, tmp_path):
    """An empty catalog must not leave previously-served models on disk."""
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps({"provider": {"customProxy": {"models": {"old/stale": {}}}}}),
                   encoding="utf-8")
    monkeypatch.setenv("PROXY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("KILO_CONFIG_PATH", str(cat))
    sys.modules.pop("server", None)
    import server as cp

    cp.providers.clear()
    assert cp._write_catalog() is True
    assert not cat.exists()
    assert cp._read_local_models() == {}


def test_quota_exhausted_403_reads_as_billing():
    """Some upstreams answer 403 'Insufficient user quota' instead of 402.

    The key rotates either way, but the kind is what reaches the operator and
    the logs, so a balance problem must not be reported as a permission one.
    """
    sys.modules.pop("server", None)
    import server as cp

    kind, status = cp._classify_failure(403, "Insufficient user quota, remaining: $0.00")
    assert (kind, status) == ("billing_required", 402)
    assert kind in cp._ROTATE_KINDS

    # A genuine permission error keeps its own label.
    assert cp._classify_failure(403, "You do not have access to this model") == ("forbidden", 403)


def test_free_only_provider_refuses_unseen_model():
    """free-only must not forward a model it never saw upstream.

    A mistyped or paid-but-unlisted id would otherwise be passed through, and
    the upstream answers 404/403 after the key has already been spent.
    """
    sys.modules.pop("server", None)
    import asyncio
    import server as cp

    prov = cp.Provider("p", {"base_url": "http://x/v1",
                             "keys": [{"label": "k", "key": "k"}],
                             "filter_free": True})
    asyncio.run(_load_with(prov, {"data": [
        {"id": "free-m", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "paid-m", "pricing": {"prompt": "0.01", "completion": "0.02"}},
    ]}))
    assert prov.allows("free-m")
    assert not prov.allows("paid-m")
    assert not prov.allows("never-seen-paid"), "unlisted id must be refused"


def test_client_errors_do_not_rotate_keys():
    """404/422 mean the request is wrong; another key cannot fix it."""
    sys.modules.pop("server", None)
    import server as cp

    for status in (400, 404, 405, 409, 413, 415, 422):
        kind, out = cp._classify_failure(status, "bad request")
        assert kind == "invalid_request", status
        assert kind not in cp._ROTATE_KINDS, status
        assert out == status
