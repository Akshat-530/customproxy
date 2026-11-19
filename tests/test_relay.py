import os

os.environ["TAVILY_API_KEYS"] = "a=key1,b=key2,key3"

import key_rotate_proxy as kr


def test_parse_pool_strips_labels():
    assert kr._parse_pool("a=key1,b=key2,key3") == ["key1", "key2", "key3"]
    assert kr._parse_pool("") == []


def test_should_rotate():
    assert kr._should_rotate(401)
    assert kr._should_rotate(429)
    assert kr._should_rotate(500)
    assert not kr._should_rotate(400)
    assert not kr._should_rotate(404)


def test_rotate_cools_current_key():
    kr._cool.clear()
    kr._idx = 0
    kr._rotate(401)
    assert kr._keys[0] in kr._cool
