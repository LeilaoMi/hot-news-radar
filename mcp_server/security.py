# coding=utf-8
"""
MCP HTTP 传输的安全设置

- 默认只监听 127.0.0.1；
- 可选 Bearer Token 鉴权（环境变量 ``MCP_AUTH_TOKEN``）；监听非回环地址时必须配置，否则拒绝启动；
- Host / Origin 校验（防 DNS rebinding），使用 fastmcp 内置的 HostOriginGuardMiddleware
  （``host_origin_protection=True``），额外允许的主机 / 来源通过
  ``MCP_ALLOWED_HOSTS`` / ``MCP_ALLOWED_ORIGINS``（逗号分隔）配置。
"""

import hmac
import ipaddress
import os
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 3333
DEFAULT_HTTP_PATH = "/mcp"

ENV_AUTH_TOKEN = "MCP_AUTH_TOKEN"
ENV_ALLOWED_HOSTS = "MCP_ALLOWED_HOSTS"
ENV_ALLOWED_ORIGINS = "MCP_ALLOWED_ORIGINS"
ENV_ENABLE_SIDE_EFFECT_TOOLS = "MCP_ENABLE_SIDE_EFFECT_TOOLS"

# 有副作用的工具：发消息、触发抓取写盘、用 S3 凭据拉数据
SIDE_EFFECT_TOOLS = ("send_notification", "trigger_crawl", "sync_from_remote")

# 太短的 token 很容易被暴力猜到
MIN_TOKEN_LENGTH = 16


class InsecureBindError(RuntimeError):
    """监听非回环地址却没有配置鉴权 token"""


def is_loopback_host(host: Optional[str]) -> bool:
    """127.0.0.0/8、::1、localhost 视为回环；0.0.0.0 / :: / 具体网卡地址都不是。"""
    if not host:
        return False
    h = host.strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _split_env_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class HttpSecurityConfig:
    host: str
    token: Optional[str]
    allowed_hosts: List[str] = field(default_factory=list)
    allowed_origins: List[str] = field(default_factory=list)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.token)


def resolve_http_security(
    host: str,
    token: Optional[str] = None,
    allowed_hosts: Optional[Iterable[str]] = None,
    allowed_origins: Optional[Iterable[str]] = None,
) -> HttpSecurityConfig:
    """根据监听地址和环境变量生成 HTTP 安全配置。

    Raises:
        InsecureBindError: 监听非回环地址但没有配置 MCP_AUTH_TOKEN，或 token 过短
    """
    if token is None:
        token = os.environ.get(ENV_AUTH_TOKEN, "")
    token = (token or "").strip() or None

    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        raise InsecureBindError(
            f"{ENV_AUTH_TOKEN} 太短（至少 {MIN_TOKEN_LENGTH} 个字符）。"
            "可用 `python -c \"import secrets;print(secrets.token_urlsafe(32))\"` 生成。"
        )

    if not is_loopback_host(host) and token is None:
        raise InsecureBindError(
            f"拒绝启动：HTTP 模式监听非回环地址 {host!r}，但没有设置 {ENV_AUTH_TOKEN}。\n"
            f"  - 仅本机使用：去掉 --host 或使用 --host 127.0.0.1\n"
            f"  - 需要远程/容器访问：先设置环境变量 {ENV_AUTH_TOKEN}=<随机长字符串>，"
            f"客户端请求头带 Authorization: Bearer <token>"
        )

    hosts = list(allowed_hosts) if allowed_hosts is not None else _split_env_list(os.environ.get(ENV_ALLOWED_HOSTS))
    origins = (
        list(allowed_origins) if allowed_origins is not None else _split_env_list(os.environ.get(ENV_ALLOWED_ORIGINS))
    )
    # 显式监听某个具体地址时，允许以该地址访问
    if host and not is_loopback_host(host) and host not in ("0.0.0.0", "::") and host not in hosts:
        hosts.append(host)
    return HttpSecurityConfig(host=host, token=token, allowed_hosts=hosts, allowed_origins=origins)


class BearerTokenMiddleware:
    """校验 ``Authorization: Bearer <token>``（常量时间比较）。

    纯 ASGI 实现，不缓冲响应体，兼容 streamable-http 的 SSE 流。
    """

    def __init__(self, app: ASGIApp, token: str, realm: str = "trendradar-mcp") -> None:
        if not token:
            raise ValueError("BearerTokenMiddleware 需要非空 token")
        self.app = app
        self._token = token.encode("utf-8")
        self._realm = realm

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        provided = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                provided = value
                break

        scheme, _, credential = provided.partition(b" ")
        ok = scheme.lower() == b"bearer" and hmac.compare_digest(credential.strip(), self._token)
        if not ok:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            response = JSONResponse(
                {"error": "unauthorized", "message": "缺少或无效的 Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer realm="{self._realm}"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def build_http_middleware(config: HttpSecurityConfig) -> List[Middleware]:
    """需要附加到 fastmcp HTTP app 上的中间件列表（Host/Origin 校验由 fastmcp 负责）"""
    middleware: List[Middleware] = []
    if config.token:
        middleware.append(Middleware(BearerTokenMiddleware, token=config.token))
    return middleware


def http_transport_kwargs(config: HttpSecurityConfig) -> dict:
    """传给 ``FastMCP.run(transport='http', ...)`` / ``FastMCP.http_app(...)`` 的安全参数"""
    return {
        "middleware": build_http_middleware(config),
        # True = strict：所有请求都校验 Host（默认允许 127.0.0.1 / localhost / ::1 + allowed_hosts）
        # 和 Origin（同源或 allowed_origins），防 DNS rebinding
        "host_origin_protection": True,
        "allowed_hosts": config.allowed_hosts,
        "allowed_origins": config.allowed_origins,
    }
