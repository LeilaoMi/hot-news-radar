#!/usr/bin/env python3
"""每日摘要订阅生成器
从最近日期目录抽每日汇总内容，生成两份订阅产物：
- docs/reports/feed.xml   (RSS 2.0，供 RSS 阅读器订阅)
- docs/reports/feed.json  (JSON Feed 1.1，机器可读，供第三方系统消费)

每个日期一个条目：
- id/guid      = 日期（同一天的条目在更新时保持同一个 id）
- date_published = 当天第一份快照时间；date_modified = 最后一份快照时间
  （旧实现把 date_published 写成最后一份快照时间，每轮都在漂移）
"""
import json
import sys
from datetime import datetime
from html import escape as esc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_common import DATE_DIR_RE, site_url, snapshots  # noqa: E402

BASE = Path("docs/reports")
TZ_SUFFIX = "+08:00"  # 快照文件名是站点时区（Asia/Shanghai）墙钟


def _snap_time(day_name: str, snap: Path) -> datetime:
    return datetime.strptime(day_name + " " + snap.stem, "%Y-%m-%d %H-%M")


def collect_items(site: str):
    """收集最近 7 个有快照的日期，返回条目列表（RSS/JSON 共用）。

    只认 HH-MM.html，其它 html（rss_HH-MM.html、daily.html 等）一律忽略，
    不能因为一个异名文件让整个发布失败。
    """
    items = []
    if not BASE.is_dir():
        return items
    days = sorted(
        [d for d in BASE.iterdir() if d.is_dir() and DATE_DIR_RE.match(d.name)],
        reverse=True,
    )
    for day in days:
        snaps = snapshots(day)
        if not snaps:
            continue
        first_snap, last_snap = snaps[0], snaps[-1]
        items.append({
            "title": f"{day.name} 热点雷达 · {len(snaps)} 个时段快照",
            "link": f"{site}/reports/{day.name}/{last_snap.name}",
            "guid": day.name,
            "published": _snap_time(day.name, first_snap),
            "modified": _snap_time(day.name, last_snap),
            "desc": (
                f"{day.name} 共 {len(snaps)} 次抓取，覆盖全天热点变化。"
                f"首次 {first_snap.stem.replace('-', ':')}，末次 {last_snap.stem.replace('-', ':')}。"
            ),
        })
        if len(items) >= 7:
            break
    return items


def _rfc822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0800")


def write_rss(items, site: str):
    last_build = _rfc822(max(i["modified"] for i in items))
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
           '<title>Hot News Radar 快照订阅</title>',
           f'<link>{esc(site)}/</link>',
           f'<atom:link href="{esc(site)}/reports/feed.xml" rel="self" type="application/rss+xml"/>',
           '<description>每日新闻雷达快照更新通知</description>',
           '<language>zh-CN</language>',
           f'<lastBuildDate>{esc(last_build)}</lastBuildDate>']
    for it in items:
        xml += [
            '<item>',
            f'<title>{esc(it["title"])}</title>',
            f'<link>{esc(it["link"])}</link>',
            f'<guid isPermaLink="false">{esc(it["guid"])}</guid>',
            # RSS 2.0 没有 modified 字段；pubDate 用最后更新时间，便于阅读器排序
            f'<pubDate>{_rfc822(it["modified"])}</pubDate>',
            f'<description>{esc(it["desc"])}</description>',
            '</item>']
    xml.append('</channel></rss>')
    out = BASE / "feed.xml"
    out.write_text("\n".join(xml), encoding="utf-8")
    print(f"RSS 已生成: {out} ({len(items)} 条)")


def write_json(items, site: str):
    """JSON Feed 1.1（https://jsonfeed.org/version/1.1）。"""
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Hot News Radar 快照订阅",
        "home_page_url": f"{site}/",
        "feed_url": f"{site}/reports/feed.json",
        "description": "每日新闻雷达快照更新通知",
        "language": "zh-CN",
        "items": [
            {
                "id": it["guid"],
                "url": it["link"],
                "title": it["title"],
                "content_text": it["desc"],
                "date_published": it["published"].strftime("%Y-%m-%dT%H:%M:%S") + TZ_SUFFIX,
                "date_modified": it["modified"].strftime("%Y-%m-%dT%H:%M:%S") + TZ_SUFFIX,
            }
            for it in items
        ],
    }
    out = BASE / "feed.json"
    out.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 已生成: {out} ({len(items)} 条)")


def build():
    site = site_url()
    items = collect_items(site)
    if not items:
        print("无内容生成feed")
        return
    write_rss(items, site)
    write_json(items, site)


if __name__ == "__main__":
    build()
