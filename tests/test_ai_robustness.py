# coding=utf-8
"""AI 层健壮性回归测试（审计 ai.md 各项）。

全部离线：AIClient 的 litellm 调用被 monkeypatch，或直接替换 _call_ai / client.chat。
"""

import json
import sys
import time
import types

import pytest

from trendradar.ai import budget as budget_mod
from trendradar.ai import settings
from trendradar.ai.analyzer import AIAnalysisResult, AIAnalyzer, as_text, render_template
from trendradar.ai.budget import AIBudget, AIBudgetExhausted
from trendradar.ai.cache import AICache
from trendradar.ai.client import AIClient, AIResponseTruncated
from trendradar.ai import formatter


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个用例使用独立的 yaml / 预算 / 缓存路径，互不影响。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  model: openai/yaml-model\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("AI_CACHE_DB", str(tmp_path / "ai_cache.db"))
    for key in ("AI_MODEL", "AI_ANALYSIS_MODEL", "AI_TRANSLATION_MODEL", "AI_DEDUP_MODEL",
                "AI_DAILY_MODEL", "AI_FILTER_MODEL", "AI_API_BASE", "AI_CACHE_ENABLED",
                "AI_TOTAL_BUDGET_SECONDS", "AI_TRANSLATION_BATCH_SIZE"):
        monkeypatch.delenv(key, raising=False)
    settings.reset_cache()
    budget_mod.reset_budget(AIBudget(total_seconds=0, min_request_seconds=0, circuit_threshold=0))
    yield cfg
    settings.reset_cache()
    budget_mod.reset_budget()


def _write_yaml(path, text):
    path.write_text(text, encoding="utf-8")
    settings.reset_cache()


# ---------------------------------------------------------------------------
# #1 字段类型归一化 + 渲染隔离
# ---------------------------------------------------------------------------

def _bare_analyzer():
    a = AIAnalyzer.__new__(AIAnalyzer)
    return a


def test_parse_response_normalizes_list_dict_null():
    a = _bare_analyzer()
    resp = json.dumps({
        "core_trends": ["1. 甲", "2. 乙"],
        "sentiment_controversy": {"正方": "支持", "反方": ["反对1", "反对2"]},
        "signals": None,
        "rss_insights": 42,
        "outlook_strategy": [{"投资者": "观望"}],
        "standalone_summaries": {"知乎": ["要点一", "要点二"], "空": None},
    }, ensure_ascii=False)
    r = a._parse_response(resp)
    assert r.success is True
    assert r.core_trends == "1. 甲\n2. 乙"
    assert "正方：支持" in r.sentiment_controversy and "反对2" in r.sentiment_controversy
    assert r.signals == ""
    assert r.rss_insights == "42"
    assert r.outlook_strategy == "投资者：观望"
    assert r.standalone_summaries == {"知乎": "要点一\n要点二"}
    for v in (r.core_trends, r.sentiment_controversy, r.signals, r.rss_insights, r.outlook_strategy):
        assert isinstance(v, str)


def test_renderers_never_crash_on_non_string_fields():
    r = AIAnalysisResult(success=True)
    # 直接塞入非字符串（模拟绕过解析的调用方）
    r.core_trends = ["a", {"b": "c"}]
    r.signals = {"x": ["y"]}
    r.standalone_summaries = {"源": ["s1"]}
    for fn in (formatter.render_ai_analysis_markdown, formatter.render_ai_analysis_feishu,
               formatter.render_ai_analysis_dingtalk, formatter.render_ai_analysis_plain,
               formatter.render_ai_analysis_telegram, formatter.render_ai_analysis_html_rich):
        out = fn(r)
        assert isinstance(out, str) and "a" in out
    # 原对象不被渲染改写
    assert isinstance(r.core_trends, list)


def test_renderer_exception_is_isolated(monkeypatch):
    def boom(_text):
        raise ValueError("bad")
    monkeypatch.setattr(formatter, "_format_list_content", boom)
    r = AIAnalysisResult(success=True, core_trends="x")
    assert formatter.render_ai_analysis_html_rich(r) == ""
    assert formatter.render_ai_analysis_markdown(r) == ""


def test_as_text_nested():
    assert as_text([["a", "b"], None, "c"]) == "a\nb\nc"
    assert as_text(None) == ""


# ---------------------------------------------------------------------------
# #6/#7 截断与拒答不算成功
# ---------------------------------------------------------------------------

def test_refusal_is_failure():
    r = _bare_analyzer()._parse_response("抱歉，我无法完成该请求。")
    assert r.success is False


def test_json_without_known_fields_is_failure():
    r = _bare_analyzer()._parse_response('{"foo": "bar"}')
    assert r.success is False


