# coding=utf-8
"""调度器：翻译只做一次并被所有渠道 / 后续调用复用；多账号解析；不发出真实请求"""

import pytest

from trendradar.notification import dispatcher as dispatcher_mod
from trendradar.notification.dispatcher import (
    NotificationDispatcher,
    parse_accounts,
    parse_webhook_templates,
    reset_translation_memo,
)

from test_notification_helpers import make_report, make_title, splitter


class _Res:
    def __init__(self, original, translated, success=True):
        self.original_text = original
        self.translated_text = translated
        self.success = success
        self.error = "" if success else "boom"


class _Batch:
    def __init__(self, results):
        self.results = results
        self.success_count = sum(1 for r in results if r.success)
        self.fail_count = len(results) - self.success_count
        self.prompt = ""
        self.raw_response = ""
        self.parsed_count = len(results)


class FakeTranslator:
    enabled = True
    target_language = "English"
    scope = {"HOTLIST": True, "RSS": True, "STANDALONE": True}

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def translate_batch(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            return _Batch([_Res(t, "", success=False) for t in texts])
        return _Batch([_Res(t, f"EN:{t}") for t in texts])


@pytest.fixture(autouse=True)
def _reset():
    reset_translation_memo()
    yield
    reset_translation_memo()


def _config(**extra):
    cfg = {"AI_TRANSLATION": {"BATCH_SIZE": 100, "BATCH_INTERVAL": 0}, "DISPLAY": {"REGIONS": {}}}
    cfg.update(extra)
    return cfg


def _dispatcher(translator, **cfg):
    return NotificationDispatcher(_config(**cfg), get_time_func=None, split_content_func=splitter(),
                                  translator=translator)


def _report():
    return make_report([make_title("标题一"), make_title("标题二")])


def test_hotlist_translated_once_across_html_and_push_phases():
    translator = FakeTranslator()
    # HTML 阶段（__main__ 里的 translate_report_func）
    html_rd, _, _, _ = _dispatcher(translator).translate_content(
        report_data=_report(), skip_rss=True, skip_standalone=True
    )
    assert len(translator.calls) == 1
    # 推送阶段：新的调度器实例，同一批标题
    push_rd, _, _, _ = _dispatcher(translator).translate_content(
        report_data=_report(), skip_rss=True, skip_standalone=True
    )
    assert len(translator.calls) == 1, "推送阶段不应再次调用 AI"
    assert push_rd["stats"][0]["titles"][0]["title"] == "EN:标题一"
    assert html_rd["stats"][0]["titles"][0]["title"] == push_rd["stats"][0]["titles"][0]["title"]


def test_only_new_titles_sent_to_ai():
    translator = FakeTranslator()
    _dispatcher(translator).translate_content(report_data=_report(), skip_rss=True, skip_standalone=True)
    rd = make_report([make_title("标题一"), make_title("全新标题")])
    _dispatcher(translator).translate_content(report_data=rd, skip_rss=True, skip_standalone=True)
    assert translator.calls[-1] == ["全新标题"]


def test_dispatch_all_translates_once_for_all_channels(monkeypatch):
    translator = FakeTranslator()
    seen = []

    def fake_sender(*args, **kwargs):
        seen.append(kwargs["report_data"]["stats"][0]["titles"][0]["title"])
        return True

    monkeypatch.setattr(dispatcher_mod, "send_to_feishu", fake_sender)
    monkeypatch.setattr(dispatcher_mod, "send_to_slack", fake_sender)
    d = _dispatcher(translator, FEISHU_WEBHOOK_URL="https://a", SLACK_WEBHOOK_URL="https://b;https://c")
    results = d.dispatch_all(_report(), "当前榜单", skip_translation=True)
    assert results == {"feishu": True, "slack": True}
    assert len(translator.calls) == 1
    assert seen == ["EN:标题一"] * 3


def test_translation_breaker_skips_later_calls():
    translator = FakeTranslator(fail=True)
    big = make_report([make_title(f"t{i}") for i in range(250)])
    _dispatcher(translator).translate_content(report_data=big, skip_rss=True, skip_standalone=True)
    calls_after_first = len(translator.calls)
    assert calls_after_first == 2  # 连续 2 批全失败后熔断
    _dispatcher(translator).translate_content(report_data=big, skip_rss=True, skip_standalone=True)
    assert len(translator.calls) == calls_after_first


def test_parse_accounts_drops_trailing_empty():
    assert parse_accounts("t1;") == ["t1"]
    assert parse_accounts(";t2") == ["", "t2"]
    assert parse_accounts("") == []
    assert parse_accounts(None) == []


def test_telegram_trailing_semicolon_still_pairs(monkeypatch):
    sent = []
    monkeypatch.setattr(dispatcher_mod, "send_to_telegram", lambda **kw: sent.append(kw["chat_id"]) or True)
    d = _dispatcher(None, TELEGRAM_BOT_TOKEN="1:a;", TELEGRAM_CHAT_ID="42")
    assert d.dispatch_all(_report(), "当前榜单") == {"telegram": True}
    assert sent == ["42"]


def test_ntfy_batch_size_configurable(monkeypatch):
    seen = {}
    monkeypatch.setattr(dispatcher_mod, "send_to_ntfy", lambda **kw: seen.update(kw) or True)
    d = _dispatcher(None, NTFY_SERVER_URL="https://ntfy.example", NTFY_TOPIC="t;", NTFY_BATCH_SIZE=3000)
    d.dispatch_all(_report(), "当前榜单")
    assert seen["batch_size"] == 3000 and seen["topic"] == "t"


def test_webhook_template_with_semicolon_not_split():
    tpl = '{"text": "{content}", "note": "a;b"}'
    assert parse_webhook_templates(tpl) == [tpl]
    assert parse_webhook_templates('{"a":"{content}"};{"b":"{content}"}') == ['{"a":"{content}"}', '{"b":"{content}"}']
    assert parse_webhook_templates("") == []


def test_no_channel_configured_sends_nothing(monkeypatch):
    called = []
    for name in ["send_to_feishu", "send_to_dingtalk", "send_to_wework", "send_to_telegram", "send_to_ntfy",
                 "send_to_bark", "send_to_slack", "send_to_generic_webhook", "send_to_email"]:
        monkeypatch.setattr(dispatcher_mod, name, lambda *a, **k: called.append(1) or True)
    assert _dispatcher(None).dispatch_all(_report(), "当前榜单") == {}
    assert called == []
