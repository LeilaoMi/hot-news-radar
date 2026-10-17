# coding=utf-8
"""
通知渠道转义与脱敏工具

- escape_text: 按渠道语法转义外部文本（标题、来源名、关键词等）
- safe_url:    只放行 http(s) 链接，并对会破坏 markdown / HTML 语法的字符做百分号编码
- redact:      日志脱敏，去掉 bot token、webhook key、query 中的凭据等
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote, urlsplit

# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------

_SAFE_SCHEMES = {"http", "https"}
_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# 保留 URL 结构字符和已有的百分号编码；其余（空格、引号、括号、尖括号、| 、反引号、非 ASCII）全部编码
_URL_SAFE_CHARS = ":/?#@!$&*+,;=%~-._"


def safe_url(url: Any) -> str:
    """返回可安全嵌入推送消息的 http(s) URL；不安全或无效时返回空串。

    - 去掉控制字符（防止 ``java\\tscript:`` 一类绕过）
    - 只允许 http / https，且必须有主机名
    - 对 ``( ) [ ] < > " ' | ` 空格`` 等会破坏 markdown 链接 / HTML 属性 / Slack 链接的字符做百分号编码
    """
    if not url:
        return ""
    u = _CTRL_CHARS.sub("", str(url)).strip()
    if not u:
        return ""
    try:
        parts = urlsplit(u)
    except ValueError:
        return ""
    if parts.scheme.lower() not in _SAFE_SCHEMES or not parts.netloc:
        return ""
    return quote(u, safe=_URL_SAFE_CHARS)


# ---------------------------------------------------------------------------
# 文本转义
# ---------------------------------------------------------------------------

# 群体 @ 关键字：插入零宽空格使其失效（Discord @everyone/@here，部分 IM 的 @all/@所有人）
_MENTION_RE = re.compile(r"@(everyone|here|all|channel|所有人)(?![A-Za-z0-9_])", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

# markdown 系渠道（钉钉/企微/ntfy/Bark/通用 Webhook）：方括号会破坏 [text](url)，
# 尖括号会被企微/钉钉当作 <font>/<@userid> 等标签解析 → 换成外观接近的全角字符
_MARKDOWN_TRANSLATE = str.maketrans({"[": "［", "]": "］", "<": "＜", ">": "＞"})

MARKDOWN_FORMATS = {"dingtalk", "wework", "bark", "ntfy", "generic", "generic_webhook"}


def neutralize_mentions(text: str) -> str:
    """让 @everyone / @here / @all / @所有人 等群体提醒失效"""
    return _MENTION_RE.sub(lambda m: "@\u200b" + m.group(1), text)


def escape_text(text: Any, format_type: str) -> str:
    """按渠道语法转义一段外部文本（单行）。

    Args:
        text: 原始文本（标题、来源名、关键词、作者、版本号……）
        format_type: telegram / slack / feishu / dingtalk / wework / bark / ntfy / wework_text / plain

    Returns:
        可以直接拼进对应渠道消息体的文本；换行被压成空格
    """
    if text is None:
        return ""
    s = _WS_RE.sub(" ", str(text)).strip()
    if not s:
        return ""

    if format_type == "telegram":
        # parse_mode=HTML：& < > " ' 全部转义
        return html.escape(s, quote=True)

    if format_type == "slack":
        # Slack mrkdwn 只要求转义 & < >（<!channel>、<url|text> 均由尖括号触发）
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return neutralize_mentions(s)

    if format_type == "feishu":
        # 飞书卡片 markdown 支持数字实体（现有代码即用 &#91;来源&#93;）：
        # 尖括号转义可阻止 <at id=all></at>、<font> 注入；方括号转义避免破坏 [text](url)
        s = s.replace("<", "&#60;").replace(">", "&#62;").replace("[", "&#91;").replace("]", "&#93;")
        return neutralize_mentions(s)

    if format_type in MARKDOWN_FORMATS:
        return neutralize_mentions(s.translate(_MARKDOWN_TRANSLATE))

    # wework_text / plain / 未知：纯文本，仅处理群体 @
    return neutralize_mentions(s)


# ---------------------------------------------------------------------------
# 日志脱敏
# ---------------------------------------------------------------------------

_REDACT_PATTERNS = [
    # Telegram: /bot123456:ABC-xyz/sendMessage
    (re.compile(r"/bot\d+:[A-Za-z0-9_\-]+"), "/bot***"),
    (re.compile(r"\bbot\d{5,}:[A-Za-z0-9_\-]{10,}"), "bot***"),
    # 飞书 / Lark: /open-apis/bot/v2/hook/<key>
    (re.compile(r"(/hook/)[A-Za-z0-9_\-]+"), r"\1***"),
    # Slack: /services/T000/B000/XXXX
    (re.compile(r"(/services/)[A-Za-z0-9_\-/]+"), r"\1***"),
    # Discord: /api/webhooks/<id>/<token>
    (re.compile(r"(/webhooks/)[A-Za-z0-9_\-/]+"), r"\1***"),
    # query 中的凭据：access_token= / key= / token= / sign= / secret=
    (
        re.compile(r"((?:access_token|token|key|sign|secret|password|device_key)=)[^&\s'\"]+", re.IGNORECASE),
        r"\1***",
    ),
    # Authorization: Bearer xxx
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=]+", re.IGNORECASE), r"\1***"),
]

_URL_RE = re.compile(r"https?://[^\s'\"<>)]+")


def mask_url(url: Any) -> str:
    """只保留 scheme://host，路径和参数一律隐藏（用于日志）"""
    if not url:
        return ""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "***"
    if not parts.netloc:
        return "***"
    host = parts.hostname or "***"
    tail = "/***" if (parts.path not in ("", "/") or parts.query) else ""
    return f"{parts.scheme}://{host}{tail}"


def redact(message: Any, *secrets: Any) -> str:
    """日志脱敏：去掉已知凭据格式，以及显式传入的 secret 值。

    Args:
        message: 原始消息（异常、响应文本等）
        *secrets: 需要额外隐藏的原值（如 webhook URL、token、topic），空值自动忽略
    """
    s = str(message) if message is not None else ""
    for secret in secrets:
        if secret and len(str(secret)) >= 4:
            s = s.replace(str(secret), "***")
    # 完整 URL → 只留主机名（requests 异常里通常带完整 URL）
    s = _URL_RE.sub(lambda m: mask_url(m.group(0)), s)
    for pattern, repl in _REDACT_PATTERNS:
        s = pattern.sub(repl, s)
    return s


def describe_error(exc: BaseException, *secrets: Any, limit: int = 300) -> str:
    """把异常转成脱敏后的简短描述，用于日志"""
    text = redact(f"{type(exc).__name__}: {exc}", *secrets)
    return text if len(text) <= limit else text[:limit] + "..."


def short_response(text: Any, *secrets: Any, limit: int = 200) -> str:
    """脱敏并截断第三方响应文本（有些服务会回显请求内容）"""
    s = redact(text or "", *secrets)
    return s if len(s) <= limit else s[:limit] + "..."
