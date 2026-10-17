@echo off
chcp 65001 >nul
REM 启动 TrendRadar MCP Server（HTTP 模式）
REM 默认只监听 127.0.0.1（仅本机可访问）。需要远程访问时：
REM   set MCP_AUTH_TOKEN=<至少 16 位的随机字符串>
REM   set MCP_HOST=0.0.0.0
REM   start-http.bat
REM 客户端请求头需带：Authorization: Bearer <MCP_AUTH_TOKEN>

REM 切到脚本所在目录
cd /d "%~dp0"

if "%MCP_HOST%"=="" set "MCP_HOST=127.0.0.1"
if "%MCP_PORT%"=="" set "MCP_PORT=3333"

echo ============================================================
echo   TrendRadar MCP Server (HTTP 模式)
echo ============================================================
echo.

REM 检查虚拟环境
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 虚拟环境未找到
    echo 请先运行 setup-windows.bat 或 setup-windows-en.bat 进行部署
    echo.
    pause
    exit /b 1
)

if /i "%MCP_HOST%"=="127.0.0.1" goto local_mode
if /i "%MCP_HOST%"=="localhost" goto local_mode
if "%MCP_AUTH_TOKEN%"=="" goto need_token
echo [模式] HTTP（远程访问，已启用 Bearer Token 鉴权）
goto start

:need_token
echo [错误] 监听 %MCP_HOST%（非本机地址）必须先设置 MCP_AUTH_TOKEN
echo        例如：set MCP_AUTH_TOKEN=请替换为至少16位的随机字符串
echo.
pause
exit /b 1

:local_mode
echo [模式] HTTP（仅本机访问）

:start
echo [地址] http://%MCP_HOST%:%MCP_PORT%/mcp
echo [提示] 按 Ctrl+C 停止服务
echo.

uv run python -m mcp_server.server --transport http --host %MCP_HOST% --port %MCP_PORT%

pause
