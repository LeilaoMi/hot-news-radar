#!/usr/bin/env python3
"""检查「当日汇总」报告是否过期，供 CI 决定是否补生成。

退出码：
    0 = 报告新鲜，无需处理
    1 = 报告缺失 / 无法解析 / 已过期，需要补生成

为什么不用文件 mtime 判断：
    CI 每次都是全新 checkout，文件的 mtime 恒等于 checkout 时刻，
    用 stat 判断"是否超过 24 小时"永远不会成立，导致 daily.html 长期停留在
    某一次历史快照上。因此这里改为解析报告正文里的生成时间。
"""
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 报告正文里的生成时间，形如：生成时间</span>\n<span class="info-value">08-29 10:22
# 注意：标签与值之间有换行，正则必须用 \s* 而非普通空格
PATTERN = re.compile(
    r"生成时间</span>\s*<span class=[\"']info-value[\"']>\s*"
    r"(\d{2}-\d{2})[ T](\d{2}):(\d{2})"
)

DEFAULT_PATH = "docs/reports/latest/daily.html"
MAX_AGE_SECONDS = int(os.environ.get("DAILY_MAX_AGE", str(24 * 3600)))
SITE_TZ = os.environ.get("SITE_TZ", "Asia/Shanghai")


def check(path: str = DEFAULT_PATH) -> int:
    p = Path(path)
    if not p.exists():
        print(f"daily.html 不存在（{path}），需要生成")
        return 1

    try:
        html = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"读取失败：{e}，需要生成")
        return 1

    m = PATTERN.search(html)
    if not m:
        print("无法从报告正文解析生成时间，需要生成")
        return 1

    md, hh, mm = m.group(1), m.group(2), m.group(3)
    try:
        month, day = md.split("-")
        now = datetime.now()
        gen = now.replace(
            month=int(month), day=int(day),
            hour=int(hh), minute=int(mm), second=0, microsecond=0,
        )
    except ValueError as e:
        print(f"生成时间格式非法（{md} {hh}:{mm}）：{e}，需要生成")
        return 1

    age = (now - gen).total_seconds()

    # 跨年边界：报告生成于去年 12 月、当前已是今年 1 月，age 会是很大的正数；
    # 反之若报告日期"晚于"今天（时钟回拨/时区异常），age 为负。两者都按过期处理。
    if age < 0:
        print(f"生成时间早于当前时钟异常（{md} {hh}:{mm}，age={age:.0f}s），需要生成")
        return 1

    if age > MAX_AGE_SECONDS:
        print(f"已过期 {age / 3600:.1f}h（生成于 {md} {hh}:{mm}，阈值 {MAX_AGE_SECONDS / 3600:.0f}h），需要生成")
        return 1

    print(f"仍新鲜（生成于 {md} {hh}:{mm}，{age / 3600:.1f}h 前）")
    return 0


if __name__ == "__main__":
    sys.exit(check(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH))
