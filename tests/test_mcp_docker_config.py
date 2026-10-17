"""Docker / compose / 启动脚本的静态一致性检查（不需要 docker 环境）。"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKER = ROOT / "docker"
COMPOSE_FILES = ["docker-compose.yml", "docker-compose-build.yml"]


def _compose(name):
    return yaml.safe_load((DOCKER / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_mcp_port_mapping_consistent(name):
    svc = _compose(name)["services"]["trendradar-mcp"]
    (mapping,) = svc["ports"]
    container_port = mapping.rsplit(":", 1)[-1]
    assert container_port == "3333"
    assert mapping.startswith("${MCP_HOST:-127.0.0.1}:${MCP_PORT:-3333}:")
    # 容器内监听端口固定，不能再把 MCP_PORT 传进容器（否则改端口后宿主→容器映射错位）
    assert "MCP_PORT" not in svc["environment"]
    cmd = (DOCKER / "Dockerfile.mcp").read_text(encoding="utf-8")
    assert '"--port", "3333"' in cmd


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_mcp_gets_secrets_from_env(name):
    env = _compose(name)["services"]["trendradar-mcp"]["environment"]
    for key in ("MCP_AUTH_TOKEN", "FEISHU_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "S3_ACCESS_KEY_ID"):
        assert key in env


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_crawler_restart_policy_configurable(name):
    svc = _compose(name)["services"]["trendradar"]
    assert svc["restart"] == "${TRENDRADAR_RESTART:-unless-stopped}"


def test_default_images_are_local_builds():
    services = _compose("docker-compose.yml")["services"]
    for svc in services.values():
        assert "build" in svc
        assert "leilaomi/" not in svc["image"]
        assert svc["image"].endswith(":local}")


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.mcp"])
def test_dockerfile_hardening(dockerfile):
    text = (DOCKER / dockerfile).read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text
    assert "useradd" in text and "run-as-app" in text
    assert ":latest" not in text
    assert re.search(r"^FROM python:[\d.]+-slim-bookworm@sha256:[0-9a-f]{64}", text, re.M)
    assert re.search(r"astral-sh/uv:[\d.]+@sha256:[0-9a-f]{64}", text)
    assert "org.opencontainers.image.licenses" in text


def test_mcp_cmd_is_exec_form():
    text = (DOCKER / "Dockerfile.mcp").read_text(encoding="utf-8")
    assert re.search(r'^CMD \["python", "-m", "mcp_server.server"', text, re.M)


def test_entrypoint_rejects_cron_newline_injection(tmp_path):
    script = (DOCKER / "entrypoint.sh").read_text(encoding="utf-8")
    assert "*$'\\n'*" in script
    assert "flock -n" in script
    assert "ONCE_MARKER" in script


def test_shell_scripts_syntax():
    for path in (DOCKER / "entrypoint.sh", DOCKER / "run-as-app.sh", ROOT / "start-http.sh"):
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_start_scripts_default_loopback():
    sh = (ROOT / "start-http.sh").read_text(encoding="utf-8")
    bat = (ROOT / "start-http.bat").read_text(encoding="utf-8")
    assert 'MCP_HOST="${MCP_HOST:-127.0.0.1}"' in sh
    assert "--host 0.0.0.0" not in sh
    assert 'set "MCP_HOST=127.0.0.1"' in bat
    assert "--host 0.0.0.0" not in bat
    assert b"\r\n" in (ROOT / "start-http.bat").read_bytes()
