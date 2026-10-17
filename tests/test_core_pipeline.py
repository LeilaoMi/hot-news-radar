"""编排层（trendradar/__main__.py）回归测试：退出码、RENDER_ONLY、RSS 模式覆盖、once 跨运行。

所有测试都在 tmp_path 中运行（output/ 落在临时目录），网络请求全部打桩。
"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
import pytz

import trendradar.context as context_mod
import trendradar.storage.manager as manager_mod
from trendradar.__main__ import NewsAnalyzer, main
from trendradar.core.errors import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from trendradar.core.loader import load_config

ROOT = Path(__file__).resolve().parent.parent
TZ = pytz.timezone("Asia/Shanghai")

_ENV_TO_CLEAR = [
    "REPORT_MODE", "SCHEDULE_ENABLED", "SCHEDULE_PRESET", "RENDER_ONLY", "NOTIFICATION_ENABLED",
    "ENABLE_NOTIFICATION", "AI_ANALYSIS_ENABLED", "AI_TRANSLATION_ENABLED", "AI_DEDUP_ENABLED",
    "AI_FILTER_ENABLED", "STORAGE_BACKEND", "PLATFORM_FAILURE_THRESHOLD", "GITHUB_ACTIONS",
    "S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL",
    "FEISHU_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "BARK_URL",
    "SLACK_WEBHOOK_URL", "GENERIC_WEBHOOK_URL", "DINGTALK_WEBHOOK_URL", "WEWORK_WEBHOOK_URL",
    "EMAIL_FROM", "EMAIL_PASSWORD", "EMAIL_TO", "DEBUG", "FREQUENCY_WORDS_PATH", "STORAGE_RETENTION_DAYS",
]

PLATFORMS = [
    {"id": "p1", "name": "平台一"},
    {"id": "p2", "name": "平台二"},
    {"id": "p3", "name": "平台三"},
    {"id": "p4", "name": "平台四"},
]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """隔离的运行目录 + 干净的环境变量 + 重置存储单例。"""
    for key in _ENV_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DOCKER_CONTAINER", "true")  # 不打开浏览器
    monkeypatch.setenv("CONFIG_PATH", str(ROOT / "config" / "config.yaml"))
    freq = tmp_path / "frequency_words.txt"
    freq.write_text("", encoding="utf-8")
    monkeypatch.setenv("FREQUENCY_WORDS_PATH", str(freq))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(manager_mod, "_storage_manager", None)
    yield tmp_path
    if manager_mod._storage_manager is not None:
        try:
            manager_mod._storage_manager.cleanup()
        except Exception:
            pass
    manager_mod._storage_manager = None


def make_config(**overrides):
    cfg = load_config()
    cfg["PLATFORMS"] = [dict(p) for p in PLATFORMS]
    cfg["RSS"]["ENABLED"] = False
    cfg["AI_ANALYSIS"]["ENABLED"] = False
    cfg["AI_TRANSLATION"]["ENABLED"] = False
    cfg["AI_DEDUP"]["ENABLED"] = False
    cfg["AI_DAILY"]["ENABLED"] = False
    cfg["FILTER"]["METHOD"] = "keyword"
    cfg["ENABLE_NOTIFICATION"] = False
    cfg["SCHEDULE"] = {"enabled": False, "preset": "always_on"}
    cfg["STORAGE"]["BACKEND"] = "local"
    cfg["STORAGE"]["FORMATS"]["TXT"] = False
    cfg["STORAGE"]["LOCAL"]["RETENTION_DAYS"] = 0
    cfg["STORAGE"]["REMOTE"]["RETENTION_DAYS"] = 0
    cfg["SHOW_VERSION_UPDATE"] = False
    cfg["DEBUG"] = False
    for k, v in overrides.items():
        cfg[k] = v
    return cfg


def fake_results(ids=("p1", "p2", "p3", "p4"), tag="A"):
    results = {}
    for pid in ids:
        results[pid] = {
            f"{pid} 新闻{tag}{i}": {
                "ranks": [i],
                "url": f"https://example.com/{pid}/{tag}{i}",
                "mobileUrl": "",
            }
            for i in range(1, 4)
        }
    return results


def patch_crawl(analyzer, results, failed=()):
    names = {p["id"]: p["name"] for p in PLATFORMS}
    calls = []

    def _crawl(ids, interval, domain_rules=None):
        calls.append(ids)
        return results, dict(names), list(failed)

    analyzer.data_fetcher.crawl_websites = _crawl
    return calls


def crawl_record_count(tmp_path, date):
    db = tmp_path / "output" / "news" / f"{date}.db"
    if not db.exists():
        return 0
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM crawl_records").fetchone()[0]
    finally:
        conn.close()


def freeze_time(monkeypatch, dt):
    """把 AppContext 的「现在」固定到 dt（Asia/Shanghai）。"""
    monkeypatch.setattr(context_mod, "get_configured_time", lambda tz=None: dt)
    monkeypatch.setattr(context_mod, "format_date_folder", lambda date=None, timezone=None: dt.strftime("%Y-%m-%d"))
    monkeypatch.setattr(context_mod, "format_time_filename", lambda timezone=None: dt.strftime("%H-%M"))


# ---------------------------------------------------------------------------
# 退出码
# ---------------------------------------------------------------------------

def test_success_exit_0_and_report_written(sandbox):
    analyzer = NewsAnalyzer(config=make_config(REPORT_MODE="current"))
    patch_crawl(analyzer, fake_results())
    assert analyzer.run() == EXIT_OK
    assert (sandbox / "output" / "html" / "latest" / "current.html").exists()


def test_all_platforms_fail_is_fatal_but_still_renders_rss_only(sandbox):
    analyzer = NewsAnalyzer(config=make_config(REPORT_MODE="current"))
    patch_crawl(analyzer, {}, failed=[p["id"] for p in PLATFORMS])
    assert analyzer.run() == EXIT_FATAL
    assert any("全部" in m for m in analyzer.status.fatal_errors)
    # 降级：仍然生成报告（仅 RSS / 空热榜），而不是整轮静默失败
    assert (sandbox / "output" / "html" / "latest" / "current.html").exists()


def test_partial_failure_warns_and_exits_0_by_default(sandbox):
    analyzer = NewsAnalyzer(config=make_config(REPORT_MODE="current"))
    patch_crawl(analyzer, fake_results(ids=("p1", "p2", "p3")), failed=["p4"])
    assert analyzer.run() == EXIT_OK
    assert analyzer.status.warnings and not analyzer.status.partial_errors


def test_partial_failure_over_threshold_exits_2(sandbox):
    analyzer = NewsAnalyzer(config=make_config(REPORT_MODE="current", PLATFORM_FAILURE_THRESHOLD=0.5))
    patch_crawl(analyzer, fake_results(ids=("p1", "p2")), failed=["p3", "p4"])
    assert analyzer.run() == EXIT_PARTIAL


def test_storage_failure_is_fatal(sandbox):
    analyzer = NewsAnalyzer(config=make_config(REPORT_MODE="current"))
    patch_crawl(analyzer, fake_results())
    analyzer.storage_manager.save_news_data = lambda data: False
    assert analyzer.run() == EXIT_FATAL
    assert any("StorageError" in m for m in analyzer.status.fatal_errors)


def test_render_failure_is_fatal(sandbox):
    analyzer = NewsAnalyzer(config=make_config(REPORT_MODE="current"))
    patch_crawl(analyzer, fake_results())

    def boom(*a, **k):
        raise OSError("disk full")

    analyzer.ctx.generate_html = boom
    assert analyzer.run() == EXIT_FATAL


def test_unhandled_error_in_main_returns_nonzero(sandbox, monkeypatch):
    def boom(self):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(NewsAnalyzer, "run", boom)
    monkeypatch.setattr("trendradar.__main__.check_all_versions", lambda *a, **k: (False, None))
    assert main([]) == EXIT_FATAL


def test_missing_config_returns_nonzero(sandbox, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(sandbox / "nope.yaml"))
    assert main([]) == EXIT_FATAL


def test_invalid_report_mode_returns_nonzero(sandbox, monkeypatch):
    monkeypatch.setenv("REPORT_MODE", "Daily")
    monkeypatch.setattr("trendradar.__main__.check_all_versions", lambda *a, **k: (False, None))
    assert main([]) == EXIT_FATAL


def test_all_push_channels_failing_is_not_reported_as_success(sandbox, monkeypatch):
    cfg = make_config(REPORT_MODE="current")
    cfg["ENABLE_NOTIFICATION"] = True
    cfg["BARK_URL"] = "https://bark.example.com/key"
    analyzer = NewsAnalyzer(config=cfg)
    patch_crawl(analyzer, fake_results())

    class FakeDispatcher:
        def dispatch_all(self, **kwargs):
            return {"bark": False}

    analyzer.ctx.create_notification_dispatcher = lambda: FakeDispatcher()
    assert analyzer.run() == EXIT_OK  # 推送失败只告警
    assert any("推送" in w for w in analyzer.status.warnings)


# ---------------------------------------------------------------------------
# RENDER_ONLY
# ---------------------------------------------------------------------------

def test_render_only_without_data_fails(sandbox):
    cfg = make_config(REPORT_MODE="daily", RENDER_ONLY=True)
    analyzer = NewsAnalyzer(config=cfg)
    patch_crawl(analyzer, fake_results())
    assert analyzer.run() == EXIT_FATAL


def test_render_only_renders_daily_from_storage_without_side_effects(sandbox, monkeypatch):
    now = TZ.localize(datetime(2026, 10, 18, 21, 5))
    freeze_time(monkeypatch, now)
    date = now.strftime("%Y-%m-%d")

    # 第一次：正常抓取，落库
    first = NewsAnalyzer(config=make_config(REPORT_MODE="current"))
    patch_crawl(first, fake_results())
    assert first.run() == EXIT_OK
    records_before = crawl_record_count(sandbox, date)
    snapshots_before = sorted(p.name for p in (sandbox / "output" / "html" / date).glob("*.html"))
    index_before = (sandbox / "output" / "index.html").read_text(encoding="utf-8")
    manager_mod._storage_manager = None

    # 第二次：RENDER_ONLY，任何抓取 / AI / 推送都必须不发生
    later = TZ.localize(datetime(2026, 10, 18, 21, 30))
    freeze_time(monkeypatch, later)
    cfg = make_config(REPORT_MODE="daily", RENDER_ONLY=True)
    cfg["AI_ANALYSIS"]["ENABLED"] = True
    cfg["AI_TRANSLATION"]["ENABLED"] = True
    cfg["ENABLE_NOTIFICATION"] = True
    cfg["BARK_URL"] = "https://bark.example.com/key"
    cfg["RSS"]["ENABLED"] = True
    analyzer = NewsAnalyzer(config=cfg)

    def forbidden(*a, **k):
        raise AssertionError("RENDER_ONLY 不应调用该方法")

    analyzer.data_fetcher.crawl_websites = forbidden
    analyzer.data_fetcher.fetch_data = forbidden
    analyzer._rss.fetch = forbidden
    analyzer.ctx.create_notification_dispatcher = forbidden
    analyzer._ai.run_analysis = forbidden

    assert analyzer.run() == EXIT_OK
    assert analyzer.ctx.config["ENABLE_NOTIFICATION"] is False
    assert analyzer.ctx.config["AI_ANALYSIS"]["ENABLED"] is False
    daily = sandbox / "output" / "html" / "latest" / "daily.html"
    assert daily.exists()
    html = daily.read_text(encoding="utf-8")
    assert "p1 新闻A1" in html
    # 没有伪造新的抓取快照 / 抓取记录，也没有覆盖入口页
    assert crawl_record_count(sandbox, date) == records_before
    assert sorted(p.name for p in (sandbox / "output" / "html" / date).glob("*.html")) == snapshots_before
    assert (sandbox / "output" / "index.html").read_text(encoding="utf-8") == index_before


def test_render_only_env_contract(sandbox, monkeypatch):
    """CI 契约：RENDER_ONLY=true SCHEDULE_ENABLED=false REPORT_MODE=daily python -m trendradar"""
    monkeypatch.setenv("RENDER_ONLY", "true")
    monkeypatch.setenv("SCHEDULE_ENABLED", "false")
    monkeypatch.setenv("REPORT_MODE", "daily")
    cfg = load_config()
    assert cfg["RENDER_ONLY"] is True
    assert cfg["REPORT_MODE"] == "daily"
    assert cfg["SCHEDULE"]["enabled"] is False


# ---------------------------------------------------------------------------
# RSS 使用调度覆盖后的模式 + 调度在抓取之前解析
# ---------------------------------------------------------------------------

def _evening_timeline():
    return {
        "custom": {
            "default": {"collect": True, "analyze": False, "push": False, "report_mode": "current",
                        "once": {"analyze": True, "push": True}},
            "periods": {
                "morning": {"name": "早班", "start": "06:00", "end": "10:00", "push": True,
                            "report_mode": "current", "once": {"push": True}},
                "evening": {"name": "晚间汇总", "start": "19:00", "end": "23:59", "push": True,
                            "report_mode": "daily", "ai_mode": "daily",
                            "frequency_file": None, "once": {"analyze": True, "push": True}},
            },
            "day_plans": {"all": {"periods": ["morning", "evening"]}},
            "week_map": {i: "all" for i in range(1, 8)},
        }
    }


def test_rss_uses_report_mode_after_schedule_override(sandbox, monkeypatch):
    now = TZ.localize(datetime(2026, 10, 18, 20, 0))
    freeze_time(monkeypatch, now)
    cfg = make_config(REPORT_MODE="current")
    cfg["SCHEDULE"] = {"enabled": True, "preset": "custom"}
    cfg["_TIMELINE_DATA"] = _evening_timeline()
    cfg["RSS"]["ENABLED"] = True
    analyzer = NewsAnalyzer(config=cfg)
    order = []

    def _crawl(ids, interval, domain_rules=None):
        order.append(("crawl", analyzer.report_mode))
        return fake_results(), {p["id"]: p["name"] for p in PLATFORMS}, []

    analyzer.data_fetcher.crawl_websites = _crawl
    sentinel = object()
    analyzer._rss.fetch = lambda: sentinel
    seen = {}

    def _process(rss_data, report_mode, rank_threshold, frequency_file):
        seen["mode"] = report_mode
        return None, None, None, set()

    analyzer._rss.process_by_mode = _process
    assert analyzer.run() == EXIT_OK
    assert seen["mode"] == "daily"
    # 调度在抓取之前就已解析（抓取时 report_mode 已是覆盖后的 daily）
    assert order == [("crawl", "daily")]
    # timeline 显式 ai_mode 生效到 AI 层配置
    assert analyzer.ctx.config["AI_ANALYSIS"]["MODE"] == "daily"


def test_collect_false_skips_crawling(sandbox, monkeypatch):
    now = TZ.localize(datetime(2026, 10, 18, 12, 0))
    freeze_time(monkeypatch, now)
    timeline = _evening_timeline()
    timeline["custom"]["default"]["collect"] = False
    cfg = make_config(REPORT_MODE="current")
    cfg["SCHEDULE"] = {"enabled": True, "preset": "custom"}
    cfg["_TIMELINE_DATA"] = timeline
    analyzer = NewsAnalyzer(config=cfg)

    def forbidden(*a, **k):
        raise AssertionError("collect=false 时不应抓取")

    analyzer.data_fetcher.crawl_websites = forbidden
    assert analyzer.run() == EXIT_OK


def test_platforms_disabled_still_runs_rss(sandbox):
    cfg = make_config(REPORT_MODE="current", ENABLE_CRAWLER=False)
    cfg["RSS"]["ENABLED"] = True
    analyzer = NewsAnalyzer(config=cfg)
    called = {}

    def _fetch():
        called["rss"] = True
        return None

    analyzer._rss.fetch = _fetch

    def forbidden(*a, **k):
        raise AssertionError("platforms.enabled=false 时不应抓取热榜")

    analyzer.data_fetcher.crawl_websites = forbidden
    assert analyzer.run() == EXIT_OK
    assert called.get("rss")


# ---------------------------------------------------------------------------
# once 窗口跨运行（持久化 output/）
# ---------------------------------------------------------------------------

class _CountingDispatcher:
    def __init__(self, counter):
        self.counter = counter

    def dispatch_all(self, **kwargs):
        self.counter.append(kwargs.get("mode"))
        return {"bark": True}

    def translate_content(self, **kwargs):  # pragma: no cover - 翻译关闭
        return kwargs.get("report_data"), None, None, None


def _run_in_window(sandbox, monkeypatch, dt, pushes):
    freeze_time(monkeypatch, dt)
    manager_mod._storage_manager = None
    cfg = make_config(REPORT_MODE="current")
    cfg["SCHEDULE"] = {"enabled": True, "preset": "custom"}
    cfg["_TIMELINE_DATA"] = _evening_timeline()
    cfg["ENABLE_NOTIFICATION"] = True
    cfg["BARK_URL"] = "https://bark.example.com/key"
    analyzer = NewsAnalyzer(config=cfg)
    patch_crawl(analyzer, fake_results(tag=dt.strftime("%H%M")))
    analyzer.ctx.create_notification_dispatcher = lambda: _CountingDispatcher(pushes)
    assert analyzer.run() == EXIT_OK
    return analyzer


def test_once_push_holds_across_separate_runs_with_persisted_output(sandbox, monkeypatch):
    pushes = []
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 18, 19, 5)), pushes)
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 18, 21, 35)), pushes)
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 18, 23, 58)), pushes)
    assert pushes == ["daily"], "晚间窗口内跨多次独立运行只应推送一次"

    # 早窗是另一个窗口：照常推送一次
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 19, 6, 0)), pushes)
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 19, 9, 59)), pushes)
    assert pushes == ["daily", "current"]

    # 窗口外不推送
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 19, 10, 0)), pushes)
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 19, 18, 59)), pushes)
    assert pushes == ["daily", "current"]

    # 次日晚窗：新的一天重新推送
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 19, 19, 0)), pushes)
    assert pushes == ["daily", "current", "daily"]


def test_daily_report_accumulates_across_runs(sandbox, monkeypatch):
    """持久化 output/ 后，晚间 daily 应包含当天早些时候运行抓到的标题。"""
    pushes = []
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 18, 7, 0)), pushes)
    _run_in_window(sandbox, monkeypatch, TZ.localize(datetime(2026, 10, 18, 20, 0)), pushes)
    html = (sandbox / "output" / "html" / "latest" / "daily.html").read_text(encoding="utf-8")
    assert "p1 新闻0700" in html and "p1 新闻2000" in html
