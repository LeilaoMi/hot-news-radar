"""sync_from_remote 下载路径必须与读取路径一致；data_dir / project_root 在各工具里一致生效。"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from mcp_server.services.parser_service import ParserService
from mcp_server.tools.storage_sync import StorageSyncTools
from mcp_server.utils import paths
from trendradar.utils.time import get_configured_time

FAKE_ACCESS = "AKIAFAKEACCESSKEY123"
FAKE_SECRET = "fakeSecretAccessKey/0123456789abcdef"


def _write_config(root: Path, data_dir: str = "output"):
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "config.yaml").write_text(
        "app:\n  timezone: Asia/Shanghai\n"
        "storage:\n"
        f"  local:\n    data_dir: {data_dir}\n"
        "  remote:\n"
        "    endpoint_url: https://example.r2.cloudflarestorage.com\n"
        "    bucket_name: bucket\n"
        f"    access_key_id: {FAKE_ACCESS}\n"
        f"    secret_access_key: {FAKE_SECRET}\n",
        encoding="utf-8",
    )


def _make_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()


class _FakeS3:
    def __init__(self, objects, fail_keys=()):
        self.objects = objects  # key -> source file
        self.fail_keys = set(fail_keys)
        self.downloads = []

    def download_file(self, bucket, key, filename):
        self.downloads.append((bucket, key, filename))
        if key in self.fail_keys or key not in self.objects:
            raise RuntimeError(
                f"An error occurred (404) for https://example.r2.cloudflarestorage.com/{bucket}/{key}"
                f"?X-Amz-Credential={FAKE_ACCESS}&secret={FAKE_SECRET}"
            )
        Path(filename).write_bytes(Path(self.objects[key]).read_bytes())


class _FakeBackend:
    def __init__(self, dates, s3):
        self._dates = dates
        self.s3_client = s3
        self.bucket_name = "bucket"

    def list_remote_dates(self):
        return list(self._dates)


@pytest.fixture
def today():
    return get_configured_time("Asia/Shanghai").strftime("%Y-%m-%d")


@pytest.mark.parametrize("data_dir", ["output", "custom_data"])
def test_sync_writes_where_reader_reads(tmp_path, today, data_dir):
    _write_config(tmp_path, data_dir)
    src = tmp_path / "src.db"
    _make_db(src)
    s3 = _FakeS3({f"news/{today}.db": src, f"rss/{today}.db": src})

    tools = StorageSyncTools(str(tmp_path))
    tools._remote_backend = _FakeBackend([today], s3)

    result = tools.sync_from_remote(days=1)
    assert result["success"], result
    assert result["data"]["synced_dates"] == [today]

    expected = tmp_path / data_dir / "news" / f"{today}.db"
    assert expected.exists()
    assert (tmp_path / data_dir / "rss" / f"{today}.db").exists()
    # 旧 bug 的位置：output/{date}/news.db —— 不应再出现
    assert not (tmp_path / data_dir / today / "news.db").exists()
    assert not list((tmp_path / data_dir).rglob("*.part"))

    # 读取端（ParserService）能找到同一个文件
    parser = ParserService(str(tmp_path))
    assert parser._get_db_path(datetime.strptime(today, "%Y-%m-%d"), "news") == expected
    assert today in parser.get_available_dates("news")

    # 再次同步：本地已存在 → 跳过，不再重复下载
    s3.downloads.clear()
    again = tools.sync_from_remote(days=1)
    assert again["data"]["skipped_dates"] == [today]
    assert s3.downloads == []


def test_sync_rss_missing_is_not_failure(tmp_path, today):
    _write_config(tmp_path)
    src = tmp_path / "src.db"
    _make_db(src)
    tools = StorageSyncTools(str(tmp_path))
    tools._remote_backend = _FakeBackend([today], _FakeS3({f"news/{today}.db": src}))
    result = tools.sync_from_remote(days=1)
    assert result["data"]["synced_dates"] == [today]
    assert result["data"]["failed_dates"] == []


def test_sync_failure_is_masked(tmp_path, today):
    _write_config(tmp_path)
    tools = StorageSyncTools(str(tmp_path))
    tools._remote_backend = _FakeBackend([today], _FakeS3({}, fail_keys={f"news/{today}.db"}))
    result = tools.sync_from_remote(days=1)
    failed = result["data"]["failed_dates"]
    assert failed and failed[0]["date"] == today
    text = str(result)
    assert FAKE_ACCESS not in text
    assert FAKE_SECRET not in text


@pytest.mark.parametrize("days", [-1, 366, 10**9, "abc"])
def test_sync_days_validated(tmp_path, days):
    _write_config(tmp_path)
    tools = StorageSyncTools(str(tmp_path))
    result = tools.sync_from_remote(days=days)
    assert result["success"] is False
    assert result["error"]["code"] == "INVALID_PARAMETER"


def test_get_data_dir_resolution(tmp_path):
    _write_config(tmp_path, "custom")
    assert paths.get_data_dir(tmp_path) == tmp_path / "custom"
    abs_dir = tmp_path / "abs"
    assert paths.get_data_dir(tmp_path, data_dir=str(abs_dir)) == abs_dir
    # 没有配置文件时默认 output
    other = tmp_path / "other"
    other.mkdir()
    assert paths.get_data_dir(other) == other / "output"


def test_get_db_path_layout(tmp_path):
    assert paths.get_db_path(tmp_path, "news", "2026-10-17") == tmp_path / "news" / "2026-10-17.db"
    with pytest.raises(ValueError):
        paths.get_db_path(tmp_path, "../etc", "2026-10-17")


def test_set_project_root_affects_config_path(tmp_path):
    try:
        paths.set_project_root(tmp_path)
        assert paths.get_config_path() == tmp_path.resolve() / "config" / "config.yaml"
    finally:
        paths.set_project_root(None)
    assert paths.get_config_path() == paths.default_project_root() / "config" / "config.yaml"
