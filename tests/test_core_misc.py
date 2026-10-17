"""core / crawler / storage / report 杂项回归测试。"""
import json
import time
from pathlib import Path

import pytest

from trendradar.core.frequency import load_frequency_words, matches_word_groups
from trendradar.crawler.fetcher import DataFetcher
from trendradar.report.generator import generate_html_report
from trendradar.utils.env import parse_bool


# ---------------------------------------------------------------------------
# 频率词
# ---------------------------------------------------------------------------

def _load(tmp_path, text):
    p = tmp_path / "fw.txt"
    p.write_text(text, encoding="utf-8")
    return load_frequency_words(str(p))


def test_whitespace_only_blank_line_still_separates_groups(tmp_path):
    groups, _, _ = _load(tmp_path, "AI\n  \n+芯片\n华为\n")
    assert [g["group_key"] for g in groups] == ["AI", "华为"]
    # 「芯片」作为第二组的必须词，不能污染第一组
    assert matches_word_groups("AI 大模型", groups, [])


def test_escaped_hash_keyword(tmp_path):
    groups, _, _ = _load(tmp_path, "# 注释\n\\#话题\n")
    assert groups[0]["group_key"] == "#话题"
    assert matches_word_groups("今天的 #话题 很热", groups, [])


def test_global_filter_supports_regex(tmp_path):
    groups, fw, gf = _load(tmp_path, "[GLOBAL_FILTER]\n/广告|推广/\n八卦\n\n[WORD_GROUPS]\n手机\n")
    assert not matches_word_groups("手机推广", groups, fw, gf)
    assert not matches_word_groups("手机八卦", groups, fw, gf)
    assert matches_word_groups("手机发布", groups, fw, gf)


# ---------------------------------------------------------------------------
# 热榜抓取
# ---------------------------------------------------------------------------

def test_api_url_with_existing_query_string():
    f = DataFetcher(api_url="https://api.example.com/s?token=abc")
    assert f._build_api_url("weibo") == "https://api.example.com/s?token=abc&id=weibo&latest"
    f2 = DataFetcher(api_url="https://api.example.com/s")
    assert f2._build_api_url("a b") == "https://api.example.com/s?id=a+b&latest"


def test_stale_cache_is_rejected():
    f = DataFetcher(api_url="https://api.example.com/s", max_cache_age=7200)
    now = time.time()
    fresh = {"status": "cache", "updatedTime": int((now - 600) * 1000)}
    stale = {"status": "cache", "updatedTime": int((now - 5 * 3600) * 1000)}
    assert f._check_freshness(fresh, now) is None
    assert "过期" in f._check_freshness(stale, now)
    assert f._check_freshness({"status": "success", "updatedTime": 0}, now) is None
    assert DataFetcher(api_url="https://x", max_cache_age=0)._check_freshness(stale, now) is None


def test_stale_cache_counts_as_failure(monkeypatch):
    f = DataFetcher(api_url="https://api.example.com/s", max_cache_age=3600)

    class Resp:
        text = json.dumps({"status": "cache", "updatedTime": int((time.time() - 86400) * 1000),
                           "items": [{"title": "旧", "url": "https://a.com"}]})

        def raise_for_status(self):
            pass

    monkeypatch.setattr("trendradar.crawler.fetcher.requests.get", lambda *a, **k: Resp())
    results, _, failed = f.crawl_websites(["p"], request_interval=0)
    assert failed == ["p"] and results == {}
    assert "过期" in f.failure_reasons["p"]


def test_empty_items_counts_as_failure(monkeypatch):
    f = DataFetcher(api_url="https://api.example.com/s")
    monkeypatch.setattr(f, "fetch_data", lambda id_info, **k: (json.dumps({"status": "success", "items": []}), "p", "p"))
    results, _, failed = f.crawl_websites(["p"], request_interval=0)
    assert failed == ["p"] and "p" not in results


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

def test_generate_html_without_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = generate_html_report(
        [], 0, mode="daily", output_dir=str(tmp_path / "output"),
        date_folder="2026-10-18", time_filename="21-00",
        render_html_func=lambda *a: "<html>ok</html>",
        write_snapshot=False, write_index=False,
    )
    assert Path(path) == tmp_path / "output" / "html" / "latest" / "daily.html"
    assert not (tmp_path / "output" / "html" / "2026-10-18").exists()
    assert not (tmp_path / "output" / "index.html").exists()
    assert not (tmp_path / "index.html").exists()
    # 原子写不留临时文件
    assert not list((tmp_path / "output" / "html" / "latest").glob("*.tmp"))


