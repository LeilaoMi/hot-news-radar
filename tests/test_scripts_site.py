"""scripts/ 下站点生成脚本的回归测试：gen_feed / gen_archive / retention / build_trends。"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"_t_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SNAP = """<!DOCTYPE html><html><head></head><body>
<span class="info-label">报告类型</span>
<span class="info-value">{kind}</span>
<p>content {name}</p></body></html>"""


@pytest.fixture
def site(tmp_path, monkeypatch):
    """在 tmp 里搭一个 docs/ 目录并 chdir 过去（脚本都用相对路径 docs/reports）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SITE_URL", raising=False)
    docs = tmp_path / "docs"
    (docs / "reports").mkdir(parents=True)
    (docs / "CNAME").write_text("news.example.com\n", encoding="utf-8")
    return docs


def add_snaps(docs, day, names, kind="当前榜单"):
    d = docs / "reports" / day
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text(SNAP.format(kind=kind, name=n), encoding="utf-8")
    return d


# ── gen_feed ───────────────────────────────────────────────────────────
def test_gen_feed_ignores_non_snapshot_files(site):
    add_snaps(site, "2026-10-17", ["08-00.html", "12-30.html", "rss_12-30.html", "daily.html"])
    mod = load("gen_feed")
    mod.build()  # 旧实现在 rss_12-30.html 上 strptime 抛 ValueError
    feed = json.loads((site / "reports" / "feed.json").read_text(encoding="utf-8"))
    item = feed["items"][0]
    assert item["id"] == "2026-10-17"
    assert item["url"] == "https://news.example.com/reports/2026-10-17/12-30.html"
    assert item["date_published"] == "2026-10-17T08:00:00+08:00"
    assert item["date_modified"] == "2026-10-17T12:30:00+08:00"
    assert "2 个时段快照" in item["title"]
    rss = (site / "reports" / "feed.xml").read_text(encoding="utf-8")
    assert 'rel="self"' in rss and "https://news.example.com/reports/feed.xml" in rss


def test_site_url_env_overrides_cname(site, monkeypatch):
    monkeypatch.setenv("SITE_URL", "https://x.example.org/")
    common = load("site_common")
    assert common.site_url() == "https://x.example.org"


# ── gen_archive：上一时刻/下一时刻 ─────────────────────────────────────
def test_prevnext_is_refreshed_when_new_snapshot_arrives(site):
    d = add_snaps(site, "2026-10-17", ["08-00.html", "12-00.html"])
    mod = load("gen_archive")
    mod.inject_prevnext()
    s = (d / "12-00.html").read_text(encoding="utf-8")
    assert 'href="08-00.html"' in s          # 「更早」现在会渲染
    assert '<span style="opacity:.3">更晚 &#9654;</span>' in s  # 当天最后一份：「更晚」暂不可点

    # 新快照到来：上一份最新快照的「更晚」必须补上链接（旧实现永远灰着）
    add_snaps(site, "2026-10-17", ["17-00.html"])
    mod.inject_prevnext()
    s = (d / "12-00.html").read_text(encoding="utf-8")
    assert 'href="17-00.html"' in s and 'href="08-00.html"' in s
    assert s.count('id="rdr-pn"') == 1       # 原位替换，不叠加

    # 再跑一次：幂等，不再改动文件
    assert mod.inject_prevnext() == 0


def test_inject_nav_fixes_wrong_depth_for_reports_index(site):
    mod = load("gen_archive")
    # 脚本按相对 docs/ 的层级计算前缀，CI 里传入的就是相对路径
    latest = Path("docs/reports/latest")
    latest.mkdir()
    cur = latest / "current.html"
    cur.write_text(SNAP.format(kind="当前榜单", name="c"), encoding="utf-8")
    mod.inject_nav(cur)
    assert 'href="../../reports/archive.html"' in cur.read_text(encoding="utf-8")
    idx = Path("docs/reports/index.html")
    idx.write_text(cur.read_text(encoding="utf-8"), encoding="utf-8")
    assert mod.inject_nav(idx) is True
    s = idx.read_text(encoding="utf-8")
    assert 'href="../reports/archive.html"' in s and "../../" not in s
    assert s.count('id="rdr-nav"') == 1


# ── retention ─────────────────────────────────────────────────────────
def test_retention_archive_strips_dead_prevnext_and_labels_type(site, monkeypatch):
    d = add_snaps(site, "2000-01-01", ["08-00.html", "22-04.html", "rss_23-00.html"], kind="全天汇总")
    gen = load("gen_archive")
    gen.inject_prevnext()
    assert 'id="rdr-pn"' in (d / "22-04.html").read_text(encoding="utf-8")
    mod = load("retention")
    mod.run()
    out = site / "reports" / "archive-daily" / "2000-01-01-daily.html"
    s = out.read_text(encoding="utf-8")
    assert 'id="rdr-pn"' not in s                    # 不再有指向 22-04.html 的死链
    assert "22-04.html" in s and "全天汇总" in s       # 来源说明 + 按报告类型选文案
    assert "当前榜单快照" not in s
    assert not d.exists()


def test_retention_keeps_recent_days(site):
    mod = load("retention")
    common = load("site_common")
    today = common.now_site().strftime("%Y-%m-%d")
    d = add_snaps(site, today, ["08-00.html"])
    mod.run()
    assert d.exists()


# ── build_trends ──────────────────────────────────────────────────────
def test_build_trends_uses_relative_home_link(site):
    common = load("site_common")
    today = common.now_site().strftime("%Y-%m-%d")
    add_snaps(site, today, ["08-00.html", "notes.html"])
    mod = load("build_trends")
    mod.main()
    s = (site / "reports" / "trends.html").read_text(encoding="utf-8")
    assert '<a href="../">' in s and 'href="/"' not in s
    assert "北京时间" in s
