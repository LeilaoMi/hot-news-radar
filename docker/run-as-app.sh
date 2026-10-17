#!/bin/sh
# 以非 root 用户（app, uid/gid 默认 10001）运行容器主进程。
#
# - 若容器以 root 启动：只修正 /app/output（挂载卷）里不属于 app 的文件属主，然后用 setpriv 降权；
# - 若容器已经以非 root 启动（compose 里设置了 user:），直接执行，不做任何改动。
set -e

APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"

if [ "$(id -u)" = "0" ]; then
    if [ -d /app/output ]; then
        # 只处理属主不对的文件，避免每次启动都递归 chown 整个大目录
        find /app/output \( ! -user "$APP_UID" -o ! -group "$APP_GID" \) -exec chown "$APP_UID:$APP_GID" {} + 2>/dev/null || \
            echo "⚠️ 无法修正 /app/output 属主（只读挂载？），继续以 uid $APP_UID 运行" >&2
    fi
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups -- "$@"
fi

exec "$@"
