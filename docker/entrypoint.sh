#!/bin/bash
set -e

# 检查配置文件
if [ ! -f "/app/config/config.yaml" ] || [ ! -f "/app/config/frequency_words.txt" ]; then
    echo "❌ 配置文件缺失"
    exit 1
fi

# 防重叠：上一轮还没跑完（例如 AI 翻译很慢）时，新一轮直接跳过，避免两轮同时写 SQLite
LOCK_FILE="/tmp/trendradar.lock"
RUN_CMD="cd /app && flock -n $LOCK_FILE python -m trendradar || echo '⏭️ 上一轮尚未结束或执行失败，本轮跳过'"

case "${RUN_MODE:-cron}" in
"once")
    # 单次模式：同一个容器被 restart 策略重新拉起时不再重复执行，避免无限抓取/推送。
    # 标记文件在容器可写层里，docker compose up 重新创建容器后会自动清除。
    ONCE_MARKER="/tmp/.trendradar-once-done"
    if [ -f "$ONCE_MARKER" ]; then
        echo "ℹ️ 单次模式已执行过（容器被重启策略重新拉起），不再重复执行。"
        echo "   RUN_MODE=once 请在 docker/.env 中设置 TRENDRADAR_RESTART=no（或 on-failure）。"
        exec sleep infinity
    fi
    echo "🔄 单次执行"
    set +e
    flock -n "$LOCK_FILE" python -m trendradar
    rc=$?
    set -e
    touch "$ONCE_MARKER"
    exit $rc
    ;;
"cron")
    CRON_EXPR="${CRON_SCHEDULE:-*/30 * * * *}"

    # 校验 CRON_SCHEDULE：不允许换行（否则可注入额外的 crontab 行），只允许合法字符，且必须正好 5 个字段
    case "$CRON_EXPR" in
        *$'\n'*|*$'\r'*)
            echo "❌ CRON_SCHEDULE 不能包含换行"
            exit 1
            ;;
    esac
    if ! printf '%s' "$CRON_EXPR" | grep -qE '^[0-9*/,[:space:]-]+$'; then
        echo "❌ CRON_SCHEDULE 格式非法: $CRON_EXPR"
        exit 1
    fi
    # shellcheck disable=SC2086
    set -- $CRON_EXPR
    if [ "$#" -ne 5 ]; then
        echo "❌ CRON_SCHEDULE 必须正好是 5 个字段（分 时 日 月 周）: $CRON_EXPR"
        exit 1
    fi

    # 生成 crontab
    printf '%s %s\n' "$CRON_EXPR" "$RUN_CMD" > /tmp/crontab

    echo "📅 生成的crontab内容:"
    cat /tmp/crontab

    if ! /usr/local/bin/supercronic -test /tmp/crontab; then
        echo "❌ crontab格式验证失败"
        exit 1
    fi

    # 立即执行一次（如果配置了）；首轮失败不阻止 cron 启动
    if [ "${IMMEDIATE_RUN:-false}" = "true" ]; then
        echo "▶️ 立即执行一次"
        flock -n "$LOCK_FILE" python -m trendradar || echo "⚠️ 首轮执行失败，继续启动 cron"
    fi

    # 启动 Web 服务器（只提供 HTML 报告，不暴露数据库文件，不列目录）
    echo "🌐 启动 Web 服务器..."
    python manage.py start_webserver

    echo "⏰ 启动supercronic: $CRON_EXPR"
    echo "🎯 supercronic 将作为主进程运行"

    exec /usr/local/bin/supercronic -passthrough-logs /tmp/crontab
    ;;
*)
    exec "$@"
    ;;
esac