def _analyzer_with_responses(responses):
    a = AIAnalyzer.__new__(AIAnalyzer)
    a.ai_config = {"MODEL": "openai/x"}
    a.analysis_config = {}
    a.get_time_func = lambda: __import__("datetime").datetime(2026, 1, 1, 9, 0)
    a.debug = False
    a.max_news = 50
    a.include_rss = False
    a.include_rank_timeline = False
    a.include_standalone = False
    a.language = "Chinese"
    a.system_prompt = "sys"
    a.user_prompt_template = "{news_content}"
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    a.client = types.SimpleNamespace(api_key="k", chat=fake_chat)
    a.calls = calls
    return a


_STATS = [{"word": "全部", "titles": [{"title": "新闻A", "source_name": "微博", "ranks": [1]}]}]


def test_truncated_analysis_retries_then_fails():
    a = _analyzer_with_responses([AIResponseTruncated('{"core_trends":"半'), AIResponseTruncated("x")])
    r = a.analyze(_STATS)
    assert r.success is False and r.truncated is True
    assert len(a.calls) == 2


def test_truncated_analysis_retry_success():
    good = json.dumps({"core_trends": "完整"}, ensure_ascii=False)
    a = _analyzer_with_responses([AIResponseTruncated("x"), good])
    r = a.analyze(_STATS)
    assert r.success is True and r.core_trends == "完整"


def test_budget_exhausted_analysis_is_failure():
    a = _analyzer_with_responses([AIBudgetExhausted("no time")])
    r = a.analyze(_STATS)
    assert r.success is False and "跳过" in r.error


def test_render_template_single_pass():
    tpl = "A={news_content} L={language}"
    out = render_template(tpl, {"news_content": "标题含{language}字样", "language": "Chinese"})
    assert out == "A=标题含{language}字样 L=Chinese"
    assert render_template('{"k": 1} {unknown}', {}) == '{"k": 1} {unknown}'


# ---------------------------------------------------------------------------
# AIClient：finish_reason / 预算 / 熔断 / extra_params
# ---------------------------------------------------------------------------

def _install_fake_litellm(monkeypatch, handler):
    mod = types.ModuleType("litellm")
    mod.completion = handler
    monkeypatch.setitem(sys.modules, "litellm", mod)


def _resp(content, finish="stop", reasoning=None):
    msg = types.SimpleNamespace(content=content, reasoning_content=reasoning)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason=finish)])


def test_client_truncation_raises(monkeypatch):
    _install_fake_litellm(monkeypatch, lambda **kw: _resp('{"a":', "length"))
    c = AIClient({"MODEL": "openai/x", "API_KEY": "k"})
    with pytest.raises(AIResponseTruncated):
        c.chat([{"role": "user", "content": "hi"}])
    assert c.chat_ex([{"role": "user", "content": "hi"}]).finish_reason == "length"


def test_client_extra_params_and_timeout_cap(monkeypatch):
    seen = {}

    def handler(**kw):
        seen.update(kw)
        return _resp("ok")

    _install_fake_litellm(monkeypatch, handler)
    budget_mod.reset_budget(AIBudget(total_seconds=30, min_request_seconds=1, circuit_threshold=3))
    c = AIClient({"MODEL": "openai/x", "API_KEY": "k", "TIMEOUT": 120, "NUM_RETRIES": 0,
                  "EXTRA_PARAMS": {"top_p": 0.5}})
    assert c.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert seen["top_p"] == 0.5
    assert seen["timeout"] <= 30
    assert seen["num_retries"] == 0


def test_budget_exhausted_blocks_calls(monkeypatch):
    calls = []
    _install_fake_litellm(monkeypatch, lambda **kw: calls.append(1) or _resp("ok"))
    b = budget_mod.reset_budget(AIBudget(total_seconds=10, min_request_seconds=5, circuit_threshold=3))
    b._start = time.monotonic() - 9  # 只剩 1s < min_request_seconds
    c = AIClient({"MODEL": "openai/x", "API_KEY": "k"})
    with pytest.raises(AIBudgetExhausted):
        c.chat([{"role": "user", "content": "hi"}])
    assert calls == []
    assert b.exhausted() is True


def test_circuit_breaker_opens_across_features(monkeypatch):
    def handler(**kw):
        raise TimeoutError("slow")

    _install_fake_litellm(monkeypatch, handler)
    budget_mod.reset_budget(AIBudget(total_seconds=0, min_request_seconds=0, circuit_threshold=2))
    c1 = AIClient({"MODEL": "openai/x", "API_KEY": "k", "FEATURE": "dedup"})
    c2 = AIClient({"MODEL": "openai/x", "API_KEY": "k", "FEATURE": "translation"})
    for c in (c1, c2):
        with pytest.raises(TimeoutError):
            c.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(AIBudgetExhausted):
        c1.chat([{"role": "user", "content": "hi"}])


def test_budget_from_yaml_and_env(_isolate, monkeypatch):
    _write_yaml(_isolate, "ai:\n  total_budget_seconds: 123\n")
    assert AIBudget().total_seconds == 123
    monkeypatch.setenv("AI_TOTAL_BUDGET_SECONDS", "45")
    assert AIBudget().total_seconds == 45


