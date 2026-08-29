"""trendradar/utils/time.py 的单元测试。

这一层是「报告生成时间 / RSS 新鲜度过滤」的唯一真相来源，
时间解析错会直接导致页面上显示的生成时间不对，因此边界值必须全部覆盖。
"""
from datetime import datetime

import pytest
import pytz

from trendradar.utils.time import (
    DEFAULT_TIMEZONE,
    calculate_days_old,
    format_date_folder,
    format_time_filename,
    get_configured_time,
    is_within_days,
)

SHANGHAI = pytz.timezone("Asia/Shanghai")


@pytest.fixture
def fixed_now(monkeypatch):
    """把「当前时间」钉死在 2026-01-01 12:00 +08:00，让测试可重复。

    被钉住的是模块级函数 get_configured_time，
    is_within_days / calculate_days_old 内部都是通过全局名调用它的。
    """
    import trendradar.utils.time as t

    fake = SHANGHAI.localize(datetime(2026, 1, 1, 12, 0, 0))
    monkeypatch.setattr(t, "get_configured_time", lambda *a, **kw: fake)
    return fake


# --------------------------------------------------------------------------
# format_date_folder / format_time_filename
# --------------------------------------------------------------------------
def test_format_date_folder_returns_given_date():
    assert format_date_folder("2025-12-09") == "2025-12-09"


def test_format_date_folder_defaults_to_today(fixed_now):
    assert format_date_folder() == "2026-01-01"


def test_format_time_filename_has_no_colon(fixed_now):
    """Windows 文件名不允许冒号，必须替换成连字符。"""
    name = format_time_filename()
    assert ":" not in name
    assert len(name) == 5 and name[2] == "-"


def test_get_configured_time_falls_back_on_bad_timezone():
    """未知时区应降级到默认时区而不是抛异常。"""
    t = get_configured_time("Not/AZone")
    assert t.tzinfo is not None
    assert t.year >= 2025


# --------------------------------------------------------------------------
# is_within_days —— RSS 新鲜度过滤
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "iso_time,expected",
    [
        ("", True),  # 无时间戳 → 保留
        (None, True),
        ("not-a-time", True),  # 无法解析 → 保留（不误杀）
    ],
)
def test_is_within_days_keeps_unparseable(fixed_now, iso_time, expected):
    assert is_within_days(iso_time, 7) is expected


@pytest.mark.parametrize("max_days", [0, -1])
def test_is_within_days_disabled_filter(fixed_now, max_days):
    """max_days <= 0 表示关闭过滤，任何时间都应保留。"""
    assert is_within_days("2000-01-01T00:00:00", max_days) is True


def test_is_within_days_within_range(fixed_now):
    assert is_within_days("2025-12-31T00:00:00+08:00", 7) is True


def test_is_within_days_out_of_range(fixed_now):
    assert is_within_days("2025-12-01T00:00:00+08:00", 7) is False


def test_is_within_days_accepts_z_suffix(fixed_now):
    """UTC 'Z' 后缀要能被解析（2026-01-01T00:00Z ≈ 08:00 +08:00，距今 4 小时）。"""
    assert is_within_days("2026-01-01T00:00:00Z", 1) is True


def test_is_within_days_naive_iso_treated_as_utc(fixed_now):
    """不带时区的时间串按 UTC 处理。"""
    # 2025-12-31T00:00 UTC = 08:00 +08:00（12-31），距今 1天4小时 → 2 天内
    assert is_within_days("2025-12-31T00:00:00", 2) is True
    assert is_within_days("2025-12-31T00:00:00", 1) is False


def test_is_within_days_accepts_date_only(fixed_now):
    assert is_within_days("2025-12-31", 3) is True
    assert is_within_days("2025-12-01", 3) is False


def test_is_within_days_strips_microseconds(fixed_now):
    assert is_within_days("2025-12-31T10:00:00.123456+08:00", 3) is True


def test_is_within_days_future_time_is_kept(fixed_now):
    """时钟回拨 / 未来时间戳不应被过滤掉（负差值）。"""
    assert is_within_days("2026-06-01T00:00:00+08:00", 1) is True


def test_is_within_days_crosses_year_boundary(fixed_now):
    """跨年是最容易算错的地方：12-31 → 01-01 只差 1 天。"""
    assert is_within_days("2025-12-31T23:59:59+08:00", 1) is True
    assert is_within_days("2024-12-31T00:00:00+08:00", 1) is False


def test_is_within_days_exact_boundary(fixed_now):
    """恰好等于 max_days 天：应保留（<=）。"""
    assert is_within_days("2025-12-31T12:00:00+08:00", 1) is True


# --------------------------------------------------------------------------
# calculate_days_old
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", None, "garbage", "2025-13-45T99:99"])
def test_calculate_days_old_returns_none_for_invalid(bad):
    assert calculate_days_old(bad) is None


def test_calculate_days_old_hours_fraction(fixed_now):
    """12 小时前 → 0.5 天。"""
    assert calculate_days_old("2026-01-01T00:00:00+08:00") == pytest.approx(0.5)


def test_calculate_days_old_full_days(fixed_now):
    assert calculate_days_old("2025-12-29T12:00:00+08:00") == pytest.approx(3.0)


def test_calculate_days_old_future_is_negative(fixed_now):
    """未来时间返回负值，调用方需自行处理（check_daily_freshness 会判为异常）。"""
    assert calculate_days_old("2026-01-02T12:00:00+08:00") == pytest.approx(-1.0)


def test_calculate_days_old_crosses_year(fixed_now):
    assert calculate_days_old("2025-12-31T12:00:00+08:00") == pytest.approx(1.0)


def test_calculate_days_old_handles_z_suffix(fixed_now):
    # 2026-01-01T00:00Z = 08:00 +08:00 → 距今 4 小时
    assert calculate_days_old("2026-01-01T00:00:00Z") == pytest.approx(4 / 24)


# --------------------------------------------------------------------------
# 默认时区一致性
# --------------------------------------------------------------------------
def test_default_timezone_is_valid():
    assert pytz.timezone(DEFAULT_TIMEZONE) is not None
