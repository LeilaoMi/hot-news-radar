# coding=utf-8
"""AICache 与翻译缓存分流的单元测试。

不触网：通过替换 AITranslator._call_ai 模拟 API 响应。
覆盖点：
- 缓存读写回环 / kind 隔离 / 空结果不缓存 / 损坏文件降级 / env 关闭
- 批量翻译：未命中送 API、命中直接回填、部分命中时 API 只收未命中条目
  （编号重排）、API 失败只影响未命中条目
"""

import re

import pytest

from trendradar.ai.cache import AICache


# --------------------------------------------------------------------------
# AICache 基础行为
# --------------------------------------------------------------------------

@pytest.fixture
def cache(tmp_path):
    c = AICache(db_path=tmp_path / "cache.db", enabled=True)
    yield c
    c.close()


def test_roundtrip(cache):
    cache.put("translate:English", "你好", "Hello")
    assert cache.get("translate:English", "你好") == "Hello"


def test_kind_isolation(cache):
    """不同用途/语言的 kind 互不污染。"""
    cache.put("translate:English", "你好", "Hello")
    assert cache.get("translate:Japanese", "你好") is None
    assert cache.get("analysis", "你好") is None


def test_overwrite_same_key(cache):
    cache.put("k", "c", "v1")
    cache.put("k", "c", "v2")
    assert cache.get("k", "c") == "v2"


def test_empty_result_not_cached(cache):
    cache.put("translate:English", "你好", "   ")
    assert cache.get("translate:English", "你好") is None


def test_disabled_is_noop(tmp_path):
    c = AICache(db_path=tmp_path / "cache.db", enabled=False)
    c.put("k", "content", "result")
    assert c.get("k", "content") is None
    c.close()


def test_corrupted_db_degrades_silently(tmp_path):
    """缓存文件损坏时整体降级为禁用，绝不抛异常。"""
    db = tmp_path / "cache.db"
    db.write_bytes(b"this is definitely not a sqlite file")
    c = AICache(db_path=db, enabled=True)
    assert c.enabled is False
    assert c.get("k", "v") is None
    c.put("k", "v", "r")  # 静默忽略
    c.close()


def test_env_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CACHE_ENABLED", "false")
    c = AICache(db_path=tmp_path / "cache.db")
    assert c.enabled is False
    c.close()


def test_clear(cache):
    cache.put("k", "c1", "v1")
    cache.put("k", "c2", "v2")
    assert cache.clear() == 2
    assert cache.get("k", "c1") is None


# --------------------------------------------------------------------------
# 翻译器缓存分流
# --------------------------------------------------------------------------

class _FakeTranslator:
    """跳过 __init__（避免 AIClient/prompt 文件依赖），手工装配最小状态。"""

    def __new__(cls, tmp_path, api_response=None, api_error=None):
        from trendradar.ai.translator import AITranslator

        t = AITranslator.__new__(AITranslator)
        t.enabled = True
        t.target_language = "English"
        t.scope = {"HOTLIST": True, "RSS": True, "STANDALONE": True}
        t.translation_config = {"ENABLED": True}
        t.ai_config = {}
        t.cache = AICache(db_path=tmp_path / "t.db", enabled=True)
        # AIClient 桩：只暴露 api_key 判定，避免真实 litellm 依赖
        t.client = type("_StubClient", (), {"api_key": "test-key"})()
        t.system_prompt = "sys prompt"
        t.user_prompt_template = "{target_language}:\n{content}"
        t.api_calls = []
        t.api_response = api_response
        t.api_error = api_error

        def fake_call_ai(user_prompt):
            if t.api_error is not None:
                raise t.api_error
            t.api_calls.append(user_prompt)
            return t.api_response

        t._call_ai = fake_call_ai
        return t


def _numbered_response(texts):
    return "\n".join(f"[{i}] T({txt})" for i, txt in enumerate(texts, 1))


def test_batch_all_uncached_then_all_cached(tmp_path):
    t = _FakeTranslator(tmp_path, api_response=_numbered_response(["标题一", "标题二"]))

    first = t.translate_batch(["标题一", "标题二"])
    assert first.success_count == 2
    assert [r.translated_text for r in first.results] == ["T(标题一)", "T(标题二)"]
    assert len(t.api_calls) == 1

    # 第二次：全部命中缓存，零 API 调用
    second = t.translate_batch(["标题一", "标题二"])
    assert second.success_count == 2
    assert [r.translated_text for r in second.results] == ["T(标题一)", "T(标题二)"]
    assert len(t.api_calls) == 1  # 未增加


def test_batch_partial_hit_renumbers_uncached(tmp_path):
    """部分命中时，API 只收未命中条目，且编号从 1 重排。"""
    t = _FakeTranslator(tmp_path, api_response=_numbered_response(["标题二"]))
    t.cache.put("translate:English", "标题一", "CACHED-1")

    result = t.translate_batch(["标题一", "标题二"])
    assert result.success_count == 2
    assert result.results[0].translated_text == "CACHED-1"
    assert result.results[1].translated_text == "T(标题二)"
    # API 只收到 1 条，编号为 [1]
    assert len(t.api_calls) == 1
    assert "[1] 标题二" in t.api_calls[0]
    assert "标题一" not in t.api_calls[0].split("\n", 1)[1]


def test_batch_api_failure_only_affects_uncached(tmp_path):
    """API 挂掉时，缓存命中条目保持成功，未命中条目标记失败。"""
    t = _FakeTranslator(tmp_path, api_error=RuntimeError("boom"))
    t.cache.put("translate:English", "标题一", "CACHED-1")

    result = t.translate_batch(["标题一", "标题二"])
    assert result.results[0].success is True
    assert result.results[0].translated_text == "CACHED-1"
    assert result.results[1].success is False
    assert "RuntimeError" in result.results[1].error
    assert result.fail_count == 1


def test_batch_empty_translation_falls_back(tmp_path):
    """AI 返回缺号时回退原文，且空译文不写缓存。"""
    t = _FakeTranslator(tmp_path, api_response="[1] T(标题一)\n")  # 缺 [2]

    result = t.translate_batch(["标题一", "标题二"])
    assert result.results[0].translated_text == "T(标题一)"
    assert result.results[1].translated_text == "标题二"  # 回退原文
    # 空译文未入缓存
    assert t.cache.get("translate:English", "标题二") is None


def test_single_translate_cache(tmp_path):
    t = _FakeTranslator(tmp_path, api_response="Hello")
    r1 = t.translate("你好")
    assert r1.translated_text == "Hello"
    r2 = t.translate("你好")
    assert r2.translated_text == "Hello"
    assert len(t.api_calls) == 1  # 第二次走缓存
