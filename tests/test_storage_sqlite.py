"""trendradar/storage 的 SQLite 层单元测试。

用 tmp_path 作为 data_dir，完全隔离真实 output/ 数据。
重点覆盖：存取往返、新增标题检测、抓取轮次记录，以及 SQL 注入回归
（标题是外部抓来的不可信输入，历史上这里全部用参数化查询，测试要守住这个事实）。
"""
import hashlib
import sqlite3
from pathlib import Path

import pytest

from trendradar.storage.base import NewsData, NewsItem
from trendradar.storage.local import LocalStorageBackend

DATE = "2026-01-01"


def _url_for(title: str) -> str:
    """由标题派生稳定且唯一的 URL。

    必须用标题派生而不是顺序索引：_save_news_data_impl 以 (url, platform_id)
    作为去重依据，若不同标题共用同一 URL，后一条会被误判成前一条的「标题变更」。
    用 md5 而非 hash()，避免 PYTHONHASHSEED 随机化导致测试不稳定。
    """
    return "https://example.com/" + hashlib.md5(title.encode("utf-8")).hexdigest()[:12]


@pytest.fixture
def backend(tmp_path):
    b = LocalStorageBackend(
        data_dir=str(tmp_path), enable_txt=False, enable_html=False
    )
    yield b
    # 释放 backend 持有的 sqlite 连接，否则 Windows 上 tmp_path 清理会失败
    for conn in getattr(b, "_db_connections", {}).values():
        try:
            conn.close()
        except Exception:
            pass


def make_data(crawl_time: str, titles_by_source) -> NewsData:
    items = {}
    id_to_name = {}
    for source_id, titles in titles_by_source.items():
        id_to_name[source_id] = source_id.upper()
        items[source_id] = [
            NewsItem(
                title=t,
                source_id=source_id,
                rank=i + 1,
                url=_url_for(t),
                crawl_time=crawl_time,
            )
            for i, t in enumerate(titles)
        ]
    return NewsData(
        date=DATE,
        crawl_time=crawl_time,
        items=items,
        id_to_name=id_to_name,
    )


# --------------------------------------------------------------------------
# 存取往返
# --------------------------------------------------------------------------
def test_save_then_read_roundtrip(backend):
    backend.save_news_data(make_data("10:00", {"toutiao": ["新闻A", "新闻B"]}))
    got = backend.get_today_all_data(DATE)

    assert got is not None
    assert got.date == DATE
    titles = {i.title for i in got.items.get("toutiao", [])}
    assert titles == {"新闻A", "新闻B"}


def test_read_missing_date_returns_none(backend):
    assert backend.get_today_all_data("1999-01-01") is None


def test_is_first_crawl_today(backend):
    """注意：首次抓取后仍返回 True 是**有意设计**，不是 bug。

    _is_first_crawl_today_impl 的判定是 `count <= 1`：
    当天只有 1 条抓取记录时依旧算「第一次」，这样首抓不会产生「新增 N 条」
    的噪音提示（没有可比基线）。此处固化该语义，防止后人误改。
    """
    assert backend.is_first_crawl_today(DATE) is True  # 0 条记录

    backend.save_news_data(make_data("10:00", {"toutiao": ["新闻A"]}))
    assert backend.is_first_crawl_today(DATE) is True  # 1 条记录，仍算首次

    backend.save_news_data(make_data("11:00", {"toutiao": ["新闻B"]}))
    assert backend.is_first_crawl_today(DATE) is False  # 2 条记录，有基线了


def test_crawl_times_accumulate(backend):
    backend.save_news_data(make_data("10:00", {"toutiao": ["新闻A"]}))
    backend.save_news_data(make_data("11:00", {"toutiao": ["新闻B"]}))

    times = backend.get_crawl_times(DATE)
    assert sorted(times) == ["10:00", "11:00"]


def test_get_latest_crawl_data_returns_last_batch(backend):
    backend.save_news_data(make_data("10:00", {"toutiao": ["新闻A"]}))
    backend.save_news_data(make_data("11:00", {"toutiao": ["新闻B"]}))

    latest = backend.get_latest_crawl_data(DATE)
    assert latest is not None
    assert latest.crawl_time == "11:00"
    assert [i.title for i in latest.items["toutiao"]] == ["新闻B"]


