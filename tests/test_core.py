import json

import server as cp


def test_is_free_model():
    assert cp._is_free_model({"pricing": {"prompt": "0", "completion": "0"}}) is True
    assert cp._is_free_model({"pricing": {"prompt": "1", "completion": "0"}}) is False
    assert cp._is_free_model({"pricing": {"prompt": "0"}}) is None
    assert cp._is_free_model({"isFree": True}) is True


def test_modalities_and_category():
    m = {"architecture": {"modality": "text+image->text"}}
    mods = cp._modalities(m)
    assert mods == {"input": ["text", "image"], "output": ["text"]}
    meta = cp._upstream_meta({**m, "pricing": {"prompt": "0", "completion": "0"}})
    assert meta["capabilities"]["vision"] is True
    assert cp._categorize(meta["capabilities"], meta["modalities"]) == "vision"

    emb = cp._upstream_meta({"architecture": {"modality": "text->embedding"},
                             "pricing": {"prompt": "0", "completion": "0"}})
    assert cp._categorize(emb["capabilities"], emb["modalities"]) == "embedding"


def test_matches_globs_and_regex():
    assert cp._matches("deepseek-v3", ["deepseek-*"])
    assert cp._matches("meta/llama", ["re:^meta/"])
    assert not cp._matches("paid-model", ["*:free"])


def test_parse_env_pool_labels():
    got = cp._parse_env_pool("a=key1, key2, b=sk-x==", "p")
    assert got == [
        {"key": "key1", "label": "a"},
        {"key": "key2", "label": "p-env2"},
        {"key": "sk-x==", "label": "b"},
    ]


def test_keypool_labels_and_cooldown():
    pool = cp.KeyPool([{"key": "k1", "label": "same"}, {"key": "k2", "label": "same"}])
    assert [k["label"] for k in pool.keys] == ["same", "same#2"]
    pool.note_failure("same", 401)
    assert pool.is_cooling("same")
    first = pool.cooldown_until["same"]
    pool.note_failure("same", 401)
    assert pool.cooldown_until["same"] > first
    pool.note_success("same")
    assert not pool.is_cooling("same") and pool.fail_streak["same"] == 0


def test_provider_allows_filters():
    p = cp.Provider("p", {"base_url": "http://x/v1", "keys": [{"key": "k"}],
                          "include_models": ["*:free", "re:^meta/"],
                          "exclude_models": ["*paid*"]})
    assert p.allows("meta/vision:free")
    assert p.allows("meta/other")
    assert p.allows("other/model:free")      # matches the *:free include
    assert not p.allows("other/model")       # neither include pattern
    assert not p.allows("meta/paid-x")       # excluded


def test_classify_failure():
    assert cp._classify_failure(402, "")[0] == "billing_required"
    assert cp._classify_failure(400, "Insufficient credit balance")[0] == "billing_required"
    assert cp._classify_failure(401, "invalid api key")[0] == "auth_error"
    assert cp._classify_failure(429, "rate limit")[0] == "rate_limited"
    assert cp._classify_failure(400, "context length exceeded")[0] == "context_length"
    assert cp._classify_failure(500, "boom")[0] == "upstream_error"


def test_extract_error_message():
    raw = json.dumps({"error": {"message": "Insufficient credit balance."}}).encode()
    assert cp._extract_error_message(raw) == "Insufficient credit balance."
    assert cp._extract_error_message(b"plain failure") == "plain failure"


def test_redact_secrets():
    out = cp._redact("bad sk-ABCDEFGH12345678 rejected")
    assert "sk-ABCDEFGH12345678" not in out and "sk-***" in out


def test_error_response_retry_after():
    resp = cp._error_response("slow down", "rate_limited", 429, "rid", retry_after=20)
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "20"
    body = json.loads(resp.body)
    assert body["error"]["type"] == "rate_limited"
    assert body["error"]["request_id"] == "rid"


def test_upstream_success_metadata():
    meta = cp._upstream_meta({"id": "m", "context_length": 128000,
                              "pricing": {"prompt": "0", "completion": "0"},
                              "architecture": {"modality": "text->text"}})
    assert meta["limit"]["context"] == 128000
    assert meta["free"] is True


def test_as_list_coerces_bare_string():
    assert cp._as_list("foo") == ["foo"]
    assert cp._as_list(["a", " ", "b"]) == ["a", "b"]
    assert cp._as_list(None) == []
    assert cp._as_list(5) == ["5"]


def test_filter_matches_upstream_or_catalog_id():
    p = cp.Provider("xy", {"base_url": "http://x/v1", "keys": [{"key": "k"}],
                           "exclude_models": ["xy/some-model"]})
    p.alias_to_id = {"some-model": "some-model"}
    p.model_ids = ["some-model"]
    assert not p.allows("some-model")          # upstream id form
    assert not p.allows("xy/some-model")       # catalog id form
    assert p.allows("other-model")


def test_bare_string_filter_does_not_explode_into_chars():
    p = cp.Provider("xy", {"base_url": "http://x/v1", "keys": [{"key": "k"}],
                           "exclude_models": "xy/blocked"})
    p.alias_to_id = {"blocked": "blocked"}
    assert p.exclude_models == ["xy/blocked"]
    assert not p.allows("blocked")


def test_manual_models_accept_string_or_dict():
    p = cp.Provider("xy", {"base_url": "http://x/v1", "keys": [{"key": "k"}],
                           "manual_models": "xy/only-one"})
    assert p.manual_ids == ["xy/only-one"] and p.allows("xy/only-one")
    q = cp.Provider("xy", {"base_url": "http://x/v1", "keys": [{"key": "k"}],
                           "manual_models": {"id": "xy/pinned", "context": 4096}})
    assert q.manual_ids == ["xy/pinned"]
    assert q.model_meta["xy/pinned"]["limit"]["context"] == 4096
