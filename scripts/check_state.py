#!/usr/bin/env python3
"""打印 / 校验跨运行持久化的状态（output/news、output/rss、output/ai_cache.db）。

CI 用 actions/cache 在运行之间保留这些 SQLite 库（审计第 1 节 P0）。这个脚本只读，
用来在日志里留下「恢复了什么」，并在状态明显没有延续时发 ::warning::，
防止以后再次静默退化成「每次都从空库开始」。

用法：
    python3 scripts/check_state.py restore      # 恢复缓存之后：只打印
    python3 scripts/check_state.py after-crawl  # 抓取之后：今日库 crawl_records ≤ 1 且已过 02:00 时告警
退出码恒为 0（只告警，不阻断发布）。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_common import now_site  # noqa: E402

OUTPUT = Path("output")


def _count(db: Path, table: str):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def summary():
    now = now_site()
    today = now.strftime("%Y-%m-%d")
    news = sorted((OUTPUT / "news").glob("*.db")) if (OUTPUT / "news").is_dir() else []
    rss = sorted((OUTPUT / "rss").glob("*.db")) if (OUTPUT / "rss").is_dir() else []
    ai = OUTPUT / "ai_cache.db"
    today_db = OUTPUT / "news" / f"{today}.db"
    info = {
        "today": today,
        "hour": now.hour,
        "news_dbs": [p.name for p in news],
        "rss_dbs": [p.name for p in rss],
        "today_crawls": _count(today_db, "crawl_records") if today_db.exists() else None,
        "today_period_executions": _count(today_db, "period_executions") if today_db.exists() else None,
        "ai_cache_rows": _count(ai, "ai_cache") if ai.exists() else None,
    }
    return info


def main(argv):
    phase = argv[1] if len(argv) > 1 else "restore"
    s = summary()
    print(f"[state] 站点日期 {s['today']}：news 库 {len(s['news_dbs'])} 个 {s['news_dbs'][-3:]}，"
          f"rss 库 {len(s['rss_dbs'])} 个，今日抓取记录 {s['today_crawls']}，"
          f"今日 once 记录 {s['today_period_executions']}，AI 缓存 {s['ai_cache_rows']} 条")
    if phase == "after-crawl":
        crawls = s["today_crawls"] or 0
        if crawls <= 1 and s["hour"] >= 2:
            print("::warning::今日数据库只有 ≤1 次抓取记录：跨运行状态可能没有恢复"
                  "（actions/cache 未命中或已被淘汰），新增检测 / 当日汇总 / once 防重本轮可能不准确")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
