# coding=utf-8
"""
消息发送器模块

将报告数据发送到各种通知渠道：
- 飞书 (Feishu/Lark)
- 钉钉 (DingTalk)
- 企业微信 (WeCom/WeWork)
- Telegram
- 邮件 (Email)
- ntfy
- Bark
- Slack
- 通用 Webhook

每个发送函数都支持分批发送，并通过参数化配置实现与 CONFIG 的解耦。

安全约定：
- 日志中不出现 token / webhook key / topic / device_key 等凭据（统一经 escaping.redact 脱敏）
- 发送失败用 warning / error 级别记录
- 分批内容不会被截断：超限内容按行、再按字符边界拆成更多批次
"""

import html
import json
import re
import smtplib
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import requests

from .batch import add_batch_headers, get_max_batch_header_size, split_text_to_limit, text_size
from .escaping import describe_error, mask_url, short_response
from .formatters import convert_markdown_to_mrkdwn, strip_markdown

from trendradar.core.logger import get_logger

log = get_logger(__name__)

# 发送结果
_OK = "ok"          # 本批成功
_FAIL = "fail"      # 本批失败（内容类错误），继续发送后续批次
_FATAL = "fatal"    # 致命错误（网络/鉴权/地址错误），后续批次大概率同样失败，立即停止

# 各渠道单条消息硬上限（与 batch.CHANNEL_HARD_LIMITS 一致，超出时自动收紧 batch_size）
TELEGRAM_MAX_CHARS = 4096
NTFY_MAX_BYTES = 4096
BARK_MAX_PAYLOAD_BYTES = 4096
BARK_MAX_BATCH_BYTES = 3600       # 正文上限：APNs 4KB 包含 title / JSON 外壳
WEWORK_TEXT_MAX_BYTES = 2048
WEWORK_MARKDOWN_MAX_BYTES = 4096


def _url_secrets(url: str) -> List[str]:
    """从 webhook URL 中提取需要在日志里隐藏的部分（完整 URL、路径、查询串）"""
    if not url:
        return []
    secrets = [url]
    try:
        parts = urlsplit(url)
        if parts.path and parts.path not in ("/",):
            secrets.append(parts.path)
        if parts.query:
            secrets.append(parts.query)
    except ValueError:
        pass
    return secrets


