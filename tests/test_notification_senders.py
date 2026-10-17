# coding=utf-8
"""各渠道发送器（HTTP / SMTP 全部 mock，不会发出任何真实请求）"""

import json
import logging

import pytest
import requests

from trendradar.notification import senders
from trendradar.notification.batch import utf16_len
from trendradar.notification.senders import (
    SMTP_CONFIGS,
    _parse_recipients,
    _resolve_smtp,
    parse_bark_url,
    render_webhook_payload,
    send_to_bark,
    send_to_dingtalk,
    send_to_email,
    send_to_feishu,
    send_to_generic_webhook,
    send_to_ntfy,
    send_to_slack,
    send_to_telegram,
    send_to_wework,
    telegram_html_to_plain,
)

from test_notification_helpers import (
    FakeResponse,
    ListHandler,
    make_report,
    make_title,
    splitter,
    telegram_html_errors,
)

TG_TOKEN = "123456789:AAH-secretTOKENvalue_xyz"


@pytest.fixture
def logs():
    handler = ListHandler()
    logger = logging.getLogger("trendradar")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(senders.time, "sleep", lambda *_: None)


class Recorder:
    """记录 requests.post 调用，并按顺序返回预设响应（或抛出异常）"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _common(**extra):
    kw = dict(split_content_func=splitter(), batch_interval=0)
    kw.update(extra)
    return kw


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


def test_telegram_payload_is_valid_html(monkeypatch):
    rec = Recorder(FakeResponse(200, {"ok": True}))
    monkeypatch.setattr(senders.requests, "post", rec)
    ok = send_to_telegram(TG_TOKEN, "42", make_report(), "当前榜单", **_common())
    assert ok
    for call in rec.calls:
        payload = call["json"]
        assert payload["parse_mode"] == "HTML"
        assert telegram_html_errors(payload["text"]) == []
        assert utf16_len(payload["text"]) <= 4096


def test_telegram_parse_error_falls_back_to_plain_text(monkeypatch):
    rec = Recorder(
        FakeResponse(400, {"ok": False, "description": "Bad Request: can't parse entities: unsupported tag"}),
        FakeResponse(200, {"ok": True}),
    )
    monkeypatch.setattr(senders.requests, "post", rec)
    ok = send_to_telegram(TG_TOKEN, "42", make_report(), "当前榜单", **_common())
    assert ok
    assert "parse_mode" in rec.calls[0]["json"]
    plain_call = rec.calls[1]["json"]
    assert "parse_mode" not in plain_call
    assert "<a href" not in plain_call["text"]
    assert "标普500 & 纳指 <新高>" in plain_call["text"]  # 实体已还原


def test_telegram_429_waits_and_retries(monkeypatch):
    slept = []
    monkeypatch.setattr(senders.time, "sleep", lambda s: slept.append(s))
    rec = Recorder(
        FakeResponse(429, {"ok": False, "parameters": {"retry_after": 7}}),
        FakeResponse(200, {"ok": True}),
    )
    monkeypatch.setattr(senders.requests, "post", rec)
    assert send_to_telegram(TG_TOKEN, "42", make_report(), "当前榜单", **_common())
    assert 7 in slept


def test_telegram_network_error_does_not_leak_token(monkeypatch, logs):
    err = requests.exceptions.ProxyError(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
        f"/bot{TG_TOKEN}/sendMessage (Caused by ProxyError('Unable to connect to proxy'))"
    )
    monkeypatch.setattr(senders.requests, "post", Recorder(err))
    assert send_to_telegram(TG_TOKEN, "-100987654321", make_report(), "当前榜单", **_common()) is False
    assert "secretTOKEN" not in logs.text
    assert TG_TOKEN not in logs.text
    assert "-100987654321" not in logs.text
    assert "ProxyError" in logs.text


def test_telegram_batch_size_clamped_to_4096_chars(monkeypatch):
    rec = Recorder(FakeResponse(200, {"ok": True}))
    monkeypatch.setattr(senders.requests, "post", rec)
    titles = [make_title(f"中文标题{i}" * 20) for i in range(80)]
    send_to_telegram(TG_TOKEN, "42", make_report(titles), "当前榜单", **_common(batch_size=10000))
    assert all(utf16_len(c["json"]["text"]) <= 4096 for c in rec.calls)


def test_telegram_html_to_plain():
    out = telegram_html_to_plain('<b>[1]</b> <a href="https://x.com/?a=1&amp;b=2">S&amp;P &lt;x&gt;</a>')
    assert out == "[1] S&P <x> (https://x.com/?a=1&b=2)"


# --------------------------------------------------------------------------
# 其它 IM / Webhook：异常与失败日志不泄露凭据
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "func,url,secret",
    [
        (send_to_feishu, "https://open.feishu.cn/open-apis/bot/v2/hook/feishu-secret-key-1", "feishu-secret-key-1"),
        (send_to_dingtalk, "https://oapi.dingtalk.com/robot/send?access_token=ding-secret-2", "ding-secret-2"),
        (send_to_wework, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wework-secret-3", "wework-secret-3"),
        (send_to_slack, "https://hooks.slack.com/services/T0/B0/slack-secret-4", "slack-secret-4"),
    ],
)
def test_webhook_errors_are_redacted(monkeypatch, logs, func, url, secret):
    path = url.split("://", 1)[1].split("/", 1)[1]
    err = requests.exceptions.ConnectionError(f"Max retries exceeded with url: /{path} (Caused by NewConnectionError)")
    monkeypatch.setattr(senders.requests, "post", Recorder(err))
    assert func(url, make_report(), "当前榜单", **_common()) is False
    assert secret not in logs.text
    assert "ConnectionError" in logs.text


def test_webhook_error_response_text_is_redacted(monkeypatch, logs):
    url = "https://example.com/hooks/very-secret-path-5"
    monkeypatch.setattr(
        senders.requests, "post",
        Recorder(FakeResponse(500, None, text=f"error for {url} body=" + "x" * 1000)),
    )
    assert send_to_generic_webhook(url, None, make_report(), "当前榜单", **_common()) is False
    assert "very-secret-path-5" not in logs.text
    assert max(len(m) for m in logs.messages) < 600


def test_fatal_status_stops_remaining_batches(monkeypatch):
    rec = Recorder(FakeResponse(404, None, text="not found"))
    monkeypatch.setattr(senders.requests, "post", rec)
    titles = [make_title(f"标题{i}" * 30) for i in range(60)]
    assert send_to_slack("https://hooks.slack.com/services/x", make_report(titles), "当前榜单", **_common()) is False
    assert len(rec.calls) == 1


def test_content_error_continues_and_reports_failure(monkeypatch):
    rec = Recorder(FakeResponse(200, {"errcode": 40058, "errmsg": "bad"}), FakeResponse(200, {"errcode": 0}))
    monkeypatch.setattr(senders.requests, "post", rec)
    titles = [make_title(f"标题{i}" * 30) for i in range(60)]
    ok = send_to_wework("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k", make_report(titles), "当前榜单",
                        **_common())
    assert ok is False          # 有一批失败 → 整体失败
    assert len(rec.calls) > 1   # 但后续批次仍然发送


def test_wework_text_mode_respects_2048_bytes(monkeypatch):
    rec = Recorder(FakeResponse(200, {"errcode": 0}))
    monkeypatch.setattr(senders.requests, "post", rec)
    titles = [make_title(f"标题{i}" * 10) for i in range(40)]
    send_to_wework("https://qyapi.weixin.qq.com/x?key=k", make_report(titles), "当前榜单",
                   **_common(msg_type="text", batch_size=4000))
    assert all(len(c["json"]["text"]["content"].encode()) <= 2048 for c in rec.calls)


def test_feishu_card_has_no_at_all_injection(monkeypatch):
    rec = Recorder(FakeResponse(200, {"code": 0}))
    monkeypatch.setattr(senders.requests, "post", rec)
    send_to_feishu("https://open.feishu.cn/open-apis/bot/v2/hook/k", make_report(), "当前榜单", **_common())
    body = json.dumps([c["json"] for c in rec.calls], ensure_ascii=False)
    assert "<at id=all>" not in body


# --------------------------------------------------------------------------
# ntfy
# --------------------------------------------------------------------------


def test_ntfy_topic_url_encoded_and_partial_failure_is_false(monkeypatch, logs):
    rec = Recorder(FakeResponse(200, None, text="{}"), FakeResponse(500, None, text="oops"))
    monkeypatch.setattr(senders.requests, "post", rec)
    titles = [make_title(f"标题{i}" * 30) for i in range(40)]
    ok = send_to_ntfy("ntfy.example.com/", "my topic/../x?y", "tk_secret", make_report(titles), "当前榜单",
                      split_content_func=splitter())
    assert rec.calls[0]["url"] == "https://ntfy.example.com/my%20topic%2F..%2Fx%3Fy"
    assert rec.calls[0]["headers"]["Authorization"] == "Bearer tk_secret"
    assert ok is False
    assert "tk_secret" not in logs.text
    assert all(len(c["data"]) <= 4096 for c in rec.calls)


def test_ntfy_without_server_skips(monkeypatch):
    rec = Recorder(FakeResponse(200))
    monkeypatch.setattr(senders.requests, "post", rec)
    assert send_to_ntfy("", "t", None, make_report(), "当前榜单", split_content_func=splitter()) is False
    assert rec.calls == []


# --------------------------------------------------------------------------
# Bark
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,endpoint,key",
    [
        ("https://api.day.app/KEY123", "https://api.day.app/push", "KEY123"),
        ("https://api.day.app/KEY123/", "https://api.day.app/push", "KEY123"),
        ("https://host.example/bark/KEY9", "https://host.example/bark/push", "KEY9"),
        ("https://host.example/", None, None),
        ("not a url", None, None),
    ],
)
def test_parse_bark_url(url, endpoint, key):
    assert parse_bark_url(url) == (endpoint, key)


def test_bark_payload_within_apns_limit_and_no_key_in_logs(monkeypatch, logs):
    rec = Recorder(FakeResponse(200, {"code": 200}))
    monkeypatch.setattr(senders.requests, "post", rec)
    titles = [make_title(f"标题{i}" * 30) for i in range(40)]
    ok = send_to_bark("https://api.day.app/DEVKEY777", make_report(titles), "当前榜单",
                      **_common(batch_size=4000))
    assert ok
    for c in rec.calls:
        assert len(json.dumps(c["json"], ensure_ascii=False).encode()) <= 4096
    assert "DEVKEY777" not in logs.text


def test_bark_bad_url_logged_masked(logs):
    assert send_to_bark("ftp://x/KEY", make_report(), "当前榜单", **_common()) is False
    assert "KEY" not in logs.text.replace("device_key", "")


# --------------------------------------------------------------------------
# 通用 Webhook
# --------------------------------------------------------------------------


def test_template_single_pass_replacement():
    payload = render_webhook_payload('{"t": "{title}", "c": "{content}"}', "标题", '正文含 {title} 和 "引号"')
    assert payload == {"t": "标题", "c": '正文含 {title} 和 "引号"'}


def test_invalid_template_falls_back():
    assert render_webhook_payload("{not json", "T", "C") == {"title": "T", "content": "C"}


def test_discord_disables_mentions(monkeypatch):
    rec = Recorder(FakeResponse(204))
    monkeypatch.setattr(senders.requests, "post", rec)
    ok = send_to_generic_webhook(
        "https://discord.com/api/webhooks/1/tok", '{"content": "{content}"}', make_report(), "当前榜单",
        **_common(batch_size=1900),
    )
    assert ok
    for c in rec.calls:
        assert c["json"]["allowed_mentions"] == {"parse": []}
        assert "@everyone" not in c["json"]["content"]


def test_generic_webhook_respects_batch_size_including_template(monkeypatch):
    rec = Recorder(FakeResponse(200))
    monkeypatch.setattr(senders.requests, "post", rec)
    template = '{"msgtype": "markdown", "markdown": {"title": "{title}", "text": "{content}"}, "pad": "' + "p" * 300 + '"}'
    titles = [make_title(f"标题{i}" * 30) for i in range(40)]
    send_to_generic_webhook("https://hook.example/x", template, make_report(titles), "当前榜单",
                            **_common(batch_size=2000))
    for c in rec.calls:
        assert len(json.dumps(c["json"], ensure_ascii=False).encode()) <= 2000 + 64


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain,port,encryption",
    [("aliyun.com", 465, "SSL"), ("yandex.com", 465, "SSL"), ("yandex.ru", 465, "SSL"),
     ("icloud.com", 587, "TLS"), ("gmail.com", 587, "TLS"), ("qq.com", 465, "SSL")],
)
def test_smtp_presets(domain, port, encryption):
    cfg = SMTP_CONFIGS[domain]
    assert (cfg["port"], cfg["encryption"]) == (port, encryption)


def test_all_presets_port_matches_encryption():
    for domain, cfg in SMTP_CONFIGS.items():
        assert (cfg["port"] == 465) == (cfg["encryption"] == "SSL"), domain


def test_resolve_smtp_custom_port():
    assert _resolve_smtp("a@x.com", "smtp.x.com", "465") == ("smtp.x.com", 465, False)
    assert _resolve_smtp("a@x.com", "smtp.x.com", 587) == ("smtp.x.com", 587, True)
    assert _resolve_smtp("a@yandex.com", None, None) == ("smtp.yandex.com", 465, False)


def test_parse_recipients_accepts_comma_and_semicolon():
    assert _parse_recipients("a@x.com, b@y.com;c@z.com;") == ["a@x.com", "b@y.com", "c@z.com"]


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls = []
        self.closed = False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")
        if password == "bad":
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append(("send", tuple(to_addrs)))


@pytest.fixture
def fake_smtp(monkeypatch, tmp_path):
    _FakeSMTP.instances = []

    class SSL(_FakeSMTP):
        kind = "SSL"

    class Plain(_FakeSMTP):
        kind = "PLAIN"

    monkeypatch.setattr(senders.smtplib, "SMTP_SSL", SSL)
    monkeypatch.setattr(senders.smtplib, "SMTP", Plain)
    html_file = tmp_path / "r.html"
    html_file.write_text("<html>ok</html>", encoding="utf-8")
    return str(html_file)


def test_email_yandex_uses_implicit_ssl(fake_smtp):
    assert send_to_email("me@yandex.com", "pw", "a@x.com;b@y.com", "当前榜单", fake_smtp)
    server = _FakeSMTP.instances[0]
    assert server.kind == "SSL" and server.port == 465
    assert "starttls" not in server.calls
    assert ("send", ("a@x.com", "b@y.com")) in server.calls
    assert server.closed


def test_email_icloud_uses_starttls(fake_smtp):
    assert send_to_email("me@icloud.com", "pw", "a@x.com", "当前榜单", fake_smtp)
    server = _FakeSMTP.instances[0]
    assert server.kind == "PLAIN" and server.port == 587
    assert "starttls" in server.calls


def test_email_connection_closed_on_error(fake_smtp, logs):
    assert send_to_email("me@gmail.com", "bad", "a@x.com", "当前榜单", fake_smtp) is False
    assert _FakeSMTP.instances[0].closed
    assert "认证错误" in logs.text


def test_email_missing_file(logs):
    assert send_to_email("me@gmail.com", "pw", "a@x.com", "当前榜单", "/nonexistent.html") is False
