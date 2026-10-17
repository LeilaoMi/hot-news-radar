# coding=utf-8
"""
敏感信息脱敏工具

返回给 MCP 客户端（也就是 LLM 和对话记录）的错误信息里，绝不能出现 webhook URL、
bot token、密码、S3 密钥等凭据。requests/urllib3/boto3 的异常字符串经常带完整 URL，
所以所有错误信息在返回前都要经过 ``redact``。
"""

import os
import re
from typing import Any, Iterable, Mapping, Optional

MASK = "***"

# 配置键名里出现这些片段就视为敏感值
_SENSITIVE_KEY_PARTS = ("URL", "TOKEN", "PASSWORD", "SECRET", "KEY", "WEBHOOK")
# 这些键虽然匹配上面的片段，但值是公开的，不需要脱敏
_NON_SECRET_KEYS = {
    "NTFY_SERVER_URL_DEFAULT",
    "VERSION_CHECK_URL",
    "CONFIGS_VERSION_CHECK_URL",
    "MAX_NEWS_PER_KEYWORD",
    "MAX_TOKENS",
}
_PUBLIC_VALUES = {"https://ntfy.sh", "https://ntfy.sh/"}

# 兜底正则：即使不知道具体密钥，也能遮住常见凭据格式
_FALLBACK_PATTERNS = [
    # Telegram bot token：bot123456:AAH...
    (re.compile(r"(bot)\d{5,}:[A-Za-z0-9_-]{20,}"), r"\1" + MASK),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), MASK),
    # 查询参数里的凭据：access_token= / key= / token= / sign= / secret= / password=
    (
        re.compile(
            r"(?i)([?&](?:access_token|token|key|sign|secret|password|apikey|api_key|signature)=)[^&\s'\")]+"
        ),
        r"\1" + MASK,
    ),
    # Authorization 头
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?(?:bearer|basic)\s+)[^\s'\",}]+"), r"\1" + MASK),
    # 各家 webhook 路径本身就是凭据
    (re.compile(r"(hooks\.slack\.com/services/)[^\s'\")]+"), r"\1" + MASK),
    (re.compile(r"(open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/)[^\s'\")]+"), r"\1" + MASK),
    (re.compile(r"(qyapi\.weixin\.qq\.com/cgi-bin/webhook/send)\?[^\s'\")]+"), r"\1?" + MASK),
    (re.compile(r"(oapi\.dingtalk\.com/robot/send)\?[^\s'\")]+"), r"\1?" + MASK),
    (re.compile(r"(api\.day\.app/)[^\s'\")]+"), r"\1" + MASK),
    # URL 里的 user:password@
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1" + MASK + "@"),
]

# 太短的值替换会误伤普通文本（例如 "1"、"true"）
_MIN_SECRET_LEN = 6


def _is_sensitive_key(key: str) -> bool:
    upper = str(key).upper()
    if upper in _NON_SECRET_KEYS:
        return False
    return any(part in upper for part in _SENSITIVE_KEY_PARTS)


def _split_values(value: Any) -> Iterable[str]:
    """多账号配置用 ; 分隔，逐个拆出来"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for v in value:
            out.extend(_split_values(v))
        return out
    text = str(value).strip()
    if not text:
        return []
    parts = [text]
    if ";" in text:
        parts.extend(p.strip() for p in text.split(";") if p.strip())
    return parts


def collect_secrets(*sources: Optional[Mapping[str, Any]], include_env: bool = True) -> list:
    """从配置字典（以及环境变量）里收集所有可能是凭据的值。

    返回按长度倒序排列的列表，保证长串先被替换。
    """
    secrets = set()

    def _walk(mapping: Mapping[str, Any]):
        for k, v in mapping.items():
            if isinstance(v, Mapping):
                _walk(v)
                continue
            if _is_sensitive_key(k):
                for s in _split_values(v):
                    if len(s) >= _MIN_SECRET_LEN and s not in _PUBLIC_VALUES:
                        secrets.add(s)

    for src in sources:
        if src:
            _walk(src)
    if include_env:
        _walk(dict(os.environ))
    return sorted(secrets, key=len, reverse=True)


def redact(text: Any, secrets: Optional[Iterable[str]] = None) -> str:
    """把文本里的已知密钥和常见凭据格式替换为 ***。"""
    if text is None:
        return ""
    out = str(text)
    if not out:
        return out
    for s in secrets or ():
        if s and len(s) >= _MIN_SECRET_LEN:
            out = out.replace(s, MASK)
    for pattern, repl in _FALLBACK_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_obj(obj: Any, secrets: Optional[Iterable[str]] = None) -> Any:
    """递归脱敏 dict / list 中的所有字符串。"""
    secrets = list(secrets or ())
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, Mapping):
        return {k: redact_obj(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, secrets) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v, secrets) for v in obj)
    return obj


def safe_error_message(exc: BaseException, secrets: Optional[Iterable[str]] = None) -> str:
    """异常 → 可以安全返回给客户端的简短描述（类型名 + 脱敏后的信息）。"""
    msg = redact(str(exc), secrets)
    name = type(exc).__name__
    if len(msg) > 300:
        msg = msg[:300] + "…"
    return f"{name}: {msg}" if msg else name


def safe_error(exc: BaseException, *sources: Optional[Mapping[str, Any]]) -> str:
    """``safe_error_message`` + 已知密钥（配置字典 / 环境变量）替换，一步到位。"""
    return redact(safe_error_message(exc), collect_secrets(*sources))
