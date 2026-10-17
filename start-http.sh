#!/bin/bash
# 启动 TrendRadar MCP Server（HTTP 模式）
#
# 默认只监听 127.0.0.1（仅本机可访问）。需要局域网/远程访问时：
#   export MCP_AUTH_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
#   MCP_HOST=0.0.0.0 ./start-http.sh
# 客户端请求头需带：Authorization: Bearer <MCP_AUTH_TOKEN>
# 通过域名/局域网 IP 访问时还需设置 MCP_ALLOWED_HOSTS（逗号分隔），否则会被 DNS rebinding 防护拒绝。

# 切到脚本所在目录，保证从任何位置执行都能找到 .venv
cd "$(dirname "$0")" || exit 1

MCP_HOST="${MCP_HOST:-127.0.0.1}"
MCP_PORT="${MCP_PORT:-3333}"

echo "╔════════════════════════════════════════╗"
echo "║  TrendRadar MCP Server (HTTP 模式)    ║"
echo "╚════════════════════════════════════════╝"
echo ""

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "❌ [错误] 虚拟环境未找到"
    echo "请先运行 ./setup-mac.sh 进行部署"
    echo ""
    exit 1
fi

case "$MCP_HOST" in
    127.*|localhost|::1)
        echo "[模式] HTTP（仅本机访问）"
        ;;
    *)
        if [ -z "$MCP_AUTH_TOKEN" ]; then
            echo "❌ [错误] 监听 $MCP_HOST（非本机地址）必须先设置 MCP_AUTH_TOKEN"
            echo "   export MCP_AUTH_TOKEN=\"\$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')\""
            exit 1
        fi
        echo "[模式] HTTP（远程访问，已启用 Bearer Token 鉴权）"
        ;;
esac
echo "[地址] http://${MCP_HOST}:${MCP_PORT}/mcp"
echo "[提示] 按 Ctrl+C 停止服务"
echo ""

exec uv run python -m mcp_server.server --transport http --host "$MCP_HOST" --port "$MCP_PORT"
