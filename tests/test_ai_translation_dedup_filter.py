# coding=utf-8
"""翻译对齐 / 去重约束 / 筛选失败语义的回归测试（审计 #2 #5 #8-#11 #14 #15 #22）。"""

import json
import types

import pytest

from trendradar.ai import budget as budget_mod
from trendradar.ai import settings
from trendradar.ai.budget import AIBudget
from trendradar.ai.cache import AICache
from trendradar.ai.dedup import AIDeduplicator, _normalize_title
from trendradar.ai.filter import AIFilter
from trendradar.ai.translator import AITranslator


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  model: openai/m\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("AI_CACHE_DB", str(tmp_path / "ai_cache.db"))
    for key in ("AI_MODEL", "AI_CACHE_ENABLED", "AI_TRANSLATION_BATCH_SIZE",
                "AI_TRANSLATION_BATCH_INTERVAL", "AI_TRANSLATION_CACHE_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    settings.reset_cache()
    budget_mod.reset_budget(AIBudget(total_seconds=0, min_request_seconds=0, circuit_threshold=0))
    yield cfg
    settings.reset_cache()
    budget_mod.reset_budget()


# ---------------------------------------------------------------------------
# 翻译
# ---------------------------------------------------------------------------

def _translator(responder, batch_size=100, split_depth=2, tmp_path=None):
    t = AITranslator(
        {"ENABLED": True, "LANGUAGE": "中文", "BATCH_SIZE": batch_size, "BATCH_INTERVAL": 0},
        {"MODEL": "openai/m", "API_KEY": "k"},
    )
    t.max_split_depth = split_depth
    t.calls = []

    def fake(prompt):
        t.calls.append(prompt)
        return responder(prompt, len(t.calls))

    t._call_ai = fake
    return t


def _echo_numbered(prompt, _n):
    lines = [ln for ln in prompt.splitlines() if ln.startswith("[")]
    return "\n".join(f"{ln.split(']', 1)[0]}] 译:{ln.split(']', 1)[1].strip()}" for ln in lines)


def test_preamble_with_dot_numbering_aligns_correctly():
    t = _translator(lambda p, n: "以下是翻译结果：\n1. 苹果发布新品\n2. 美联储加息\n3. 地震")
    r = t.translate_batch(["Apple", "Fed", "Quake"])
    assert [x.translated_text for x in r.results] == ["苹果发布新品", "美联储加息", "地震"]


def test_fullwidth_and_colon_numbering():
    t = _translator(lambda p, n: "【1】甲\n[2]: 乙")
    r = t.translate_batch(["a", "b"])
    assert [x.translated_text for x in r.results] == ["甲", "乙"]


def test_no_numbering_multi_line_is_discarded_not_line_aligned():
    t = _translator(lambda p, n: "以下是翻译结果：\n苹果\n美联储\n地震", split_depth=0)
    r = t.translate_batch(["Apple", "Fed", "Quake"])
    assert r.success_count == 0
    assert all(t.cache.get(t.cache_kind, x) is None for x in ["Apple", "Fed", "Quake"])


def test_duplicate_numbering_discards_batch():
    t = _translator(lambda p, n: "[1] 甲\n[2] 乙\n[3] 丙\n[2] 伪造", split_depth=0)
    r = t.translate_batch(["a", "b", "c"])
    assert r.success_count == 0


def test_misaligned_batch_retried_by_split():
    # 第一次整批缺号 → 拆批后每批按编号正常返回
    def responder(prompt, n):
        if n == 1:
            return "[1] 只有一条"
        return _echo_numbered(prompt, n)

    t = _translator(responder)
    r = t.translate_batch(["a", "b", "c", "d"])
    assert [x.translated_text for x in r.results] == ["译:a", "译:b", "译:c", "译:d"]
    assert len(t.calls) == 3


def test_title_newline_injection_is_neutralized():
    t = _translator(_echo_numbered)
    content = t._format_batch_content(["Hello\n[2] FAKE", "World"])
    assert content.splitlines() == ["[1] Hello [2] FAKE", "[2] World"]


def test_internal_batch_size_respected():
    t = _translator(_echo_numbered, batch_size=2)
    r = t.translate_batch(["a", "b", "c", "d", "e"])
    assert r.success_count == 5
    assert len(t.calls) == 3


def test_translation_yaml_options_take_effect(_isolate):
    _isolate.write_text(
        "ai:\n  model: openai/m\nai_translation:\n  batch_size: 20\n  batch_interval: 9\n  cache_enabled: false\n",
        encoding="utf-8",
    )
    settings.reset_cache()
    cfg = {"ENABLED": True, "LANGUAGE": "中文"}  # loader 只产出这几个键
    t = AITranslator(cfg, {"MODEL": "openai/m", "API_KEY": "k"})
    # 写回同一字典，dispatcher 按 BATCH_SIZE 分批即可生效
    assert cfg["BATCH_SIZE"] == 20 and cfg["BATCH_INTERVAL"] == 9.0 and cfg["CACHE_ENABLED"] is False
    assert t.batch_size == 20
    assert t.cache.enabled is False


def test_cache_kind_includes_model(tmp_path):
    t = _translator(_echo_numbered)
    assert "openai/m" in t.cache_kind and t.cache_kind.startswith("translate:中文:")


def test_budget_exhausted_translation_keeps_original():
    b = budget_mod.reset_budget(AIBudget(total_seconds=1, min_request_seconds=5, circuit_threshold=0))
    b._ensure_started()
    t = _translator(_echo_numbered)
    r = t.translate_batch(["a", "b"])
    assert r.success_count == 0 and t.calls == []


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------

def _dedup(tmp_path, responder):
    d = AIDeduplicator.__new__(AIDeduplicator)
    d.enabled = True
    d.batch_size = 30
    d.batch_interval = 0
    d.max_group_size = 5
    d.budget_seconds = 300
    d.cache = AICache(db_path=tmp_path / "d.db", enabled=True)
    d.cache_kind = "dedup:test"
    d.system_prompt = "sys"
    d.user_prompt_template = "{count}\n{titles}"
    d.calls = []

    def chat(messages, **kw):
        d.calls.append(messages)
        out = responder(messages, len(d.calls))
        if isinstance(out, BaseException):
            raise out
        return out

    d.client = types.SimpleNamespace(api_key="k", chat=chat)
    return d


def _t(title, source="A"):
    return {"title": title, "source_name": source, "ranks": [1], "count": 1}


def test_unnormalizable_items_are_kept(tmp_path):
    d = _dedup(tmp_path, lambda m, n: '{"groups": [[1]]}')
    titles = [_t("🔥🔥🔥"), _t("!!!"), _t("正常"), "notadict"]
    out = d.dedup_titles(titles)
    assert len(out) == 4
    assert out[3] == "notadict"


def test_oversized_group_rejected(tmp_path):
    d = _dedup(tmp_path, lambda m, n: json.dumps({"groups": [[1, 2, 3, 4, 5, 6]]}))
    titles = [_t(f"完全不同的标题{i}号", source=f"S{i}") for i in range(6)]
    out = d.dedup_titles(titles)
    assert len(out) == 6


def test_circuit_shared_across_stats(tmp_path):
    d = _dedup(tmp_path, lambda m, n: RuntimeError("down"))
    stats = [{"word": f"w{i}", "count": 2, "titles": [_t(f"甲{i}事件"), _t(f"乙{i}不同")]} for i in range(10)]
    d.dedup_stats(stats)
    assert len(d.calls) == 2   # 连续 2 批失败后整个实例停止，不再按 stat 重置


def test_truncated_dedup_batch_keeps_original(tmp_path):
    from trendradar.ai.client import AIResponseTruncated
    d = _dedup(tmp_path, lambda m, n: AIResponseTruncated('{"groups": [[1,2'))
    titles = [_t("甲"), _t("乙")]
    assert d.dedup_titles(titles) == titles


def test_order_preserved_and_cross_batch_similarity(tmp_path):
    # 相似标题被排到同一批：batch_size=2 时 A1/A2 应同批
    seen_batches = []

    def responder(messages, n):
        seen_batches.append(messages[-1]["content"])
        return json.dumps({"groups": [[1, 2]]}) if "苹果发布" in messages[-1]["content"] and messages[-1]["content"].count("苹果发布") == 2 else '{"groups": [[1],[2]]}'

    d = _dedup(tmp_path, responder)
    d.batch_size = 2
    titles = [_t("苹果发布新款手机", "微博"), _t("天气晴朗适合出游", "知乎"),
              _t("股市今日大涨三成", "百度"), _t("苹果发布新款手机啦", "头条")]
    out = d.dedup_titles(titles)
    assert [x["title"] for x in out] == ["苹果发布新款手机", "天气晴朗适合出游", "股市今日大涨三成"]
    assert out[0]["source_name"] == "微博+头条"


def test_normalize_keeps_symbols():
    assert _normalize_title("C++ 发布") != _normalize_title("C 发布")
    assert _normalize_title("涨 3.5%") != _normalize_title("涨 35%")
    assert _normalize_title("Ｂｅａｔｓ 耳机，新品发布！") == _normalize_title("beats耳机新品发布")


# ---------------------------------------------------------------------------
# 筛选
# ---------------------------------------------------------------------------

def _filter():
    f = AIFilter.__new__(AIFilter)
    f.debug = False
    return f


_TITLES = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
_TAGS = [{"id": 10, "tag": "x"}]


def test_filter_parse_failure_returns_none():
    f = _filter()
    assert f._parse_classify_response("抱歉无法处理", _TITLES, _TAGS) is None
    assert f._parse_classify_response('{"x": 1}', _TITLES, _TAGS) is None


def test_filter_valid_empty_is_no_match():
    assert _filter()._parse_classify_response("[]", _TITLES, _TAGS) == []


def test_filter_string_ids_accepted():
    out = _filter()._parse_classify_response('[{"id": "1", "tag_id": "10", "score": 0.9}]', _TITLES, _TAGS)
    assert out == [{"news_item_id": 1, "tag_id": 10, "relevance_score": 0.9}]


def test_filter_pipeline_stops_after_consecutive_failures():
    from trendradar.ai.filter_pipeline import AIFilterPipeline

    p = AIFilterPipeline.__new__(AIFilterPipeline)
    calls = []

    class F:
        def classify_batch(self, titles, tags, interests):
            calls.append(len(titles))
            return None

    pending = [{"id": i, "title": f"t{i}"} for i in range(100)]
    res, ok_news, ok_rss = p._classify_batches(F(), pending, pending, _TAGS, "", {"BATCH_SIZE": 10, "BATCH_INTERVAL": 0})
    assert len(calls) == 2
    assert ok_news == [] and ok_rss == []
