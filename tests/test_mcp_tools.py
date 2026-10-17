"""MCP 工具注册、命令行入口、副作用工具开关、stdio 输出纯净度。"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server import __version__
from mcp_server import server as srv

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = {
    "resolve_date_range",
    "get_latest_news", "get_news_by_date", "get_trending_topics",
    "get_latest_rss", "search_rss", "get_rss_feeds_status",
    "search_news", "find_related_news",
    "analyze_topic_trend", "analyze_data_insights", "analyze_sentiment",
    "aggregate_news", "compare_periods", "generate_summary_report",
    "get_current_config", "get_system_status", "check_version", "trigger_crawl",
    "sync_from_remote", "get_storage_status", "list_available_dates",
    "read_article", "read_articles_batch",
    "get_channel_format_guide", "get_notification_channels", "send_notification",
}


def test_all_tools_registered():
    tools = asyncio.run(srv.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS
    # 每个工具都要有描述，否则 LLM 不知道怎么用
    assert all((t.description or "").strip() for t in tools)


def test_banner_groups_cover_all_tools():
    grouped = {n for _, names in srv._TOOL_GROUPS for n in names}
    assert grouped == EXPECTED_TOOLS


def test_resources_registered():
    resources = asyncio.run(srv.mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert {"config://platforms", "config://rss-feeds", "data://available-dates", "config://keywords"} <= uris


def test_version_files_consistent():
    assert (ROOT / "version_mcp").read_text(encoding="utf-8").strip() == __version__
    assert srv.mcp.version == __version__


def test_console_script_points_to_main():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'trendradar-mcp = "mcp_server.server:main"' in text


def test_side_effect_tools_can_be_disabled():
    """在子进程里验证，避免改动本进程的全局 mcp 实例"""
    code = (
        "import asyncio, json, os\n"
        "os.environ['MCP_ENABLE_SIDE_EFFECT_TOOLS']='false'\n"
        "from mcp_server import server as s\n"
        "removed = s.apply_tool_policy()\n"
        "names = sorted(t.name for t in asyncio.run(s.mcp.list_tools()))\n"
        "print(json.dumps({'removed': removed, 'names': names}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60, check=True
    ).stdout.strip().splitlines()[-1]
    data = json.loads(out)
    assert set(data["removed"]) == {"send_notification", "trigger_crawl", "sync_from_remote"}
    assert not set(data["names"]) & {"send_notification", "trigger_crawl", "sync_from_remote"}
    assert "get_latest_news" in data["names"]


@pytest.mark.slow
def test_stdio_stdout_only_jsonrpc():
    """PYTHONUNBUFFERED=1 时横幅 / 日志也不能出现在 stdout"""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_notification_channels", "arguments": {}}},
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server"], cwd=ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    responses = {}
    try:
        for m in msgs:
            proc.stdin.write((json.dumps(m) + "\n").encode())
            proc.stdin.flush()
        while len(responses) < 3:
            line = proc.stdout.readline()
            assert line, "server closed stdout early: " + proc.stderr.read().decode(errors="replace")[-2000:]
            data = json.loads(line)  # 每一行都必须是合法 JSON-RPC
            assert data.get("jsonrpc") == "2.0"
            if "id" in data:
                responses[data["id"]] = data
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    rest = proc.stdout.read().decode(errors="replace").strip()
    for line in rest.splitlines():
        assert json.loads(line).get("jsonrpc") == "2.0"
    assert responses[1]["result"]["serverInfo"]["version"] == __version__
    assert {t["name"] for t in responses[2]["result"]["tools"]} == EXPECTED_TOOLS
    stderr = proc.stderr.read().decode(errors="replace")
    assert "TrendRadar MCP Server" in stderr
