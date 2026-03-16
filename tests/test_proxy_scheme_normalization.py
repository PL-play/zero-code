import os

from llm_client.llm_factory import _normalize_proxy_env_inplace, _normalize_proxy_url_scheme


def test_normalize_proxy_url_scheme_socks_to_socks5():
    assert _normalize_proxy_url_scheme("socks://127.0.0.1:7897/") == "socks5://127.0.0.1:7897/"


def test_normalize_proxy_url_scheme_leaves_http_unchanged():
    assert _normalize_proxy_url_scheme("http://127.0.0.1:7890") == "http://127.0.0.1:7890"


def test_normalize_proxy_env_inplace_updates_proxy_vars(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897/")
    monkeypatch.setenv("HTTPS_PROXY", "socks://127.0.0.1:7897/")

    _normalize_proxy_env_inplace()

    assert os.getenv("ALL_PROXY") == "socks5://127.0.0.1:7897/"
    assert os.getenv("HTTPS_PROXY") == "socks5://127.0.0.1:7897/"
