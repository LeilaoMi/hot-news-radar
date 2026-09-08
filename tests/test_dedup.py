# coding=utf-8
"""AI 语义去重测试"""

import json

from trendradar.ai.cache import AICache
from trendradar.ai.dedup import AIDeduplicator, _normalize_title


# --------------------------------------------------------------------------
# 桩装配（与 test_ai_cache.py 同模式：跳过 __init__，避免真实 litellm 依赖）
# --------------------------------------------------------------------------

class _FakeDeduplicator:

    def __new__(cls, tmp_path, api_response=None, api_error=None, prompt=True):
        d = AIDeduplicator.__new__(AIDeduplicator)
        d.enabled = True
        d.batch_size = 30
        d.batch_interval = 0
        d.cache = AICache(db_path=tmp_path / "d.db", enabled=True)
        d.system_prompt = "sys" if prompt else ""
        d.user_prompt_template = "共 {count} 条:\n{titles}" if prompt else ""
        d.api_calls = []
        d.api_response = api_response
        d.api_error = api_error

        client = type("_StubClient", (), {})()
        client.api_key = "test-key"

        def fake_chat(messages, **kwargs):
            if d.api_error is not None:
                raise d.api_error
            d.api_calls.append(messages)
            return d.api_response

        client.chat = fake_chat
        d.client = client
        return d


def _title(title, source="A", **extra):
    t = {
        "title": title,
        "source_name": source,
        "time_display": "09:30",
        "count": 1,
        "ranks": [3],
        "rank_threshold": 5,
        "url": "https://example.com",
        "mobile_url": "",
        "is_new": False,
        "rank_timeline": [],
    }
    t.update(extra)
    return t


# --------------------------------------------------------------------------
# 规范化精确去重（无 AI）
# --------------------------------------------------------------------------

def test_normalize_title():
    assert _normalize_title("Ｂｅａｔｓ  耳机，新品发布！") == _normalize_title("beats耳机新品发布")
    assert _normalize_title("") == ""


def test_normalize_exact_dedup(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response="", prompt=False)
    titles = [
        _title("Ｂｅａｔｓ 耳机，新品发布！", source="微博"),
        _title("beats耳机新品发布", source="百度"),
        _title("完全不同的一条", source="知乎"),
    ]
    merged = d.dedup_titles(titles)
    assert len(merged) == 2
    assert len(d.api_calls) == 0          # 规范化直接合并，零 AI 调用
    assert merged[0]["source_name"] == "微博+百度"
    assert merged[1]["title"] == "完全不同的一条"


# --------------------------------------------------------------------------
# AI 语义判定
# --------------------------------------------------------------------------

def test_ai_group_merge(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response=json.dumps({"groups": [[1, 3], [2]]}))
    titles = [
        _title("iPhone 正式发布", source="微博"),
        _title("天气不错", source="知乎"),
        _title("Apple launches new iPhone", source="BBC"),
    ]
    merged = d.dedup_titles(titles)
    assert len(merged) == 2
    assert len(d.api_calls) == 1
    # 组间按最早编号排序：[1,3] 在前
    assert merged[0]["title"] == "iPhone 正式发布"
    assert merged[0]["source_name"] == "微博+BBC"
    assert merged[1]["title"] == "天气不错"


def test_markdown_codeblock_response(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response='```json\n{"groups": [[1, 2]]}\n```')
    titles = [_title("甲事件", source="A"), _title("乙事件", source="B")]
    merged = d.dedup_titles(titles)
    assert len(merged) == 1
    assert merged[0]["source_name"] == "A+B"


def test_parse_groups_tolerant(tmp_path):
    # 重复编号忽略、越界忽略、非数组忽略、缺号不补并
    d = _FakeDeduplicator(tmp_path, api_response='{"groups": [[1, 2], [1], [9], "bad", [3]]}')
    titles = [_title("A", source="S1"), _title("B", source="S2"), _title("C", source="S3")]
    merged = d.dedup_titles(titles)
    assert len(merged) == 2
    assert merged[0]["source_name"] == "S1+S2"
    assert merged[1]["title"] == "C"


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------

def test_cache_hit_second_run(tmp_path):
    resp = json.dumps({"groups": [[1, 2]]})
    d1 = _FakeDeduplicator(tmp_path, api_response=resp)
    titles = [_title("标题甲", source="A"), _title("标题乙", source="B")]
    m1 = d1.dedup_titles(titles)
    assert len(d1.api_calls) == 1

    # 同一缓存库重建实例：零 API 调用，结果一致
    d2 = _FakeDeduplicator(tmp_path, api_response=resp)
    m2 = d2.dedup_titles(titles)
    assert len(d2.api_calls) == 0
    assert [t["source_name"] for t in m2] == [t["source_name"] for t in m1]


# --------------------------------------------------------------------------
# 降级路径
# --------------------------------------------------------------------------

def test_parse_failure_fallback(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response="抱歉，我无法处理这个请求。")
    titles = [_title("A"), _title("B"), _title("C")]
    assert d.dedup_titles(titles) == titles


def test_api_error_fallback(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_error=RuntimeError("boom"))
    titles = [_title("A"), _title("B")]
    assert d.dedup_titles(titles) == titles


def test_disabled(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response="{}")
    d.enabled = False
    titles = [_title("相同标题"), _title("相同标题")]
    assert d.dedup_titles(titles) == titles


def test_no_key(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response="{}")
    d.client.api_key = ""
    titles = [_title("相同标题"), _title("相同标题")]
    assert d.dedup_titles(titles) == titles


# --------------------------------------------------------------------------
# dedup_stats 与合并字段语义
# --------------------------------------------------------------------------

def test_dedup_stats_count_sync(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response=json.dumps({"groups": [[1, 2]]}))
    stats = [{
        "word": "AI",
        "count": 2,
        "percentage": 10.0,
        "titles": [_title("同事件甲"), _title("同事件乙")],
    }]
    out = d.dedup_stats(stats)
    assert out[0]["count"] == 1
    assert len(out[0]["titles"]) == 1


def test_dedup_stats_skips_single_title(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response="{}")
    stats = [{"word": "AI", "count": 1, "percentage": 5.0, "titles": [_title("唯一")]}]
    assert d.dedup_stats(stats)[0]["titles"] == [_title("唯一")]
    assert len(d.api_calls) == 0


def test_merge_group_fields(tmp_path):
    d = _FakeDeduplicator(tmp_path, api_response=json.dumps({"groups": [[1, 2, 3]]}))
    titles = [
        _title("T1", source="A", ranks=[5], count=1, url="u1", is_new=False,
               rank_timeline=[{"time": "10:00", "rank": 5}]),
        _title("T2", source="A", ranks=[1], count=3, url="", is_new=True,
               rank_timeline=[{"time": "09:00", "rank": 1}], mobile_url="m2"),
        _title("T3", source="B", ranks=[3], count=2, url="u3", is_new=False,
               rank_timeline=[{"time": "09:30", "rank": 3}]),
    ]
    merged = d.dedup_titles(titles)
    assert len(merged) == 1
    m = merged[0]
    assert m["source_name"] == "A+B"         # 来源去重保序后"+"连接
    assert m["ranks"] == [1, 3, 5]           # 并集排序
    assert m["count"] == 3                   # 取 max 不虚增
    assert m["url"] == "u1"                  # 首条非空
    assert m["mobile_url"] == "m2"           # 首个非空
    assert m["is_new"] is True               # 任一为真
    assert [x["time"] for x in m["rank_timeline"]] == ["09:00", "09:30", "10:00"]
