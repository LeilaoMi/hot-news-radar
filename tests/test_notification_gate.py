# coding=utf-8
"""通知前置判定（是否值得推送 / 是否配置了渠道）的单元测试。

这两个函数原先是 NewsAnalyzer 的方法，依赖 self 因而无法单测。抽成纯函数后，
本项目最容易「静默出问题」的两处判定终于可以被覆盖：

- 该推却没推 → 用户以为没新闻
- 渠道配了一半 → webhook 明明填了却不推送，日志里只有一句警告
"""

import pytest

from trendradar.__main__ import has_notification_configured, has_valid_content


# --------------------------------------------------------------------------
# has_valid_content
# --------------------------------------------------------------------------


def _stats(*counts):
    """按 count 序列构造 stats，count 即该分组命中的新闻条数。"""
    return [{"word": f"w{i}", "count": c, "titles": []} for i, c in enumerate(counts)]


@pytest.mark.parametrize("mode", ["incremental", "current"])
def test_matched_news_is_enough(mode):
    """这两种模式只看热榜命中，命中即推送。"""
    assert has_valid_content(mode, _stats(0, 3, 0)) is True


@pytest.mark.parametrize("mode", ["incremental", "current"])
def test_no_match_means_no_push(mode):
    assert has_valid_content(mode, _stats(0, 0)) is False


@pytest.mark.parametrize("mode", ["incremental", "current"])
def test_new_titles_alone_does_not_trigger(mode):
    """incremental / current 不看 new_titles——这是它们与 daily 的关键差别。"""
    assert has_valid_content(mode, _stats(0), {"zhihu": ["标题A"]}) is False


@pytest.mark.parametrize("mode", ["incremental", "current"])
def test_empty_stats(mode):
    assert has_valid_content(mode, []) is False


@pytest.mark.parametrize("mode", ["incremental", "current"])
def test_none_stats_does_not_raise(mode):
    """stats 为 None 时不应抛 TypeError。调用方本来就对 stats 做了 falsy 防御。"""
    assert has_valid_content(mode, None) is False


@pytest.mark.parametrize(
    "new_titles,expected",
    [
        (None, False),
        ({}, False),
        ({"zhihu": []}, False),  # 有平台但里面没有条目
        ({"zhihu": ["标题A"]}, True),
        ({"zhihu": [], "weibo": ["标题B"]}, True),  # 任一平台有即可
    ],
)
def test_daily_falls_back_to_new_titles(new_titles, expected):
    """daily 模式下热榜无命中时，靠新增条目兜底。"""
    assert has_valid_content("daily", _stats(0, 0), new_titles) is expected


def test_daily_matched_news_short_circuits():
    """热榜有命中时直接为 True，不必再看 new_titles。"""
    assert has_valid_content("daily", _stats(2), None) is True


def test_daily_all_empty():
    assert has_valid_content("daily", _stats(0, 0), {"zhihu": []}) is False


def test_unknown_mode_uses_daily_semantics():
    """兜底分支与 daily 同语义：命中或新增任一成立即可。

    锁住这个行为，避免将来有人调整分支顺序时把未预期模式变成「永不推送」。
    """
    assert has_valid_content("some_future_mode", _stats(0), {"a": ["x"]}) is True
    assert has_valid_content("some_future_mode", _stats(0), None) is False


def test_mixed_stats_partial_match():
    """只要有一个分组 count > 0 就成立。"""
    assert has_valid_content("daily", _stats(0, 0, 0, 1)) is True


# --------------------------------------------------------------------------
# has_notification_configured
# --------------------------------------------------------------------------

# 所有渠道涉及的键，缺任一键都会让函数在真实配置下抛 KeyError，
# 这里作为「最小完整配置」的基线。
_CHANNEL_KEYS = (
    "FEISHU_WEBHOOK_URL",
    "DINGTALK_WEBHOOK_URL",
    "WEWORK_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "EMAIL_FROM",
    "EMAIL_PASSWORD",
    "EMAIL_TO",
    "NTFY_SERVER_URL",
    "NTFY_TOPIC",
    "BARK_URL",
    "SLACK_WEBHOOK_URL",
    "GENERIC_WEBHOOK_URL",
)


def _cfg(**overrides):
    cfg = {k: "" for k in _CHANNEL_KEYS}
    cfg.update(overrides)
    return cfg


def test_nothing_configured():
    assert has_notification_configured(_cfg()) is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/x"},
        {"DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/x"},
        {"WEWORK_WEBHOOK_URL": "https://qyapi.weixin.qq.com/x"},
        {"BARK_URL": "https://api.day.app/x"},
        {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"},
        {"GENERIC_WEBHOOK_URL": "https://example.com/hook"},
    ],
)
def test_single_field_channels(overrides):
    """单字段渠道：填了就算配置完成。"""
    assert has_notification_configured(_cfg(**overrides)) is True


def test_telegram_needs_both_fields():
    """Telegram 是双字段联合判定，只填 token 不算配置完成。"""
    assert has_notification_configured(_cfg(TELEGRAM_BOT_TOKEN="123:abc")) is False
    assert has_notification_configured(_cfg(TELEGRAM_CHAT_ID="-100")) is False
    assert (
        has_notification_configured(
            _cfg(TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHAT_ID="-100")
        )
        is True
    )


def test_email_needs_all_three_fields():
    assert has_notification_configured(_cfg(EMAIL_FROM="a@b.com")) is False
    assert has_notification_configured(_cfg(EMAIL_FROM="a@b.com", EMAIL_TO="c@d.com")) is False
    assert (
        has_notification_configured(
            _cfg(EMAIL_FROM="a@b.com", EMAIL_PASSWORD="pw", EMAIL_TO="c@d.com")
        )
        is True
    )


def test_ntfy_needs_both_fields():
    assert has_notification_configured(_cfg(NTFY_SERVER_URL="https://ntfy.sh")) is False
    assert has_notification_configured(_cfg(NTFY_TOPIC="mytopic")) is False
    assert (
        has_notification_configured(
            _cfg(NTFY_SERVER_URL="https://ntfy.sh", NTFY_TOPIC="mytopic")
        )
        is True
    )


def test_whitespace_only_is_treated_as_configured():
    """当前实现用真值判断，纯空白字符串会被视为已配置。

    固化现状：上游配置校验负责拦住空白值，本函数不做 trim。
    """
    assert has_notification_configured(_cfg(BARK_URL="   ")) is True