def test_repeated_title_merges_count(backend):
    """同一标题多次出现应累加 count，而不是插入重复行。"""
    backend.save_news_data(make_data("10:00", {"toutiao": ["新闻A"]}))
    backend.save_news_data(make_data("11:00", {"toutiao": ["新闻A"]}))

    got = backend.get_today_all_data(DATE)
    item = got.items["toutiao"][0]
    assert item.title == "新闻A"
    assert item.count >= 2


# --------------------------------------------------------------------------
# 新增标题检测
# --------------------------------------------------------------------------
def test_detect_new_titles_only_returns_unseen(backend):
    backend.save_news_data(make_data("10:00", {"toutiao": ["新闻A"]}))

    second = make_data("11:00", {"toutiao": ["新闻A", "新闻B"]})
    new = backend.detect_new_titles(second)

    new_titles = {t for items in new.values() for t in items}
    assert "新闻B" in new_titles
    assert "新闻A" not in new_titles


def test_detect_new_titles_first_crawl_returns_all(backend):
    first = make_data("10:00", {"toutiao": ["新闻A", "新闻B"]})
    new = backend.detect_new_titles(first)

    new_titles = {t for items in new.values() for t in items}
    assert new_titles == {"新闻A", "新闻B"}


# --------------------------------------------------------------------------
# SQL 注入回归 —— 标题来自外部抓取，是不可信输入
# --------------------------------------------------------------------------
INJECTION_PAYLOADS = [
    "'; DROP TABLE news; --",
    '"); DELETE FROM news; --',
    "新闻' OR '1'='1",
    "'; UPDATE news SET title='x'; --",
    "\\'; DROP TABLE news; --",
    "新闻\x00注入",
]


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_sql_injection_payloads_are_stored_literally(backend, payload):
    """恶意标题必须被原样存进去，绝不能被当作 SQL 执行。"""
    backend.save_news_data(make_data("10:00", {"toutiao": [payload]}))

    got = backend.get_today_all_data(DATE)
    assert got is not None

    titles = [i.title for i in got.items.get("toutiao", [])]
    assert payload in titles, f"标题未被原样保存：{payload!r}"


def test_tables_survive_injection_attempt(backend):
    """注入尝试之后，表结构必须完好、数据仍可读。"""
    backend.save_news_data(make_data("10:00", {"toutiao": ["'; DROP TABLE news; --"]}))
    backend.save_news_data(make_data("11:00", {"toutiao": ["正常新闻"]}))

    got = backend.get_today_all_data(DATE)
    titles = {i.title for i in got.items["toutiao"]}
    assert titles == {"'; DROP TABLE news; --", "正常新闻"}


def test_source_id_injection_is_parameterized(backend):
    """source_id 同样来自外部，也要参数化。"""
    evil_id = "src'; DROP TABLE news; --"
    data = NewsData(
        date=DATE,
        crawl_time="10:00",
        items={evil_id: [NewsItem(title="新闻A", source_id=evil_id, rank=1)]},
        id_to_name={evil_id: "恶意来源"},
    )
    backend.save_news_data(data)

    got = backend.get_today_all_data(DATE)
    assert got is not None
    assert evil_id in got.items


def test_connection_still_usable_after_injection(backend, tmp_path):
    """注入后数据库连接应仍能正常查询（证明没有发生破坏）。"""
    backend.save_news_data(make_data("10:00", {"toutiao": ["'; DROP TABLE news; --"]}))

    db_path = Path(tmp_path) / "news" / f"{DATE}.db"
    assert db_path.exists()

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        assert "news_items" in tables, "news_items 表被删掉了，说明存在注入漏洞"
        assert "crawl_records" in tables
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 批次与清理
# --------------------------------------------------------------------------
def test_batch_context_manager(backend):
    """begin_batch/end_batch 应可安全调用（批量写入优化路径）。"""
    backend.begin_batch()
    backend.save_news_data(make_data("10:00", {"toutiao": ["新闻A"]}))
    backend.end_batch()

    got = backend.get_today_all_data(DATE)
    assert got is not None
    assert [i.title for i in got.items["toutiao"]] == ["新闻A"]


def test_backend_name_and_txt_support(backend):
    assert backend.backend_name == "local"
    assert backend.supports_txt is False