def _proxies(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def _clamp_batch_size(batch_size: int, hard_limit: int, log_prefix: str, unit: str = "字节") -> int:
    if batch_size > hard_limit:
        log.warning(f"{log_prefix} batch_size={batch_size} 超过渠道上限 {hard_limit}{unit}，已自动收紧")
        return hard_limit
    return batch_size


def _run_batches(
    batches: List[str],
    send_one: Callable[[int, str], str],
    log_prefix: str,
    report_type: str,
    batch_interval: float,
    reverse: bool = False,
    unit_format: Optional[str] = None,
) -> bool:
    """逐批发送，返回是否全部成功。

    - send_one(batch_num, content) 返回 _OK / _FAIL / _FATAL
    - reverse=True 时从最后一批开始推送（ntfy/Bark 客户端新消息在上，倒序推送后阅读顺序正确）
    - 遇到 _FATAL 立即停止；_FAIL 记录后继续发送后续批次
    """
    total = len(batches)
    if total == 0:
        return False
    log.info(f"{log_prefix}消息分为 {total} 批次发送 [{report_type}]")
    order = list(range(total, 0, -1)) if reverse else list(range(1, total + 1))
    if reverse and total > 1:
        log.info(f"{log_prefix}将按反向顺序推送（最后批次先推送），确保客户端显示顺序正确")

    success = 0
    for idx, batch_num in enumerate(order, 1):
        content = batches[batch_num - 1]
        size = text_size(content, unit_format)
        unit = "字符" if unit_format == "telegram" else "字节"
        log.info(f"发送{log_prefix}第 {batch_num}/{total} 批次，大小：{size} {unit} [{report_type}]")
        result = send_one(batch_num, content)
        if result == _OK:
            success += 1
            log.info(f"{log_prefix}第 {batch_num}/{total} 批次发送成功 [{report_type}]")
            if idx < total and batch_interval > 0:
                time.sleep(batch_interval)
        elif result == _FATAL:
            log.error(f"{log_prefix}第 {batch_num}/{total} 批次发送失败，停止发送剩余批次 [{report_type}]")
            break

    if success == total:
        log.info(f"{log_prefix}所有 {total} 批次发送完成 [{report_type}]")
        return True
    if success > 0:
        log.warning(f"{log_prefix}部分发送成功：{success}/{total} 批次 [{report_type}]")
    else:
        log.error(f"{log_prefix}发送完全失败 [{report_type}]")
    return False


def _status_result(status_code: int) -> str:
    """HTTP 状态码 → 发送结果：鉴权 / 地址类错误视为致命"""
    if status_code in (401, 403, 404, 410):
        return _FATAL
    return _FAIL


def _extract_ai_stats(ai_analysis) -> Optional[Dict]:
    """从 AI 分析结果中提取统计数据"""
    if not ai_analysis or not getattr(ai_analysis, "success", False):
        return None
    return {
        "total_news": getattr(ai_analysis, "total_news", 0),
        "analyzed_news": getattr(ai_analysis, "analyzed_news", 0),
        "max_news_limit": getattr(ai_analysis, "max_news_limit", 0),
        "hotlist_count": getattr(ai_analysis, "hotlist_count", 0),
        "rss_count": getattr(ai_analysis, "rss_count", 0),
        "hotlist_analyzed": getattr(ai_analysis, "hotlist_analyzed", 0),
        "rss_analyzed": getattr(ai_analysis, "rss_analyzed", 0),
        "standalone_analyzed": getattr(ai_analysis, "standalone_analyzed", 0),
        "ai_mode": getattr(ai_analysis, "ai_mode", ""),
        "include_rss": getattr(ai_analysis, "include_rss", True),
        "include_standalone": getattr(ai_analysis, "include_standalone", False),
    }


def _render_ai_analysis(ai_analysis: Any, channel: str) -> str:
    """渲染 AI 分析内容为指定渠道格式"""
    if not ai_analysis:
        return ""

    try:
        from trendradar.ai.formatter import get_ai_analysis_renderer
        renderer = get_ai_analysis_renderer(channel)
        return renderer(ai_analysis)
    except ImportError:
        return ""


# === SMTP 邮件配置 ===
# encryption: SSL = 隐式 TLS（端口 465，smtplib.SMTP_SSL）；TLS = STARTTLS（端口 587，smtplib.SMTP + starttls）
SMTP_CONFIGS = {
    # Gmail（使用 STARTTLS）
    "gmail.com": {"server": "smtp.gmail.com", "port": 587, "encryption": "TLS"},
    # QQ邮箱（使用 SSL，更稳定）
    "qq.com": {"server": "smtp.qq.com", "port": 465, "encryption": "SSL"},
    # Outlook（使用 STARTTLS）
    "outlook.com": {"server": "smtp-mail.outlook.com", "port": 587, "encryption": "TLS"},
    "hotmail.com": {"server": "smtp-mail.outlook.com", "port": 587, "encryption": "TLS"},
    "live.com": {"server": "smtp-mail.outlook.com", "port": 587, "encryption": "TLS"},
    # 网易邮箱（使用 SSL，更稳定）
    "163.com": {"server": "smtp.163.com", "port": 465, "encryption": "SSL"},
    "126.com": {"server": "smtp.126.com", "port": 465, "encryption": "SSL"},
    # 新浪邮箱（使用 SSL）
    "sina.com": {"server": "smtp.sina.com", "port": 465, "encryption": "SSL"},
    # 搜狐邮箱（使用 SSL）
    "sohu.com": {"server": "smtp.sohu.com", "port": 465, "encryption": "SSL"},
    # 天翼邮箱（使用 SSL）
    "189.cn": {"server": "smtp.189.cn", "port": 465, "encryption": "SSL"},
    # 阿里云邮箱（465 为隐式 SSL）
    "aliyun.com": {"server": "smtp.aliyun.com", "port": 465, "encryption": "SSL"},
    # Yandex邮箱（465 为隐式 SSL）
    "yandex.com": {"server": "smtp.yandex.com", "port": 465, "encryption": "SSL"},
    "yandex.ru": {"server": "smtp.yandex.ru", "port": 465, "encryption": "SSL"},
    # iCloud邮箱（587 为 STARTTLS）
    "icloud.com": {"server": "smtp.mail.me.com", "port": 587, "encryption": "TLS"},
    "me.com": {"server": "smtp.mail.me.com", "port": 587, "encryption": "TLS"},
}


def _parse_recipients(to_email: str) -> List[str]:
    """收件人同时兼容 , 与 ; 分隔"""
    return [addr.strip() for addr in re.split(r"[,;]", to_email or "") if addr.strip()]


def _resolve_smtp(
    from_email: str, custom_smtp_server: Optional[str], custom_smtp_port: Optional[Any]
) -> Tuple[str, int, bool]:
    """返回 (server, port, use_starttls)"""
    domain = from_email.split("@")[-1].lower()
    preset = SMTP_CONFIGS.get(domain)

    if custom_smtp_server or custom_smtp_port:
        server = custom_smtp_server or (preset["server"] if preset else f"smtp.{domain}")
        if custom_smtp_port:
            port = int(custom_smtp_port)
        else:
            port = preset["port"] if preset else 587
        # 465 = 隐式 SSL，其余端口（587/25/2525…）用 STARTTLS
        return server, port, port != 465

    if preset:
        return preset["server"], preset["port"], preset["encryption"] == "TLS"

    log.info(f"未识别的邮箱服务商: {domain}，使用通用 SMTP 配置")
    return f"smtp.{domain}", 587, True


def _prepare_batches(
    split_content_func: Callable,
    report_data: Dict,
    split_format: str,
    header_format: str,
    update_info: Optional[Dict],
    batch_size: int,
    mode: str,
    report_type: str,
    rss_items: Optional[list],
    rss_new_items: Optional[list],
    ai_analysis: Any,
    ai_channel: str,
    standalone_data: Optional[Dict],
    extra_reserve: int = 0,
) -> List[str]:
    """渲染 AI 内容 → 分批 → 添加批次头部（预留头部空间，超限自动再拆，不截断）"""
    if split_content_func is None:
        raise ValueError("split_content_func is required")
    ai_content = _render_ai_analysis(ai_analysis, ai_channel) if ai_analysis else None
    ai_stats = _extract_ai_stats(ai_analysis)
    limit = batch_size - extra_reserve
    header_reserve = get_max_batch_header_size(header_format)
    batches = split_content_func(
        report_data,
        split_format,
        update_info,
        max_bytes=limit - header_reserve,
        mode=mode,
        rss_items=rss_items,
        rss_new_items=rss_new_items,
        ai_content=ai_content,
        standalone_data=standalone_data,
        ai_stats=ai_stats,
        report_type=report_type,
    )
    return add_batch_headers(batches, header_format, limit)


def send_to_feishu(
    webhook_url: str,
    report_data: Dict,
    report_type: str,
    update_info: Optional[Dict] = None,
    proxy_url: Optional[str] = None,
    mode: str = "daily",
    account_label: str = "",
    *,
    batch_size: int = 29000,
    batch_interval: float = 1.0,
    split_content_func: Callable = None,
    get_time_func: Callable = None,
    rss_items: Optional[list] = None,
    rss_new_items: Optional[list] = None,
    ai_analysis: Any = None,
    display_regions: Optional[Dict] = None,
    standalone_data: Optional[Dict] = None,
) -> bool:
    """
    发送到飞书（支持分批发送，支持热榜+RSS合并+独立展示区）

    Args:
        webhook_url: 飞书 Webhook URL
        report_data: 报告数据
        report_type: 报告类型
        update_info: 更新信息（可选）
        proxy_url: 代理 URL（可选）
        mode: 报告模式 (daily/current)
        account_label: 账号标签（多账号时显示）
        batch_size: 批次大小（字节）
        batch_interval: 批次发送间隔（秒）
        split_content_func: 内容分批函数
        get_time_func: 获取当前时间的函数
        rss_items: RSS 统计条目列表（可选，用于合并推送）
        rss_new_items: RSS 新增条目列表（可选，用于新增区块）
        display_regions: 保留参数（区域过滤已在调度器完成）

    Returns:
        bool: 所有批次是否发送成功
    """
    headers = {"Content-Type": "application/json"}
    proxies = _proxies(proxy_url)
    log_prefix = f"飞书{account_label}" if account_label else "飞书"
    secrets = _url_secrets(webhook_url)

    batches = _prepare_batches(
        split_content_func, report_data, "feishu", "feishu", update_info, batch_size, mode,
        report_type, rss_items, rss_new_items, ai_analysis, "feishu", standalone_data,
    )
    # www.feishu.cn 使用纯文本格式，其他域名（open.feishu.cn/open.larksuite.com）使用卡片 2.0
    text_mode = (urlsplit(webhook_url).hostname or "") == "www.feishu.cn"

    def send_one(batch_num: int, content: str) -> str:
        if text_mode:
            payload = {"msg_type": "text", "content": {"text": content}}
        else:
            payload = {
                "msg_type": "interactive",
                "card": {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": content}]}},
            }
        try:
            response = requests.post(webhook_url, headers=headers, json=payload, proxies=proxies, timeout=30)
            if response.status_code != 200:
                log.warning(f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，状态码：{response.status_code}")
                return _status_result(response.status_code)
            result = response.json()
            if result.get("StatusCode") == 0 or result.get("code") == 0:
                return _OK
            error_msg = result.get("msg") or result.get("StatusMessage", "未知错误")
            log.warning(
                f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，错误：{short_response(error_msg, *secrets)}"
            )
            # 19001 等参数/签名错误后续批次同样会失败
            return _FATAL if result.get("code") in (19001, 19002, 19021, 19022, 19024) else _FAIL
        except (requests.RequestException, ValueError) as e:
            log.error(f"{log_prefix}第 {batch_num} 批次发送出错 [{report_type}]：{describe_error(e, *secrets)}")
            return _FATAL

    return _run_batches(batches, send_one, log_prefix, report_type, batch_interval)


def send_to_dingtalk(
    webhook_url: str,
    report_data: Dict,
    report_type: str,
    update_info: Optional[Dict] = None,
    proxy_url: Optional[str] = None,
    mode: str = "daily",
    account_label: str = "",
    *,
    batch_size: int = 20000,
    batch_interval: float = 1.0,
    split_content_func: Callable = None,
    rss_items: Optional[list] = None,
    rss_new_items: Optional[list] = None,
    ai_analysis: Any = None,
    display_regions: Optional[Dict] = None,
    standalone_data: Optional[Dict] = None,
) -> bool:
    """
    发送到钉钉（支持分批发送，支持热榜+RSS合并+独立展示区）

    Returns:
        bool: 所有批次是否发送成功
    """
    headers = {"Content-Type": "application/json"}
    proxies = _proxies(proxy_url)
    log_prefix = f"钉钉{account_label}" if account_label else "钉钉"
    secrets = _url_secrets(webhook_url)

    batches = _prepare_batches(
        split_content_func, report_data, "dingtalk", "dingtalk", update_info, batch_size, mode,
        report_type, rss_items, rss_new_items, ai_analysis, "dingtalk", standalone_data,
    )

    def send_one(batch_num: int, content: str) -> str:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": f"TrendRadar 热点分析报告 - {report_type}", "text": content},
        }
        try:
            response = requests.post(webhook_url, headers=headers, json=payload, proxies=proxies, timeout=30)
            if response.status_code != 200:
                log.warning(f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，状态码：{response.status_code}")
                return _status_result(response.status_code)
            result = response.json()
            if result.get("errcode") == 0:
                return _OK
            log.warning(
                f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，"
                f"错误：{short_response(result.get('errmsg'), *secrets)}"
            )
            # 300001 token 无效 / 310000 签名或关键词校验失败
            return _FATAL if result.get("errcode") in (300001, 310000, 400013, 400101) else _FAIL
        except (requests.RequestException, ValueError) as e:
            log.error(f"{log_prefix}第 {batch_num} 批次发送出错 [{report_type}]：{describe_error(e, *secrets)}")
            return _FATAL

    return _run_batches(batches, send_one, log_prefix, report_type, batch_interval)


def send_to_wework(
    webhook_url: str,
    report_data: Dict,
    report_type: str,
    update_info: Optional[Dict] = None,
    proxy_url: Optional[str] = None,
    mode: str = "daily",
    account_label: str = "",
    *,
    batch_size: int = 4000,
    batch_interval: float = 1.0,
    msg_type: str = "markdown",
    split_content_func: Callable = None,
    rss_items: Optional[list] = None,
    rss_new_items: Optional[list] = None,
    ai_analysis: Any = None,
    display_regions: Optional[Dict] = None,
    standalone_data: Optional[Dict] = None,
) -> bool:
    """
    发送到企业微信（支持分批发送，支持 markdown 和 text 两种格式，支持热榜+RSS合并+独立展示区）

    Returns:
        bool: 所有批次是否发送成功
    """
    headers = {"Content-Type": "application/json"}
    proxies = _proxies(proxy_url)
    log_prefix = f"企业微信{account_label}" if account_label else "企业微信"
    secrets = _url_secrets(webhook_url)

    is_text_mode = (msg_type or "markdown").lower() == "text"
    if is_text_mode:
        log.info(f"{log_prefix}使用 text 格式（个人微信模式）[{report_type}]")
        batch_size = _clamp_batch_size(batch_size, WEWORK_TEXT_MAX_BYTES, log_prefix)
    else:
        log.info(f"{log_prefix}使用 markdown 格式（群机器人模式）[{report_type}]")
        batch_size = _clamp_batch_size(batch_size, WEWORK_MARKDOWN_MAX_BYTES, log_prefix)
    header_format_type = "wework_text" if is_text_mode else "wework"

    batches = _prepare_batches(
        split_content_func, report_data, "wework", header_format_type, update_info, batch_size, mode,
        report_type, rss_items, rss_new_items, ai_analysis, "wework", standalone_data,
    )

    def send_one(batch_num: int, content: str) -> str:
        if is_text_mode:
            payload = {"msgtype": "text", "text": {"content": strip_markdown(content)}}
        else:
            payload = {"msgtype": "markdown", "markdown": {"content": content}}
        try:
            response = requests.post(webhook_url, headers=headers, json=payload, proxies=proxies, timeout=30)
            if response.status_code != 200:
                log.warning(f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，状态码：{response.status_code}")
                return _status_result(response.status_code)
            result = response.json()
            if result.get("errcode") == 0:
                return _OK
            log.warning(
                f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，"
                f"错误：{short_response(result.get('errmsg'), *secrets)}"
            )
            # 93000 webhook key 无效
            return _FATAL if result.get("errcode") in (93000, 40008) else _FAIL
        except (requests.RequestException, ValueError) as e:
            log.error(f"{log_prefix}第 {batch_num} 批次发送出错 [{report_type}]：{describe_error(e, *secrets)}")
            return _FATAL

    return _run_batches(batches, send_one, log_prefix, report_type, batch_interval)


_TG_ANCHOR_RE = re.compile(r'<a\s+href="([^"]*)"\s*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_TG_TAG_RE = re.compile(r"<[^<>]+>")


def telegram_html_to_plain(text: str) -> str:
    """把 Telegram HTML 降级为纯文本：链接变成「标题 (URL)」，其余标签去掉，实体还原"""
    text = _TG_ANCHOR_RE.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = _TG_TAG_RE.sub("", text)
    return html.unescape(text)


def send_to_telegram(
    bot_token: str,
    chat_id: str,
    report_data: Dict,
    report_type: str,
    update_info: Optional[Dict] = None,
    proxy_url: Optional[str] = None,
    mode: str = "daily",
    account_label: str = "",
    *,
    batch_size: int = 4000,
    batch_interval: float = 1.0,
    split_content_func: Callable = None,
    rss_items: Optional[list] = None,
    rss_new_items: Optional[list] = None,
    ai_analysis: Any = None,
    display_regions: Optional[Dict] = None,
    standalone_data: Optional[Dict] = None,
) -> bool:
    """
    发送到 Telegram（支持分批发送，支持热榜+RSS合并+独立展示区）

    - batch_size 按字符（UTF-16 码元）计，上限 4096
    - 某批 HTML 解析失败（can't parse entities）时，自动降级为纯文本重发该批
    - 429 按 retry_after 等待后重试一次

    Returns:
        bool: 所有批次是否发送成功
    """
    headers = {"Content-Type": "application/json"}
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    proxies = _proxies(proxy_url)
    log_prefix = f"Telegram{account_label}" if account_label else "Telegram"
    secrets = [bot_token, str(chat_id)]

    batch_size = _clamp_batch_size(batch_size, TELEGRAM_MAX_CHARS, log_prefix, unit="字符")
    batches = _prepare_batches(
        split_content_func, report_data, "telegram", "telegram", update_info, batch_size, mode,
        report_type, rss_items, rss_new_items, ai_analysis, "telegram", standalone_data,
    )

    def post(text: str, parse_mode: Optional[str]) -> Tuple[Optional[requests.Response], Optional[dict]]:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        for attempt in range(2):
            response = requests.post(url, headers=headers, json=payload, proxies=proxies, timeout=30)
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code == 429 and attempt == 0:
                retry_after = (body.get("parameters") or {}).get("retry_after", 5)
                wait = min(max(int(retry_after), 1), 60)
                log.warning(f"{log_prefix}触发限流，{wait}s 后重试")
                time.sleep(wait)
                continue
            return response, body
        return response, body

    def send_one(batch_num: int, content: str) -> str:
        try:
            response, body = post(content, "HTML")
            if response.status_code == 200 and body.get("ok"):
                return _OK
            description = str(body.get("description", ""))
            if response.status_code == 400 and "parse entities" in description.lower():
                log.warning(
                    f"{log_prefix}第 {batch_num} 批次 HTML 解析失败（{short_response(description, *secrets)}），"
                    f"降级为纯文本重发"
                )
                plain = telegram_html_to_plain(content)
                parts = split_text_to_limit(plain, TELEGRAM_MAX_CHARS, "telegram")
                for part in parts:
                    r2, b2 = post(part, None)
                    if not (r2.status_code == 200 and b2.get("ok")):
                        log.warning(
                            f"{log_prefix}第 {batch_num} 批次纯文本重发失败，"
                            f"错误：{short_response(b2.get('description') or r2.status_code, *secrets)}"
                        )
                        return _status_result(r2.status_code)
                return _OK
            log.warning(
                f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，状态码：{response.status_code}，"
                f"错误：{short_response(description, *secrets)}"
            )
            lowered = description.lower()
            if "chat not found" in lowered or "bot was blocked" in lowered or "not enough rights" in lowered:
                return _FATAL
            return _status_result(response.status_code)
        except requests.RequestException as e:
            log.error(f"{log_prefix}第 {batch_num} 批次发送出错 [{report_type}]：{describe_error(e, *secrets)}")
            return _FATAL

    return _run_batches(batches, send_one, log_prefix, report_type, batch_interval, unit_format="telegram")


def send_to_email(
    from_email: str,
    password: str,
    to_email: str,
    report_type: str,
    html_file_path: str,
    custom_smtp_server: Optional[str] = None,
    custom_smtp_port: Optional[int] = None,
    *,
    get_time_func: Callable = None,
) -> bool:
    """
    发送邮件通知

    Args:
        from_email: 发件人邮箱
        password: 邮箱密码/授权码
        to_email: 收件人邮箱（多个用 , 或 ; 分隔）
        report_type: 报告类型
        html_file_path: HTML 报告文件路径
        custom_smtp_server: 自定义 SMTP 服务器（可选）
        custom_smtp_port: 自定义 SMTP 端口（可选；465=隐式 SSL，其余=STARTTLS）
        get_time_func: 获取当前时间的函数

    Returns:
        bool: 发送是否成功

    Note:
        AI 分析内容已在 HTML 生成时嵌入，无需再追加
    """
    if not html_file_path or not Path(html_file_path).exists():
        log.error(f"邮件发送失败：HTML文件不存在或未提供: {html_file_path}")
        return False

    recipients = _parse_recipients(to_email)
    if not recipients:
        log.error("邮件发送失败：未配置有效的收件人")
        return False

    try:
        smtp_server, smtp_port, use_starttls = _resolve_smtp(from_email, custom_smtp_server, custom_smtp_port)
    except (TypeError, ValueError):
        log.error(f"邮件发送失败：SMTP 端口配置无效：{custom_smtp_port!r}")
        return False

    try:
        log.info(f"使用HTML文件: {html_file_path}")
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        msg = MIMEMultipart("alternative")
        msg["From"] = formataddr(("TrendRadar", from_email))
        msg["To"] = ", ".join(recipients)

        now = get_time_func() if get_time_func else datetime.now()
        subject = f"TrendRadar 热点分析报告 - {report_type} - {now.strftime('%m月%d日 %H:%M')}"
        msg["Subject"] = Header(subject, "utf-8")
        msg["MIME-Version"] = "1.0"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        text_content = f"""
TrendRadar 热点分析报告
========================
报告类型：{report_type}
生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}

请使用支持HTML的邮件客户端查看完整报告内容。
        """
        msg.attach(MIMEText(text_content, "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        log.info(f"正在发送邮件到 {len(recipients)} 个收件人，SMTP 服务器: {smtp_server}:{smtp_port}"
                 f"（{'STARTTLS' if use_starttls else 'SSL'}）")

        # with 语句保证异常时也会关闭连接
        if use_starttls:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(from_email, password)
                server.send_message(msg, from_addr=from_email, to_addrs=recipients)
        else:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as server:
                server.ehlo()
                server.login(from_email, password)
                server.send_message(msg, from_addr=from_email, to_addrs=recipients)

        log.info(f"邮件发送成功 [{report_type}] -> {len(recipients)} 个收件人")
        return True

    except smtplib.SMTPAuthenticationError as e:
        log.error(f"邮件发送失败：认证错误，请检查邮箱和密码/授权码（{describe_error(e, password)}）")
    except smtplib.SMTPRecipientsRefused as e:
        log.error(f"邮件发送失败：收件人地址被拒绝 {describe_error(e, password)}")
    except smtplib.SMTPSenderRefused as e:
        log.error(f"邮件发送失败：发件人地址被拒绝 {describe_error(e, password)}")
    except smtplib.SMTPDataError as e:
        log.error(f"邮件发送失败：邮件数据错误 {describe_error(e, password)}")
    except smtplib.SMTPServerDisconnected:
        log.error("邮件发送失败：服务器意外断开连接（端口/加密方式不匹配或网络问题）")
    except smtplib.SMTPConnectError as e:
        log.error(f"邮件发送失败：无法连接到 SMTP 服务器 {smtp_server}:{smtp_port}（{describe_error(e, password)}）")
    except Exception as e:
        log.error(f"邮件发送失败 [{report_type}]：{describe_error(e, password)}")
    return False


def send_to_ntfy(
    server_url: str,
    topic: str,
    token: Optional[str],
    report_data: Dict,
    report_type: str,
    update_info: Optional[Dict] = None,
    proxy_url: Optional[str] = None,
    mode: str = "daily",
    account_label: str = "",
    *,
    batch_size: int = 3800,
    split_content_func: Callable = None,
    rss_items: Optional[list] = None,
    rss_new_items: Optional[list] = None,
    ai_analysis: Any = None,
    display_regions: Optional[Dict] = None,
    standalone_data: Optional[Dict] = None,
) -> bool:
    """
    发送到 ntfy（支持分批发送，严格遵守 4KB 限制，支持热榜+RSS合并+独立展示区）

    Returns:
        bool: 所有批次是否发送成功（部分成功返回 False）
    """
    log_prefix = f"ntfy{account_label}" if account_label else "ntfy"
    secrets = [topic, token] if token else [topic]

    # 避免 HTTP header 编码问题
    report_type_en_map = {
        "全天汇总": "Daily Summary",
        "当前榜单": "Current Ranking",
        "增量分析": "Incremental Update",
        "通知连通性测试": "Notification Test",
    }
    report_type_en = report_type_en_map.get(report_type, "News Report")

    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Markdown": "yes",
        "Title": report_type_en,
        "Priority": "default",
        "Tags": "news",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    base_url = (server_url or "").strip().rstrip("/")
    if not base_url:
        log.error(f"{log_prefix}未配置服务器地址，跳过")
        return False
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    # topic 做 URL 编码，避免 / ? # 等字符改变请求路径
    url = f"{base_url}/{quote(topic.strip(), safe='')}"
    proxies = _proxies(proxy_url)
    is_public_server = (urlsplit(base_url).hostname or "") == "ntfy.sh"

    batch_size = _clamp_batch_size(batch_size, NTFY_MAX_BYTES, log_prefix)
    batches = _prepare_batches(
        split_content_func, report_data, "ntfy", "ntfy", update_info, batch_size, mode,
        report_type, rss_items, rss_new_items, ai_analysis, "ntfy", standalone_data,
    )
    total = len(batches)

    def post(content: str, current_headers: Dict[str, str]) -> requests.Response:
        return requests.post(
            url, headers=current_headers, data=content.encode("utf-8"), proxies=proxies, timeout=30
        )

    def send_one(batch_num: int, content: str) -> str:
        current_headers = headers.copy()
        if total > 1:
            current_headers["Title"] = f"{report_type_en} ({batch_num}/{total})"
        try:
            response = post(content, current_headers)
            if response.status_code == 429:
                log.warning(f"{log_prefix}第 {batch_num}/{total} 批次速率限制，10s 后重试")
                time.sleep(10)
                response = post(content, current_headers)
            if response.status_code == 200:
                return _OK
            if response.status_code == 413:
                log.warning(f"{log_prefix}第 {batch_num}/{total} 批次消息过大被拒绝 [{report_type}]")
                return _FAIL
            log.warning(
                f"{log_prefix}第 {batch_num}/{total} 批次发送失败 [{report_type}]，状态码：{response.status_code}，"
                f"详情：{short_response(response.text, *secrets)}"
            )
            return _status_result(response.status_code)
        except requests.RequestException as e:
            log.error(f"{log_prefix}第 {batch_num}/{total} 批次发送出错 [{report_type}]：{describe_error(e, *secrets)}")
            return _FATAL

    # 公共服务器建议 2-3 秒间隔，自托管可以更短
    return _run_batches(
        batches, send_one, log_prefix, report_type, 2 if is_public_server else 1, reverse=True
    )


def parse_bark_url(bark_url: str) -> Tuple[Optional[str], Optional[str]]:
    """解析 Bark URL → (api_endpoint, device_key)

    支持：
    - https://api.day.app/<device_key>
    - https://host/<prefix>/<device_key>（自建服务带路径前缀）
    - 末尾多余的 / 或旧格式 https://host/<device_key>/<title>/<body> 以外的写法按最后一段取 key
    """
    try:
        parts = urlsplit((bark_url or "").strip())
    except ValueError:
        return None, None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None, None
    segments = [seg for seg in parts.path.split("/") if seg]
    if not segments:
        return None, None
    device_key = segments[-1]
    prefix = "/".join(segments[:-1])
    endpoint = f"{parts.scheme}://{parts.netloc}/{prefix + '/' if prefix else ''}push"
    return endpoint, device_key


def send_to_bark(
    bark_url: str,
    report_data: Dict,
    report_type: str,
    update_info: Optional[Dict] = None,
    proxy_url: Optional[str] = None,
    mode: str = "daily",
    account_label: str = "",
    *,
    batch_size: int = 3600,
    batch_interval: float = 1.0,
    split_content_func: Callable = None,
    rss_items: Optional[list] = None,
    rss_new_items: Optional[list] = None,
    ai_analysis: Any = None,
    display_regions: Optional[Dict] = None,
    standalone_data: Optional[Dict] = None,
) -> bool:
    """
    发送到 Bark（支持分批发送，使用 markdown 格式，支持热榜+RSS合并+独立展示区）

    Returns:
        bool: 所有批次是否发送成功（部分成功返回 False）
    """
    log_prefix = f"Bark{account_label}" if account_label else "Bark"
    proxies = _proxies(proxy_url)

    api_endpoint, device_key = parse_bark_url(bark_url)
    if not device_key:
        log.error(f"{log_prefix} URL 格式错误，无法提取 device_key（{mask_url(bark_url)}）")
        return False
    secrets = [device_key] + _url_secrets(bark_url)

    # APNs 整个 payload 上限 4KB：正文 + title + JSON 外壳，正文最多 3600 字节
    batch_size = _clamp_batch_size(batch_size, BARK_MAX_BATCH_BYTES, log_prefix)
    batches = _prepare_batches(
        split_content_func, report_data, "bark", "bark", update_info, batch_size, mode,
        report_type, rss_items, rss_new_items, ai_analysis, "bark", standalone_data,
    )
    total = len(batches)

    def send_one(batch_num: int, content: str) -> str:
        payload = {
            "title": report_type,
            "markdown": content,
            "device_key": device_key,
            "sound": "default",
            "group": "TrendRadar",
            "action": "none",  # 点击推送跳到 APP 不弹出弹框,方便阅读
        }
        payload_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if payload_size > BARK_MAX_PAYLOAD_BYTES:
            log.warning(f"{log_prefix}第 {batch_num}/{total} 批次 payload {payload_size} 字节，超过 APNs 4KB，可能被拒绝")
        try:
            response = requests.post(api_endpoint, json=payload, proxies=proxies, timeout=30)
            if response.status_code != 200:
                log.warning(
                    f"{log_prefix}第 {batch_num}/{total} 批次发送失败 [{report_type}]，状态码：{response.status_code}，"
                    f"详情：{short_response(response.text, *secrets)}"
                )
                return _status_result(response.status_code)
            result = response.json()
            if result.get("code") == 200:
                return _OK
            log.warning(
                f"{log_prefix}第 {batch_num}/{total} 批次发送失败 [{report_type}]，"
                f"错误：{short_response(result.get('message', '未知错误'), *secrets)}"
            )
            return _FAIL
        except (requests.RequestException, ValueError) as e:
            log.error(f"{log_prefix}第 {batch_num}/{total} 批次发送出错 [{report_type}]：{describe_error(e, *secrets)}")
            return _FATAL

    return _run_batches(batches, send_one, log_prefix, report_type, batch_interval, reverse=True)


def send_to_slack(
    webhook_url: str,
    report_data: Dict,
    report_type: str,
    update_info: Optional[Dict] = None,
    proxy_url: Optional[str] = None,
    mode: str = "daily",
    account_label: str = "",
    *,
    batch_size: int = 4000,
    batch_interval: float = 1.0,
    split_content_func: Callable = None,
    rss_items: Optional[list] = None,
    rss_new_items: Optional[list] = None,
    ai_analysis: Any = None,
    display_regions: Optional[Dict] = None,
    standalone_data: Optional[Dict] = None,
) -> bool:
    """
    发送到 Slack（支持分批发送，使用 mrkdwn 格式，支持热榜+RSS合并+独立展示区）

    Returns:
        bool: 所有批次是否发送成功
    """
    headers = {"Content-Type": "application/json"}
    proxies = _proxies(proxy_url)
    log_prefix = f"Slack{account_label}" if account_label else "Slack"
    secrets = _url_secrets(webhook_url)

    batches = _prepare_batches(
        split_content_func, report_data, "slack", "slack", update_info, batch_size, mode,
        report_type, rss_items, rss_new_items, ai_analysis, "slack", standalone_data,
    )

    def send_one(batch_num: int, content: str) -> str:
        payload = {"text": convert_markdown_to_mrkdwn(content)}
        try:
            response = requests.post(webhook_url, headers=headers, json=payload, proxies=proxies, timeout=30)
            # Slack Incoming Webhooks 成功时返回 "ok" 文本
            if response.status_code == 200 and response.text == "ok":
                return _OK
            log.warning(
                f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，状态码：{response.status_code}，"
                f"错误：{short_response(response.text, *secrets)}"
            )
            return _status_result(response.status_code)
        except requests.RequestException as e:
            log.error(f"{log_prefix}第 {batch_num} 批次发送出错 [{report_type}]：{describe_error(e, *secrets)}")
            return _FATAL

    return _run_batches(batches, send_one, log_prefix, report_type, batch_interval)


_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{(content|title)\}")
_DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")


def render_webhook_payload(payload_template: Optional[str], title: str, content: str) -> Any:
    """渲染通用 Webhook 的 JSON 模板

    - 单次扫描替换 {title} / {content}，正文里本身出现的 "{title}" 不会被二次替换
    - 占位符按 JSON 字符串转义（模板中应写成 "{content}"）
    - 模板无效时回退到默认格式 {"title": ..., "content": ...}
    """
    if not payload_template:
        return {"title": title, "content": content}
    values = {"content": json.dumps(content)[1:-1], "title": json.dumps(title)[1:-1]}
    payload_str = _TEMPLATE_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], payload_template)
    try:
        return json.loads(payload_str)
    except json.JSONDecodeError as e:
        log.warning(f"通用Webhook JSON 模板解析失败（{e.msg}），回退到默认格式")
        return {"title": title, "content": content}


def send_to_generic_webhook(
    webhook_url: str,
    payload_template: Optional[str],
    report_data: Dict,
    report_type: str,
    update_info: Optional[Dict] = None,
    proxy_url: Optional[str] = None,
    mode: str = "daily",
    account_label: str = "",
    *,
    batch_size: int = 4000,
    batch_interval: float = 1.0,
    split_content_func: Optional[Callable] = None,
    rss_items: Optional[list] = None,
    rss_new_items: Optional[list] = None,
    ai_analysis: Any = None,
    display_regions: Optional[Dict] = None,
    standalone_data: Optional[Dict] = None,
) -> bool:
    """
    发送到通用 Webhook（支持分批发送，支持自定义 JSON 模板，支持热榜+RSS合并+独立展示区）

    Args:
        payload_template: JSON 模板字符串，支持 {title} 和 {content} 占位符

    Returns:
        bool: 所有批次是否发送成功
    """
    if split_content_func is None:
        raise ValueError("split_content_func is required")

    headers = {"Content-Type": "application/json"}
    proxies = _proxies(proxy_url)
    log_prefix = f"通用Webhook{account_label}" if account_label else "通用Webhook"
    secrets = _url_secrets(webhook_url)
    is_discord = (urlsplit(webhook_url).hostname or "").lower() in _DISCORD_HOSTS

    # 模板外壳（除 {content} 外的部分 + 标题）计入每批的大小预算
    shell = render_webhook_payload(payload_template, report_type, "")
    template_overhead = len(json.dumps(shell, ensure_ascii=False).encode("utf-8")) + 32

    batches = _prepare_batches(
        split_content_func, report_data, "wework", "wework", update_info, batch_size, mode,
        report_type, rss_items, rss_new_items, ai_analysis, "wework", standalone_data,
        extra_reserve=min(template_overhead, batch_size // 2),
    )

    def send_one(batch_num: int, content: str) -> str:
        payload = render_webhook_payload(payload_template, report_type, content)
        if is_discord and isinstance(payload, dict) and "allowed_mentions" not in payload:
            # 禁止消息内容触发 @everyone / @here / 角色提醒
            payload["allowed_mentions"] = {"parse": []}
        try:
            response = requests.post(webhook_url, headers=headers, json=payload, proxies=proxies, timeout=30)
            if 200 <= response.status_code < 300:
                return _OK
            log.warning(
                f"{log_prefix}第 {batch_num} 批次发送失败 [{report_type}]，状态码：{response.status_code}，"
                f"响应：{short_response(response.text, *secrets)}"
            )
            return _status_result(response.status_code)
        except requests.RequestException as e:
            log.error(f"{log_prefix}第 {batch_num} 批次发送出错 [{report_type}]：{describe_error(e, *secrets)}")
            return _FATAL

    return _run_batches(batches, send_one, log_prefix, report_type, batch_interval)
