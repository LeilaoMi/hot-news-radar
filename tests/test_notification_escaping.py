# coding=utf-8
"""推送渠道转义、链接校验与日志脱敏"""

import pytest

from trendradar.notification.escaping import (
    describe_error,
    escape_text,
    mask_url,
    neutralize_mentions,
    redact,
    safe_url,
)
from trendradar.notification.formatters import format_link, format_title_for_platform

from test_notification_helpers import make_title, telegram_html_errors


# --------------------------------------------------------------------------
# safe_url
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "  JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "/relative/path",
        "//evil.example/x",
        "https://",
        "",
        None,
    ],
)
def test_safe_url_rejects_non_http(url):
    assert safe_url(url) == ""


def test_safe_url_encodes_markup_breaking_chars():
    out = safe_url('https://x.com/a b(c)[d]<e>"f\'|g`?q=1&r=%E4')
    assert out.startswith("https://x.com/")
    for ch in ' ()[]<>"\'|`':
        assert ch not in out
    assert "&r=%E4" in out  # 已有编码与 & 保持不变


def test_safe_url_keeps_normal_url():
    assert safe_url("https://www.zhihu.com/question/1?a=1&b=2#x") == "https://www.zhihu.com/question/1?a=1&b=2#x"


# --------------------------------------------------------------------------
# escape_text
# --------------------------------------------------------------------------


def test_escape_telegram_html():
    assert escape_text('a<b && "c"', "telegram") == "a&lt;b &amp;&amp; &quot;c&quot;"


def test_escape_slack_blocks_special_mentions():
    out = escape_text("<!channel> & <https://evil|click>", "slack")
    assert "<" not in out and ">" not in out
    assert "&lt;!channel&gt;" in out


def test_escape_feishu_blocks_at_all():
    out = escape_text("<at id=all></at> [x](y)", "feishu")
    assert "<at" not in out
    assert "[" not in out and "]" not in out


@pytest.mark.parametrize("fmt", ["dingtalk", "wework", "ntfy", "bark"])
def test_escape_markdown_brackets_and_tags(fmt):
    out = escape_text("[伪造](https://evil) <@user> <font color=red>", fmt)
    assert "[" not in out and "<" not in out


def test_neutralize_mentions():
    out = neutralize_mentions("@everyone @here @all @所有人 email@here.com")
    assert "@everyone" not in out and "@here " not in out and "@所有人" not in out
    assert "email@here.com" in out or "email@\u200bhere.com" in out


def test_escape_collapses_newlines():
    assert "\n" not in escape_text("a\nb\r\nc", "wework")


# --------------------------------------------------------------------------
# 标题格式化（推送侧）
# --------------------------------------------------------------------------


def test_telegram_title_without_link_is_escaped():
    out = format_title_for_platform("telegram", make_title("a<b && c", url=""), show_source=True)
    assert telegram_html_errors(out) == []
    assert "a&lt;b &amp;&amp; c" in out


def test_telegram_title_href_quote_cannot_break_out():
    out = format_title_for_platform(
        "telegram", make_title('引号"标题', url='https://www.zhihu.com/x"onmouseover=1'), show_source=True
    )
    assert telegram_html_errors(out) == []
    assert 'x"onmouseover' not in out
    assert "x%22onmouseover=1" in out


def test_telegram_keyword_and_source_escaped():
    out = format_title_for_platform(
        "telegram", make_title("t", source="来源<x>"), show_source=False, show_keyword=True
    )
    assert telegram_html_errors(out) == []
    out2 = format_title_for_platform("telegram", make_title("t", source="来源<x>&"), show_source=True)
    assert telegram_html_errors(out2) == []


@pytest.mark.parametrize("fmt", ["telegram", "slack", "feishu", "dingtalk", "wework", "bark", "ntfy"])
def test_javascript_link_dropped_everywhere(fmt):
    out = format_title_for_platform(fmt, make_title("标题", url="javascript:alert(1)"), show_source=False)
    assert "javascript" not in out.lower()
    assert "标题" in out


def test_markdown_title_brackets_do_not_break_link():
    out = format_link("dingtalk", "[伪造](https://evil)", "https://ok.example/a(b)")
    assert out.count("](") == 1
    assert out.endswith("(https://ok.example/a%28b%29)")


def test_slack_link_format():
    out = format_link("slack", "a|b <c>", "https://x.com/?q=a|b")
    assert out.startswith("<https://x.com/?q=a%7Cb|")
    assert "<c>" not in out


# --------------------------------------------------------------------------
# 日志脱敏
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,secret",
    [
        ("Max retries exceeded with url: /bot123456:SECRET_TOKEN_ABC/sendMessage", "SECRET_TOKEN_ABC"),
        ("url: /open-apis/bot/v2/hook/abcd-efgh-ijkl (Caused by ProxyError)", "abcd-efgh-ijkl"),
        ("url: /robot/send?access_token=deadbeef1234", "deadbeef1234"),
        ("url: /cgi-bin/webhook/send?key=wxkey-9999", "wxkey-9999"),
        ("url: /services/T000/B000/XXXXSECRET", "XXXXSECRET"),
        ("url: /api/webhooks/123/discordtoken", "discordtoken"),
        ("GET https://api.day.app/DEVICEKEY123/push failed", "DEVICEKEY123"),
        ("Authorization: Bearer tk_abcdef", "tk_abcdef"),
    ],
)
def test_redact_known_formats(message, secret):
    assert secret not in redact(message)


def test_redact_explicit_secret_values():
    assert "my-private-topic" not in redact("url: /my-private-topic", "my-private-topic")


def test_mask_url_keeps_only_host():
    assert mask_url("https://open.feishu.cn/open-apis/bot/v2/hook/key") == "https://open.feishu.cn/***"


def test_describe_error_truncates_and_redacts():
    exc = ConnectionError("x" * 1000 + " /bot1:TOKENTOKENTOKEN/")
    out = describe_error(exc)
    assert len(out) <= 304
    assert out.startswith("ConnectionError:")
