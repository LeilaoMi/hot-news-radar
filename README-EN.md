<div align="center">

# 📡 Hot News Radar

**AI-driven full-coverage hot-news aggregation radar — no keyword limits**

Hourly multi-platform trending fetch · Full-coverage mode (no keyword filtering) ·
AI translation / semantic dedup / daily briefing · Scheduled push

[![GitHub Actions](https://img.shields.io/badge/⏰_Auto-GitHub_Actions-2088FF?logo=githubactions)](https://github.com/LeilaoMi/hot-news-radar/actions)
[![Reports](https://img.shields.io/badge/📊_Archive-Online-4285F4)](https://leilaomi.github.io/hot-news-radar/reports/archive.html)
[![License](https://img.shields.io/badge/⚖️_License-GPL--3.0-blue)](LICENSE)

</div>

---

## ✨ Features

- **Multi-platform**: 11 Chinese hot lists (Toutiao / Weibo / Bilibili / Douyin / Zhihu...)
  + 8 international RSS feeds (BBC / NYT / Guardian / Al Jazeera / UN News / HN / Yahoo Finance / FP)
- **Full-coverage mode**: empty keyword config = everything is kept — upstream TrendRadar
  is a keyword monitor, this fork is a full news radar
- **AI powered** (free GLM-4-Flash): title translation, optional semantic dedup
  (merging multi-source reports of the same event, off by default), daily analysis page
- **Web outputs**: hourly snapshot archive, AI daily page, trends page, RSS + JSON Feed,
  visual config editor and a keyword subscription generator — all static, served by GitHub Pages
- **Scheduled push**: morning brief (07:30–08:30) + evening digest (20:30–21:30)
- **Zero cost**: runs entirely within GitHub Actions free tier, 128 pytest cases included

## 🚀 Quick start

1. Fork / copy this repo
2. (Optional) Configure Secrets: `FEISHU_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, `AI_API_KEY`...
3. Trigger **Get Hot News** manually or wait for hourly cron (:33)

Local run requires Python ≥ 3.12 + [uv](https://docs.astral.sh/uv/):

```bash
uv sync --frozen --no-dev
uv run python -m trendradar
```

## 📊 Online resources

| Entry | Description |
|---|---|
| [Portal](https://leilaomi.github.io/hot-news-radar/) | Unified entry to everything below |
| [Latest report](https://leilaomi.github.io/hot-news-radar/reports/latest/current.html) | Most recent snapshot |
| [Daily digest](https://leilaomi.github.io/hot-news-radar/reports/latest/daily.html) | Accumulated daily view + AI analysis |
| [AI daily page](https://leilaomi.github.io/hot-news-radar/reports/ai-daily/index.html) | Daily AI analysis briefing |
| [History archive](https://leilaomi.github.io/hot-news-radar/reports/archive.html) | All snapshots by date |
| [Visual editor](https://leilaomi.github.io/hot-news-radar/editor.html) | Edit keywords & timeline in browser |
| [Subscription generator](https://leilaomi.github.io/hot-news-radar/subscribe.html) | Build `frequency_words.txt` snippets by clicking topics |

## 🙏 Credits

Built on top of [TrendRadar](https://github.com/sansan0/TrendRadar) (GPL-3.0).
Thanks [@sansan0](https://github.com/sansan0) for the excellent engine.
This repo adds full-coverage mode, RSS ingestion, semantic dedup, AI daily page,
subscription generator and the two-branch archive architecture.

## 📄 License

[GPL-3.0](./LICENSE), same as upstream TrendRadar.
