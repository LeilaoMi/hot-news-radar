"""trendradar/core/frequency.py 的单元测试。

词频规则决定「哪些新闻进报告」，是配置驱动的核心逻辑。
重点覆盖正则解析、显示名称、以及含正则元字符的普通词（如 C++、.NET）这类易踩的坑。
"""
import pytest

from trendradar.core.frequency import (
    _parse_word,
    _word_matches,
    matches_word_groups,
)


# --------------------------------------------------------------------------
# _parse_word —— 规则解析
# --------------------------------------------------------------------------
def test_parse_plain_word():
    r = _parse_word("京东")
    assert r["is_regex"] is False
    assert r["word"] == "京东"
    assert r["display_name"] is None


def test_parse_regex():
    r = _parse_word("/京东|刘强东/")
    assert r["is_regex"] is True
    assert r["word"] == "京东|刘强东"
    assert r["pattern"].search("刘强东回应") is not None


def test_parse_regex_with_flags_suffix():
    """/xxx/i 的后缀 flag 应被容忍（源码里明确忽略 flags）。"""
    r = _parse_word("/jd|jingdong/i")
    assert r["is_regex"] is True
    assert r["word"] == "jd|jingdong"


def test_parse_display_name():
    r = _parse_word("/京东|刘强东/ => 京东")
    assert r["is_regex"] is True
    assert r["display_name"] == "京东"


def test_parse_display_name_on_plain_word():
    r = _parse_word("比特币 => BTC")
    assert r["is_regex"] is False
    assert r["word"] == "比特币"
    assert r["display_name"] == "BTC"


def test_parse_empty_display_name_is_ignored():
    """'=>' 右边为空时不应产出空字符串的 display_name。"""
    r = _parse_word("京东 =>")
    assert r["display_name"] is None


def test_parse_invalid_regex_degrades_to_literal():
    """非法正则不能让程序崩溃，应降级成普通子串匹配。"""
    r = _parse_word("/[unclosed/")
    assert r["is_regex"] is False
    assert r["word"] == "/[unclosed/"


# --------------------------------------------------------------------------
# _word_matches
# --------------------------------------------------------------------------
def test_word_matches_plain_string_case_insensitive():
    """传纯字符串时按子串匹配，且调用方已 lowercase 标题。"""
    assert _word_matches("京东", "京东618大促") is True
    assert _word_matches("JD", "jd.com") is True
    assert _word_matches("京东", "淘宝促销") is False


def test_word_matches_dict_substring():
    cfg = _parse_word("京东")
    assert _word_matches(cfg, "京东物流") is True


def test_word_matches_dict_regex():
    cfg = _parse_word("/京东|刘强东/")
    assert _word_matches(cfg, "刘强东现身") is True
    assert _word_matches(cfg, "马化腾发言") is False


def test_word_matches_regex_metacharacters_are_literal_when_plain():
    """普通词含正则元字符时必须按字面匹配，不能当正则解释。"""
    cfg = _parse_word("C++")
    assert _word_matches(cfg, "c++ 新标准发布") is True
    assert _word_matches(cfg, "c 语言教程") is False  # C++ 不该退化成 C


def test_word_matches_dotnet_literal():
    cfg = _parse_word(".NET")
    assert _word_matches(cfg, ".net 8 发布") is True
    assert _word_matches(cfg, "xnet 公司") is False  # '.' 必须是字面点


# --------------------------------------------------------------------------
# matches_word_groups
# --------------------------------------------------------------------------
def _group(required=None, normal=None):
    return {"required": required or [], "normal": normal or []}


def test_empty_title_never_matches():
    assert matches_word_groups("", [_group(normal=["京东"])], []) is False
    assert matches_word_groups("   ", [_group(normal=["京东"])], []) is False


def test_non_string_title_is_coerced():
    """title 可能是 None 或数字，不能抛异常。"""
    assert matches_word_groups(None, [_group(normal=["京东"])], []) is False
    assert matches_word_groups(12345, [_group(normal=["123"])], []) is True


def test_global_filter_has_highest_priority():
    groups = [_group(normal=["京东"])]
    assert matches_word_groups("京东广告", groups, [], global_filters=["广告"]) is False
    assert matches_word_groups("京东促销", groups, [], global_filters=["广告"]) is True


def test_no_word_groups_matches_everything():
    """没配词组 = 显示所有新闻。"""
    assert matches_word_groups("任何标题", [], []) is True


def test_filter_word_excludes():
    groups = [_group(normal=["京东"])]
    assert matches_word_groups("京东裁员", groups, filter_words=["裁员"]) is False
    assert matches_word_groups("京东扩张", groups, filter_words=["裁员"]) is True


def test_required_must_all_match():
    groups = [_group(required=["京东", "物流"], normal=[])]
    assert matches_word_groups("京东物流涨价", groups, []) is True
    assert matches_word_groups("京东商城", groups, []) is False


def test_normal_needs_any_match():
    groups = [_group(required=[], normal=["京东", "淘宝"])]
    assert matches_word_groups("淘宝双11", groups, []) is True
    assert matches_word_groups("拼多多", groups, []) is False


def test_first_matching_group_wins():
    groups = [
        _group(required=["绝不存在的词"], normal=[]),
        _group(normal=["京东"]),
    ]
    assert matches_word_groups("京东新闻", groups, []) is True


def test_no_group_matches_returns_false():
    groups = [_group(required=["缺失"], normal=[])]
    assert matches_word_groups("京东新闻", groups, []) is False


def test_regex_group_matching():
    groups = [_group(normal=[_parse_word("/京东|刘强东/")])]
    assert matches_word_groups("刘强东内部讲话", groups, []) is True
    assert matches_word_groups("王兴发言", groups, []) is False


def test_filter_word_accepts_plain_strings_for_backward_compat():
    """filter_words 允许直接给字符串（旧格式）。"""
    groups = [_group(normal=["京东"])]
    assert matches_word_groups("京东广告", groups, filter_words=["广告"]) is False
