"""返回给 MCP 客户端的错误信息不能泄露 webhook URL / token；Email HTML 链接安全；飞书纯文本。"""
import pytest
import requests

from mcp_server.tools import notification as notif
from mcp_server.tools.system import SystemManagementTools
from mcp_server.utils.redact import collect_secrets, redact, redact_obj, safe_error_message

TG_TOKEN = "123456789:AAHfakeTelegramTokenValue_abcdefghij"
FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/11111111-2222-3333-4444-555555555555"
SLACK = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXX"
DINGTALK = "https://oapi.dingtalk.com/robot/send?access_token=deadbeefcafebabe1234"
BARK = "https://api.day.app/myDeviceKey123456/"


def test_redact_known_secret_values():
    secrets = collect_secrets({"FEISHU_WEBHOOK_URL": FEISHU, "TELEGRAM_BOT_TOKEN": TG_TOKEN}, include_env=False)
    text = f"failed to post {FEISHU} and https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    out = redact(text, secrets)
    assert FEISHU not in out and TG_TOKEN not in out
    assert "***" in out


@pytest.mark.parametrize("raw,leak", [
    (f"Max retries exceeded with url: /bot{TG_TOKEN}/sendMessage", TG_TOKEN),
    (f"POST {SLACK} failed", "XXXXXXXXXXXXXXXX"),
    (f"POST {FEISHU} failed", "11111111-2222-3333-4444-555555555555"),
    (f"POST {DINGTALK} failed", "deadbeefcafebabe1234"),
    (f"GET {BARK}title/body failed", "myDeviceKey123456"),
    ("https://user:hunter2pass@smtp.example.com", "hunter2pass"),
    ("headers={'Authorization': 'Bearer sk-abcdefghijklmnop'}", "sk-abcdefghijklmnop"),
])
def test_redact_fallback_patterns(raw, leak):
    assert leak not in redact(raw)


def test_multi_account_values_split():
    secrets = collect_secrets({"SLACK_WEBHOOK_URL": f"{SLACK};https://hooks.example.com/abcdef123"}, include_env=False)
    assert "https://hooks.example.com/abcdef123" in secrets
    assert "https://hooks.example.com/abcdef123" not in redact("x https://hooks.example.com/abcdef123 y", secrets)


def test_public_values_not_collected():
    secrets = collect_secrets({"NTFY_SERVER_URL": "https://ntfy.sh", "MAX_TOKENS": 5000}, include_env=False)
    assert secrets == []


def test_redact_obj_recursive():
    secrets = [TG_TOKEN]
    obj = {"a": [f"x{TG_TOKEN}"], "b": {"c": TG_TOKEN}, "n": 1}
    out = redact_obj(obj, secrets)
    assert TG_TOKEN not in str(out) and out["n"] == 1


def test_safe_error_message_type_and_truncation():
    msg = safe_error_message(ValueError("x" * 1000))
    assert msg.startswith("ValueError: ") and len(msg) < 400


def _urllib3_style_error(url_path: str) -> requests.ConnectionError:
    return requests.ConnectionError(
        "HTTPSConnectionPool(host='api.telegram.org', port=443): "
        f"Max retries exceeded with url: {url_path} (Caused by NameResolutionError)"
    )


def test_send_telegram_error_masked(monkeypatch):
    def boom(url, *a, **kw):
        raise _urllib3_style_error(f"/bot{TG_TOKEN}/sendMessage")

    monkeypatch.setattr(notif.requests, "post", boom)
    r = notif._send_telegram(TG_TOKEN, "42", "hi", "t")
    assert r["success"] is False
    assert TG_TOKEN not in r["detail"]


def test_send_notification_end_to_end_masked(monkeypatch, tmp_path):
    """完整走 send_notification：渠道失败时返回结果里不能出现 webhook / token"""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("notification:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", FEISHU)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TG_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", SLACK)

    def boom(url, *a, **kw):
        raise _urllib3_style_error(url)

    monkeypatch.setattr(notif.requests, "post", boom)
    monkeypatch.setattr(notif.time, "sleep", lambda *_: None)

    tools = notif.NotificationTools(str(tmp_path))
    result = tools.send_notification("**hello**", channels=["feishu", "telegram", "slack"])
    text = str(result)
    assert result["success"] is False
    for leak in (FEISHU, TG_TOKEN, SLACK, "11111111-2222-3333-4444-555555555555", "XXXXXXXXXXXXXXXX"):
        assert leak not in text, leak


def test_bark_invalid_url_not_echoed():
    r = notif._send_bark("https://api.day.app/", "x", "t")
    assert r["success"] is False
    assert "api.day.app" not in r["detail"]


def test_email_html_blocks_attribute_injection_and_js():
    html = notif._markdown_to_simple_html('[x](https://a" onmouseover="alert(1)) [y](javascript:alert(1)) [ok](https://ok.example/p)')
    assert 'onmouseover="' not in html
    assert 'href="javascript:' not in html
    assert '<a href="https://ok.example/p">ok</a>' in html


def test_feishu_is_plain_text():
    out = notif._format_for_channel("# 标题\n**粗体** <font color='red'>红</font> [链接](https://e.com/a_b)", "feishu")
    assert "**" not in out and "<font" not in out and "#" not in out
    assert "https://e.com/a_b" in out and "粗体" in out and "红" in out
    assert "纯文本" in notif.CHANNEL_FORMAT_GUIDES["feishu"]["format"]


@pytest.mark.parametrize("proxy,ok", [
    ("http://127.0.0.1:7890", True),
    ("socks5://localhost:1080", True),
    ("http://[::1]:7890", True),
    ("http://10.0.0.1:3128", False),
    ("http://evil.example:22", False),
    ("file:///etc/passwd", False),
    ("", False),
])
def test_check_version_proxy_whitelist(proxy, ok):
    assert SystemManagementTools._is_allowed_proxy(proxy) is ok


def test_check_version_configured_proxy_allowed():
    assert SystemManagementTools._is_allowed_proxy("http://10.0.0.1:3128", "http://10.0.0.1:3128")


def test_article_reader_url_validation():
    from mcp_server.tools.article_reader import ArticleReaderTools
    from mcp_server.utils.errors import InvalidParameterError

    for bad in ("", "ftp://x", "http://", "javascript:alert(1)"):
        with pytest.raises(InvalidParameterError):
            ArticleReaderTools._validate_url(bad)
    assert ArticleReaderTools._validate_url("https://e.com/a b#c") == "https://e.com/a%20b%23c"


def test_article_reader_jina_key_from_env(monkeypatch):
    from mcp_server.tools.article_reader import ArticleReaderTools

    monkeypatch.setenv("JINA_API_KEY", "jina_test_key")
    t = ArticleReaderTools()
    assert t._build_headers()["Authorization"] == "Bearer jina_test_key"
