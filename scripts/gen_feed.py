#!/usr/bin/env python3
"""每日摘要订阅生成器
从最近日期目录抽每日汇总内容，生成两份订阅产物：
- docs/reports/feed.xml   (RSS 2.0，供 RSS 阅读器订阅)
- docs/reports/feed.json  (JSON Feed 1.1，机器可读，供第三方系统消费)
"""
import json
import re
from datetime import datetime
from html import escape as esc
from pathlib import Path

BASE = Path("docs/reports")
SITE = "https://leilaomi.github.io/hot-news-radar"


def collect_items():
    """收集最近 7 个有快照的日期，返回条目列表（RSS/JSON 共用）。"""
    items = []
    days = sorted(
        [d for d in BASE.iterdir() if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name)],
        reverse=True,
    )[:7]
    for day in days:
        # 若该日有任何快照，链接到当天最后一个(接近汇总)
        snaps = sorted(day.glob("*.html"))
        if not snaps:
            continue
        last_snap = snaps[-1]
        items.append({
            "title": f"{day.name} 热点雷达 · {len(snaps)} 个时段快照",
            "link": f"{SITE}/reports/{day.name}/{last_snap.name}",
            "guid": day.name,
            "pubDate": datetime.strptime(day.name + " " + last_snap.stem, "%Y-%m-%d %H-%M"),
            "desc": f"{day.name} 共 {len(snaps)} 次抓取，覆盖全天热点变化。首次 {snaps[0].stem.replace('-',':')}，末次 {last_snap.stem.replace('-',':')}。",
        })
    return items


def write_rss(items):
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


def write_json(items):
    """JSON Feed 1.1（https://jsonfeed.org/version/1.1）。"""
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Hot News Radar 快照订阅",
        "home_page_url": f"{SITE}/",
        "feed_url": f"{SITE}/reports/feed.json",
        "description": "每日新闻雷达快照更新通知",
        "language": "zh-CN",
        "items": [
            {
                "id": it["guid"],
                "url": it["link"],
                "title": it["title"],
                "content_text": it["desc"],
                "date_published": it["pubDate"].strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            }
            for it in items
        ],
    }
    out = BASE / "feed.json"
    out.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 已生成: {out} ({len(items)} 条)")


def build():
    items = collect_items()
    if not items:
        print("无内容生成feed")
        return
    write_rss(items)
    write_json(items)


if __name__ == "__main__":
    build()
