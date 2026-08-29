"""scripts/check_daily_freshness.py 的单元测试。

这个脚本决定 CI 是否补生成「当日汇总」，是本次线上 bug 的修复核心。
它的判断依赖 datetime.now()，因此测试分两类：
  - 用相对当前时间构造报告（覆盖新鲜/过期，贴近真实运行）
  - 用伪造的 datetime 钉死时钟（覆盖跨年这类只能在特定日期复现的边界）
"""
import importlib.util
import os
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "check_daily_freshness.py")


def _load():
    """scripts/ 不是包，用 importlib 直接按路径加载，避免依赖 namespace package。"""
    spec = importlib.util.spec_from_file_location("check_daily_freshness", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def make_html(gen_dt: datetime, separate_lines: bool = True) -> str:
    """构造一段能通过 PATTERN 解析的报告片段。

    separate_lines=True 时标签与值之间带换行——这正是线上报告的真实形态，
    也是最初用 grep 抓不到、正则必须用 \\s* 的原因。
    """
    sep = "\n                        " if separate_lines else ""
    return (
        '<div class="info-item">'
        f'<span class="info-label">生成时间</span>{sep}'
        f'<span class="info-value">{gen_dt.strftime("%m-%d %H:%M")}</span>'
        "</div>"
    )


def write_report(tmp_path, gen_dt, name="daily.html", separate_lines=True):
    p = tmp_path / name
    p.write_text(make_html(gen_dt, separate_lines), encoding="utf-8")
    return str(p)


def freeze_clock(monkeypatch, when: datetime):
    """把脚本里的 datetime 替换掉，使 now() 恒定返回 when。"""
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(mod, "datetime", FakeDateTime)
    return when


# --------------------------------------------------------------------------
# 基本场景（相对当前时间）
# --------------------------------------------------------------------------
def test_fresh_report_returns_zero(tmp_path):
    """刚刚生成的报告 → 0（不需要补生成）。"""
    p = write_report(tmp_path, datetime.now() - timedelta(minutes=5))
    assert mod.check(p) == 0


def test_stale_report_returns_one(tmp_path):
    """9 天前生成的报告 → 1。"""
    p = write_report(tmp_path, datetime.now() - timedelta(days=9))
    assert mod.check(p) == 1


def test_missing_file_returns_one(tmp_path):
    assert mod.check(str(tmp_path / "nope.html")) == 1


def test_unparseable_html_returns_one(tmp_path):
    """报告里没有生成时间字段 → 1（宁可重生成，也不能沿用可疑内容）。"""
    p = tmp_path / "daily.html"
    p.write_text("<html><body>没有报告头</body></html>", encoding="utf-8")
    assert mod.check(str(p)) == 1


def test_single_line_markup_also_parses(tmp_path):
    """标签与值在同一行时同样要能解析。"""
    p = write_report(tmp_path, datetime.now(), separate_lines=False)
    assert mod.check(p) == 0


def test_invalid_date_returns_one(tmp_path):
    """非法日期（13 月 45 日）应被 ValueError 捕获并返回 1。"""
    p = tmp_path / "daily.html"
    p.write_text(
        '<span class="info-label">生成时间</span>'
        '<span class="info-value">13-45 99:99</span>',
        encoding="utf-8",
    )
    assert mod.check(p) == 1


def test_custom_threshold_is_respected(tmp_path, monkeypatch):
    """DAILY_MAX_AGE 应能覆盖默认 24 小时。"""
    monkeypatch.setattr(mod, "MAX_AGE_SECONDS", 60)  # 1 分钟
    p = write_report(tmp_path, datetime.now() - timedelta(minutes=10))
    assert mod.check(p) == 1

    monkeypatch.setattr(mod, "MAX_AGE_SECONDS", 48 * 3600)  # 48 小时
    assert mod.check(p) == 0


# --------------------------------------------------------------------------
# 时钟钉死后的边界场景
# --------------------------------------------------------------------------
def test_cross_year_treated_as_stale(tmp_path, monkeypatch):
    """当前 2026-01-01，报告标着 12-31（实为去年）→ 必须判为需重生成。

    replace(year=当前年) 会把它解释成「今年 12-31」，即未来时间，age 为负。
    """
    freeze_clock(monkeypatch, datetime(2026, 1, 1, 12, 0, 0))
    p = tmp_path / "daily.html"
    p.write_text(
        '<span class="info-label">生成时间</span>'
        '<span class="info-value">12-31 23:00</span>',
        encoding="utf-8",
    )
    assert mod.check(p) == 1


def test_future_timestamp_treated_as_stale(tmp_path, monkeypatch):
    """时钟回拨导致报告时间晚于当前时间 → 按异常处理，返回 1。"""
    freeze_clock(monkeypatch, datetime(2026, 6, 1, 12, 0, 0))
    p = tmp_path / "daily.html"
    p.write_text(
        '<span class="info-label">生成时间</span>'
        '<span class="info-value">06-01 18:00</span>',
        encoding="utf-8",
    )
    assert mod.check(p) == 1


def test_exactly_at_boundary_is_fresh(tmp_path, monkeypatch):
    """恰好等于阈值：不算过期（用的是 > 而非 >=）。"""
    now = datetime(2026, 6, 1, 12, 0, 0)
    freeze_clock(monkeypatch, now)
    monkeypatch.setattr(mod, "MAX_AGE_SECONDS", 2 * 3600)
    p = write_report(tmp_path, now - timedelta(hours=2))
    assert mod.check(p) == 0


def test_just_past_boundary_is_stale(tmp_path, monkeypatch):
    now = datetime(2026, 6, 1, 12, 0, 0)
    freeze_clock(monkeypatch, now)
    monkeypatch.setattr(mod, "MAX_AGE_SECONDS", 2 * 3600)
    p = write_report(tmp_path, now - timedelta(hours=2, seconds=1))
    assert mod.check(p) == 1


def test_unreadable_file_returns_one(tmp_path):
    """路径存在但读不了（例如是目录）时也不应抛异常。"""
    d = tmp_path / "daily.html"
    d.mkdir()
    assert mod.check(str(d)) == 1
