#!/usr/bin/env python3
"""每日摘要 RSS 生成器
从最近日期目录抽每日汇总内容, 生成 docs/reports/feed.xml (纯文本RSS)
供 RSS 阅读器订阅全站更新
"""
from pathlib import Path
from datetime import datetime, timedelta
import html as H
import re

BASE = Path("docs/reports")
SITE = "https://leilaomi.github.io/hot-news-radar"

def esc(s): return H.escape(s or "", quote=False)

def build():
    items = []
    # 取最近7个有内容的日期
    days = sorted([d for d in BASE.iterdir() if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name)], reverse=True)[:7]
    for day in days:
        daily_link = None
        # 若该日有任何快照，链接到当天最后一个(接近汇总)；有无 latest/daily 则更好
        snaps = sorted(day.glob("*.html"))
        if not snaps:
            continue
        last_snap = snaps[-1]
        first_dt = datetime.strptime(snaps[0].stem, "%H-%M")
        items.append({
            "title": f"{day.name} 热点雷达 · {len(snaps)} 个时段快照",
            "link": f"{SITE}/reports/{day.name}/{last_snap.name}",
            "guid": day.name,
            "pubDate": datetime.strptime(day.name + " " + last_snap.stem, "%Y-%m-%d %H-%M"),
            "desc": f"{day.name} 共 {len(snaps)} 次抓取，覆盖全天热点变化。首次 {snaps[0].stem.replace('-',':')}，末次 {last_snap.stem.replace('-',':')}。",
        })
    if not items:
        print("无内容生成feed"); return
    last_build = max(i["pubDate"] for i in items).strftime("%a, %d %b %Y %H:%M:%S +0800")
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0"><channel>',
           '<title>Hot News Radar 快照订阅</title>',
           f'<link>{SITE}/</link>',
           '<description>每日新闻雷达快照更新通知</description>',
           '<language>zh-CN</language>',
           f'<lastBuildDate>{esc(last_build)}</lastBuildDate>']
    for it in items:
        xml += [
            '<item>',
            f'<title>{esc(it["title"])}</title>',
            f'<link>{esc(it["link"])}</link>',
            f'<guid isPermaLink="false">{it["guid"]}</guid>',
            f'<pubDate>{it["pubDate"].strftime("%a, %d %b %Y %H:%M:%S +0800")}</pubDate>',
            f'<description>{esc(it["desc"])}</description>',
            '</item>']
    xml.append('</channel></rss>')
    out = BASE / "feed.xml"
    out.write_text("\n".join(xml), encoding="utf-8")
    print(f"RSS 已生成: {out} ({len(items)} 条)")

if __name__ == "__main__":
    build()