def test_generate_html_fallback_is_escaped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stats = [{"word": "<script>alert(1)</script>", "count": 0, "titles": []}]
    path = generate_html_report(stats, 0, mode="current", output_dir=str(tmp_path / "o"),
                                date_folder="d", time_filename="t", render_html_func=None)
    assert "<script>alert(1)" not in Path(path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 配置 / 环境变量
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("true", True), ("YES", True), ("1", True), ("on", True),
                                          ("false", False), ("0", False), ("no", False), ("", None)])
def test_parse_bool_is_consistent(raw, expected):
    assert parse_bool(raw) is expected


def test_loader_env_overrides(monkeypatch):
    from trendradar.core.loader import load_config

    root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("CONFIG_PATH", str(root / "config" / "config.yaml"))
    monkeypatch.setenv("NOTIFICATION_ENABLED", "false")
    monkeypatch.setenv("STORAGE_RETENTION_DAYS", "9")
    monkeypatch.setenv("PLATFORM_FAILURE_THRESHOLD", "0.5")
    monkeypatch.delenv("LOCAL_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("REMOTE_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("REPORT_MODE", raising=False)
    cfg = load_config()
    assert cfg["ENABLE_NOTIFICATION"] is False
    assert cfg["STORAGE"]["LOCAL"]["RETENTION_DAYS"] == 9
    assert cfg["STORAGE"]["REMOTE"]["RETENTION_DAYS"] == 9
    assert cfg["PLATFORM_FAILURE_THRESHOLD"] == 0.5
    assert "CACHE_ENABLED" in cfg["AI_TRANSLATION"] and "CACHE_DB_PATH" in cfg["AI"]

    monkeypatch.setenv("REPORT_MODE", "curent")
    with pytest.raises(ValueError, match="REPORT_MODE"):
        load_config()


# ---------------------------------------------------------------------------
# 远程存储：条件写，防 last-writer-wins
# ---------------------------------------------------------------------------

class _FakeBody:
    def __init__(self, data):
        self.data = data

    def iter_chunks(self, chunk_size=1024):
        yield self.data


class _FakeS3:
    def __init__(self):
        self.objects = {}
        self.etag_seq = 0
        self.puts = []

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return {}

    def get_object(self, Bucket, Key):
        data, etag = self.objects[Key]
        return {"Body": _FakeBody(data), "ETag": etag}

    def put_object(self, **kw):
        from botocore.exceptions import ClientError

        self.puts.append({k: v for k, v in kw.items() if k != "Body"})
        key = kw["Key"]
        current = self.objects.get(key)
        if "IfMatch" in kw and (current is None or current[1] != kw["IfMatch"]):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        if "IfNoneMatch" in kw and current is not None:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        self.etag_seq += 1
        etag = f'"e{self.etag_seq}"'
        self.objects[key] = (kw["Body"], etag)
        return {"ETag": etag}


def _remote(tmp_path, s3):
    from trendradar.storage.remote import RemoteStorageBackend

    b = RemoteStorageBackend.__new__(RemoteStorageBackend)
    # 只初始化本测试需要的属性，避免真实 boto3 客户端
    b.bucket_name = "bkt"
    b.endpoint_url = "https://s3.example.com"
    b.region = ""
    b.enable_txt = False
    b.enable_html = False
    b.timezone = "Asia/Shanghai"
    b.temp_dir = tmp_path
    b.s3_client = s3
    b._downloaded_files = []
    b._db_connections = {}
    b._batch_mode = False
    b._batch_dirty = set()
    b._remote_etags = {}
    b._conditional_writes = True
    return b


def test_remote_upload_uses_conditional_write_and_detects_conflict(tmp_path):
    s3 = _FakeS3()
    a = _remote(tmp_path / "a", s3)
    a.record_period_execution("2026-10-18", "evening", "push")  # 远端不存在 → IfNoneMatch
    assert s3.puts[-1].get("IfNoneMatch") == "*"
    key = a._get_remote_db_key("2026-10-18")
    assert key in s3.objects

    # 第二个「运行」下载同一对象（拿到 ETag），第一个运行再次写入改变远端
    b = _remote(tmp_path / "b", s3)
    assert not b.has_period_executed("2026-10-18", "evening", "analyze")
    a.record_period_execution("2026-10-18", "evening", "analyze")
    assert s3.puts[-1].get("IfMatch")

    # b 基于过期副本写入 → 412，拒绝覆盖
    assert b.record_period_execution("2026-10-18", "morning", "push") is False
    for backend in (a, b):
        for conn in backend._db_connections.values():
            conn.close()
