"""调度器窗口逻辑与 once 持久化测试（窗口 06:00–10:00、19:00–23:59，Asia/Shanghai）。"""
from datetime import datetime

import pytest
import pytz

from trendradar.core.scheduler import DEFAULT_ONCE_KEY, Scheduler
from trendradar.storage.local import LocalStorageBackend

TZ = pytz.timezone("Asia/Shanghai")


def timeline(extra_periods=None, plan=None):
    periods = {
        "morning": {"name": "早班", "start": "06:00", "end": "10:00", "push": True,
                    "report_mode": "current", "once": {"push": True, "analyze": True}},
        "evening": {"name": "晚间", "start": "19:00", "end": "23:59", "push": True,
                    "report_mode": "daily", "ai_mode": "daily", "once": {"push": True, "analyze": True}},
    }
    periods.update(extra_periods or {})
    return {
        "custom": {
            "default": {"collect": True, "analyze": False, "push": False, "report_mode": "current",
                        "once": {"analyze": True, "push": True}},
            "periods": periods,
            "day_plans": {"all": {"periods": plan or ["morning", "evening"]}},
            "week_map": {i: "all" for i in range(1, 8)},
        }
    }


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def make_scheduler(storage, clock, tl=None):
    return Scheduler(
        schedule_config={"enabled": True, "preset": "custom"},
        timeline_data=tl or timeline(),
        storage_backend=storage,
        get_time_func=clock,
    )


def at(h, m, day=18):
    return TZ.localize(datetime(2026, 10, day, h, m))


@pytest.mark.parametrize(
    "hh,mm,expected",
    [
        (5, 59, None),
        (6, 0, "morning"),
        (9, 59, "morning"),
        (10, 0, None),
        (18, 59, None),
        (19, 0, "evening"),
        (21, 30, "evening"),
        (23, 58, "evening"),
        (23, 59, None),  # 半开区间 [start, end)
        (0, 30, None),
    ],
)
def test_window_boundaries(hh, mm, expected):
    sch = make_scheduler(None, Clock(at(hh, mm)))
    resolved = sch.resolve()
    assert resolved.period_key == expected
    if expected == "evening":
        assert resolved.report_mode == "daily" and resolved.push and resolved.once_push
        assert resolved.ai_mode == "daily" and resolved.ai_mode_explicit
    if expected is None:
        assert resolved.push is False
        assert resolved.once_key == DEFAULT_ONCE_KEY


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "output"


def _backend(data_dir):
    return LocalStorageBackend(data_dir=str(data_dir), enable_txt=False, enable_html=False)


def test_once_is_read_back_from_persisted_db_by_a_new_process(data_dir):
    """模拟 CI：第一次运行写 once 记录后进程结束；下一次运行用全新的后端实例读同一个 output/。"""
    clock = Clock(at(19, 10))
    b1 = _backend(data_dir)
    s1 = make_scheduler(b1, clock)
    assert not s1.already_executed("evening", "push", "2026-10-18")
    s1.record_execution("evening", "push", "2026-10-18")
    b1.cleanup()

    # 新进程：新后端、新调度器，只共享磁盘上的 output/
    assert (data_dir / "news" / "2026-10-18.db").exists()
    clock2 = Clock(at(22, 40))
    b2 = _backend(data_dir)
    s2 = make_scheduler(b2, clock2)
    assert s2.already_executed("evening", "push", "2026-10-18")
    assert not s2.already_executed("morning", "push", "2026-10-18")
    assert not s2.already_executed("evening", "analyze", "2026-10-18")
    b2.cleanup()

    # 次日：新的窗口
    b3 = _backend(data_dir)
    s3 = make_scheduler(b3, Clock(at(19, 5, day=19)))
    assert not s3.already_executed("evening", "push", "2026-10-19")
    b3.cleanup()


def test_cross_midnight_window_once_uses_window_start_date(data_dir):
    tl = timeline(
        extra_periods={"late": {"name": "夜猫", "start": "22:00", "end": "01:00", "push": True,
                                "report_mode": "current", "once": {"push": True}}},
        plan=["morning", "late"],
    )
    b = _backend(data_dir)
    clock = Clock(at(22, 30, day=18))
    sch = make_scheduler(b, clock, tl)
    assert sch.resolve().period_key == "late"
    sch.record_execution("late", "push", "2026-10-18")

    # 过午夜仍在同一窗口：不能因为日期变了再推一次
    clock.dt = at(0, 30, day=19)
    assert sch.resolve().period_key == "late"
    assert sch.already_executed("late", "push", "2026-10-19")

    # 次日 22:00 是新窗口
    clock.dt = at(22, 5, day=19)
    assert not sch.already_executed("late", "push", "2026-10-19")
    b.cleanup()


def test_clear_execution_allows_reanalysis(data_dir):
    b = _backend(data_dir)
    sch = make_scheduler(b, Clock(at(19, 30)))
    sch.record_execution("evening", "analyze", "2026-10-18")
    assert sch.already_executed("evening", "analyze", "2026-10-18")
    assert sch.clear_execution("evening", "analyze", "2026-10-18")
    assert not sch.already_executed("evening", "analyze", "2026-10-18")
    b.cleanup()


def test_unquoted_yaml_time_gives_friendly_error():
    tl = timeline()
    tl["custom"]["periods"]["evening"]["start"] = 1230  # YAML 把 20:30 解析成 1230
    with pytest.raises(ValueError, match="加引号"):
        make_scheduler(None, Clock(at(12, 0)), tl)


def test_invalid_report_mode_rejected():
    tl = timeline()
    tl["custom"]["periods"]["evening"]["report_mode"] = "Daily"
    with pytest.raises(ValueError, match="report_mode"):
        make_scheduler(None, Clock(at(12, 0)), tl)


def test_string_week_map_keys_are_normalized():
    tl = timeline()
    tl["custom"]["week_map"] = {str(i): "all" for i in range(1, 8)}
    sch = make_scheduler(None, Clock(at(7, 0)), tl)
    assert sch.resolve().period_key == "morning"


def test_disabled_schedule_falls_back():
    sch = Scheduler({"enabled": False}, timeline(), None, Clock(at(7, 0)), fallback_report_mode="daily")
    r = sch.resolve()
    assert r.push and r.report_mode == "daily" and r.once_key is None