def test_budget_default():
    assert AIBudget().total_seconds == settings.DEFAULT_TOTAL_BUDGET_SECONDS


# ---------------------------------------------------------------------------
# #4 模型优先级
# ---------------------------------------------------------------------------

def test_model_precedence_analysis(_isolate, monkeypatch):
    _write_yaml(_isolate, "ai:\n  model: openai/yaml-model\n  analysis_model: openai/yaml-analysis\n")
    base = {"MODEL": "openai/yaml-model", "ANALYSIS_MODEL": "openai/yaml-analysis"}
    assert settings.feature_ai_config(base, "analysis")["MODEL"] == "openai/yaml-analysis"

    monkeypatch.setenv("AI_MODEL", "deepseek/deepseek-chat")
    # loader 会把 env AI_MODEL 放进 MODEL；yaml analysis_model 不得再劫持
    base_env = {"MODEL": "deepseek/deepseek-chat", "ANALYSIS_MODEL": "openai/yaml-analysis"}
    assert settings.feature_ai_config(base_env, "analysis")["MODEL"] == "deepseek/deepseek-chat"

    monkeypatch.setenv("AI_ANALYSIS_MODEL", "openai/env-analysis")
    assert settings.feature_ai_config(base_env, "analysis")["MODEL"] == "openai/env-analysis"


def test_model_precedence_translation_dedup_daily(_isolate, monkeypatch):
    _write_yaml(_isolate, (
        "ai:\n  model: openai/m\n  translation_model: openai/t\n  dedup_model: ''\n"
        "  analysis_model: openai/a\n  daily_model: ''\n"
    ))
    base = {"MODEL": "openai/m", "ANALYSIS_MODEL": "openai/a"}
    assert settings.feature_ai_config(base, "translation")["MODEL"] == "openai/t"
    assert settings.feature_ai_config(base, "dedup")["MODEL"] == "openai/m"
    assert settings.feature_ai_config(base, "daily")["MODEL"] == "openai/a"   # 回退 analysis
    monkeypatch.setenv("AI_ANALYSIS_MODEL", "openai/env-a")
    assert settings.feature_ai_config(base, "daily")["MODEL"] == "openai/env-a"
    monkeypatch.setenv("AI_DAILY_MODEL", "openai/env-d")
    assert settings.feature_ai_config(base, "daily")["MODEL"] == "openai/env-d"
    monkeypatch.setenv("AI_MODEL", "openai/env-m")
    assert settings.feature_ai_config(base, "translation")["MODEL"] == "openai/env-m"
    monkeypatch.setenv("AI_TRANSLATION_MODEL", "openai/env-t")
    assert settings.feature_ai_config(base, "translation")["MODEL"] == "openai/env-t"


def test_feature_temperature_defaults(_isolate):
    _write_yaml(_isolate, "ai:\n  model: openai/m\nai_dedup:\n  temperature: 0.05\n")
    base = {"MODEL": "openai/m", "TEMPERATURE": 0.7}
    assert settings.feature_ai_config(base, "analysis")["TEMPERATURE"] == 0.7
    assert settings.feature_ai_config(base, "translation")["TEMPERATURE"] == 0.2
    assert settings.feature_ai_config(base, "dedup")["TEMPERATURE"] == 0.05


def test_parse_bool_tristate():
    assert settings.parse_bool("on") is True
    assert settings.parse_bool("YES") is True
    assert settings.parse_bool("off") is False
    assert settings.parse_bool("ture") is None


# ---------------------------------------------------------------------------
# #12/#13 缓存路径 / 总闸 / TTL
# ---------------------------------------------------------------------------

def test_cache_env_master_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CACHE_ENABLED", "off")
    c = AICache(db_path=tmp_path / "c.db", enabled=True)
    assert c.enabled is False


def test_cache_path_precedence(_isolate, monkeypatch):
    monkeypatch.delenv("AI_CACHE_DB", raising=False)
    assert settings.cache_db_path({}) == "output/ai_cache.db"
    _write_yaml(_isolate, "ai:\n  cache_db_path: data/x.db\n")
    assert settings.cache_db_path({}) == "data/x.db"
    monkeypatch.setenv("AI_CACHE_DB", "/tmp/env.db")
    assert settings.cache_db_path({"CACHE_DB_PATH": "y.db"}) == "/tmp/env.db"


def test_cache_ttl(tmp_path):
    c = AICache(db_path=tmp_path / "c.db", enabled=True, ttl_seconds=100)
    c.put("k", "a", "v")
    c._conn.execute("UPDATE ai_cache SET created_at = ?", (time.time() - 1000,))
    c._conn.commit()
    assert c.get("k", "a") is None
    c.close()
    # 重新打开时过期条目被清理
    c2 = AICache(db_path=tmp_path / "c.db", enabled=True, ttl_seconds=100)
    assert c2._conn.execute("SELECT COUNT(*) FROM ai_cache").fetchone()[0] == 0
    c2.close()
