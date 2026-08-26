<div align="center">

# 📡 Hot News Radar

**AI-driven multi-platform hot-topic aggregation & monitoring radar**

Hourly multi-platform trending fetch · Keyword radar filtering · AI analysis · Scheduled push

[![GitHub Actions](https://img.shields.io/badge/⏰_Auto-GitHub_Actions-2088FF?logo=githubactions)](https://github.com/LeilaoMi/hot-news-radar/actions)
[![Reports](https://img.shields.io/badge/📊_Archive-Online-4285F4)](https://leilaomi.github.io/hot-news-radar/reports/archive.html)
[![License](https://img.shields.io/badge/⚖️_License-GPL--3.0-blue)](LICENSE)

</div>

---

## ✨ Features

- **Multi-platform**: Toutiao / Baidu / Wallstreetcn / Bilibili / Douyin / Weibo / Zhihu hot lists in one place
- **Keyword radar**: 11+ custom keyword groups (AI/LLM/Chips, Tech giants, International, Policy, Economy, Safety, EV, Robotics, Aerospace, Entertainment...)
- **AI powered**: LLM-based smart filtering, title translation & trend briefing
- **Scheduled push**: morning brief (07:30–08:30) + evening digest (20:30–21:30)
- **History archive**: hourly snapshots browsable at the [archive page](https://leilaomi.github.io/hot-news-radar/reports/archive.html)
- **Zero cost**: runs entirely within GitHub Actions free tier

## 🚀 Quick start

1. Fork / copy this repo
2. (Optional) Configure Secrets: `FEISHU_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, `AI_API_KEY`...
3. Trigger **Get Hot News** manually or wait for hourly cron (:33)

## 📊 Online resources

| Entry | Description |
|---|---|
| [Latest report](https://leilaomi.github.io/hot-news-radar/reports/index.html) | Most recent snapshot |
| [History archive](https://leilaomi.github.io/hot-news-radar/reports/archive.html) | All snapshots by date |
| [Visual editor](https://leilaomi.github.io/hot-news-radar/index.html) | Edit keywords & timeline in browser |

## 🙏 Credits

Built on top of [TrendRadar](https://github.com/sansan0/TrendRadar) (GPL-3.0).
Thanks [@sansan0](https://github.com/sansan0) for the excellent engine.
This repo adds custom configs, scheduling and history-archive extension.

## 📄 License

[GPL-3.0](./LICENSE), same as upstream TrendRadar.
