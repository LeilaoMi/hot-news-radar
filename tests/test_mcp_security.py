"""MCP HTTP 安全：默认监听地址、Bearer Token、Host/Origin 校验（DNS rebinding）。"""
import warnings

import pytest

from mcp_server import server as srv
from mcp_server.security import (
    DEFAULT_HTTP_HOST,
    BearerTokenMiddleware,
    InsecureBindError,
    http_transport_kwargs,
    is_loopback_host,
    resolve_http_security,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

TOKEN = "t0ken-for-tests-0123456789"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("MCP_AUTH_TOKEN", "MCP_ALLOWED_HOSTS", "MCP_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)


def _client(token=None, allowed_hosts=None, allowed_origins=None):
    cfg = resolve_http_security(
        "127.0.0.1", token=token, allowed_hosts=allowed_hosts or [], allowed_origins=allowed_origins or []
    )
    app = srv.mcp.http_app(path="/mcp", **http_transport_kwargs(cfg))
    return TestClient(app, base_url="http://127.0.0.1:3333")


# ---------- 默认值与启动检查 ----------

def test_default_host_is_loopback():
    assert DEFAULT_HTTP_HOST == "127.0.0.1"
    args = srv.build_arg_parser().parse_args(["--transport", "http"])
    assert args.host == "127.0.0.1"
    assert args.port == 3333


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("127.1.2.3", True), ("localhost", True), ("::1", True), ("[::1]", True),
    ("0.0.0.0", False), ("::", False), ("192.168.1.10", False), ("example.com", False), ("", False),
])
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


def test_non_loopback_without_token_refuses():
    with pytest.raises(InsecureBindError):
        resolve_http_security("0.0.0.0", token="")


def test_non_loopback_with_token_ok(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    cfg = resolve_http_security("0.0.0.0")
    assert cfg.auth_enabled and cfg.token == TOKEN


def test_short_token_rejected():
    with pytest.raises(InsecureBindError):
        resolve_http_security("127.0.0.1", token="short")


def test_loopback_without_token_allowed():
    cfg = resolve_http_security("127.0.0.1", token="")
    assert not cfg.auth_enabled


def test_allowed_hosts_from_env(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com, 10.0.0.5")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://app.example.com")
    cfg = resolve_http_security("127.0.0.1", token=TOKEN)
    assert cfg.allowed_hosts == ["mcp.example.com", "10.0.0.5"]
    assert cfg.allowed_origins == ["https://app.example.com"]


def test_main_refuses_insecure_bind(capsys):
    rc = srv.main(["--transport", "http", "--host", "0.0.0.0", "--port", "39999"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "MCP_AUTH_TOKEN" in captured.err
    assert captured.out == ""


def test_http_kwargs_enable_host_origin_protection():
    cfg = resolve_http_security("127.0.0.1", token=TOKEN, allowed_hosts=[], allowed_origins=[])
    kw = http_transport_kwargs(cfg)
    assert kw["host_origin_protection"] is True
    assert any(m.cls is BearerTokenMiddleware for m in kw["middleware"])


# ---------- 端到端（in-process ASGI） ----------

def test_bearer_token_required():
    with _client(token=TOKEN) as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        bad = {**HEADERS, "Authorization": "Bearer wrong-token-0123456789"}
        assert c.post("/mcp", json=INIT, headers=bad).status_code == 401
        basic = {**HEADERS, "Authorization": f"Basic {TOKEN}"}
        assert c.post("/mcp", json=INIT, headers=basic).status_code == 401
        ok = {**HEADERS, "Authorization": f"Bearer {TOKEN}"}
        r = c.post("/mcp", json=INIT, headers=ok)
        assert r.status_code == 200
        assert "trendradar-news" in r.text


def test_rejects_rebinding_host_header():
    with _client(token=TOKEN) as c:
        h = {**HEADERS, "Authorization": f"Bearer {TOKEN}", "Host": "attacker.example:3333"}
        assert c.post("/mcp", json=INIT, headers=h).status_code == 421


def test_rejects_foreign_origin():
    with _client(token=TOKEN) as c:
        h = {**HEADERS, "Authorization": f"Bearer {TOKEN}", "Origin": "http://attacker.example"}
        assert c.post("/mcp", json=INIT, headers=h).status_code == 403


def test_rebinding_blocked_even_without_token():
    with _client(token=None) as c:
        h = {**HEADERS, "Host": "attacker.example:3333", "Origin": "http://attacker.example:3333"}
        assert c.post("/mcp", json=INIT, headers=h).status_code == 421
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 200


def test_allowed_host_passes():
    with _client(token=TOKEN, allowed_hosts=["mcp.example.com"]) as c:
        h = {**HEADERS, "Authorization": f"Bearer {TOKEN}", "Host": "mcp.example.com"}
        assert c.post("/mcp", json=INIT, headers=h).status_code == 200


def test_server_info_version_matches_package():
    from mcp_server import __version__

    with _client(token=None) as c:
        r = c.post("/mcp", json=INIT, headers=HEADERS)
        assert f'"version":"{__version__}"' in r.text.replace(" ", "")
