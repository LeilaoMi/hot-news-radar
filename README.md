<div align="center">

# 📡 Hot News Radar

**AI 驱动的多平台热点聚合与监控雷达**

每小时自动抓取 10+ 平台热榜 · 关键词精准过滤 · AI 智能分析 · 双时段推送

[![GitHub Actions](https://img.shields.io/badge/⏰_自动运行-GitHub_Actions-2088FF?logo=githubactions)](https://github.com/LeilaoMi/hot-news-radar/actions)
[![Reports](https://img.shields.io/badge/📊_历史归档-在线浏览-4285F4)](https://leilaomi.github.io/hot-news-radar/reports/archive.html)
[![License](https://img.shields.io/badge/⚖️_License-GPL--3.0-blue)](LICENSE)

</div>

---

## ✨ 功能特性

- **多平台聚合**：头条 / 百度 / 华尔街见闻 / 沸腾 / B站 / 抖音 / 微博 / 知乎等主流热榜一站抓取
- **关键词雷达**：内置 11+ 大类自定义关键词组（AI·大模型·芯片 / 科技大厂 / 国际局势 / 国内政策 / 民生 / 经济财经 / 突发安全 / 新能源汽车 / 机器人 / 航天前沿 / 文化娱乐），命中即推送
- **AI 加持**：接入 LLM 做智能筛选、标题翻译、趋势分析简报
- **双时段推送**：早间 07:30~08:30 推当日速览，晚间 20:30~21:30 推全天汇总（可自定义）
- **历史归档**：每小时快照自动存档，[归档页](https://leilaomi.github.io/hot-news-radar/reports/archive.html)按日期回看任意历史热点
- **零成本运行**：GitHub Actions 免费额度内跑，无需服务器

## 🚀 快速开始

1. Fork 或复制本仓库到你的 GitHub 账号
2. （可选）在仓库 Settings → Secrets 配置推送渠道与 AI Key：
   `FEISHU_WEBHOOK_URL` / `TELEGRAM_BOT_TOKEN` / `AI_API_KEY` 等
3. Actions 页面手动触发一次 **Get Hot News**，或等待整点 :33 自动运行

## 📊 在线资源

| 入口 | 说明 |
|---|---|
| [最新报告](https://leilaomi.github.io/hot-news-radar/reports/index.html) | 最近一轮抓取的热点快照 |
| [历史归档](https://leilaomi.github.io/hot-news-radar/reports/archive.html) | 全部历史快照，按日期分组 |
| [可视化编辑器](https://leilaomi.github.io/hot-news-radar/index.html) | 网页端编辑关键词与时间线 |

## ⚙️ 本地开发

```bash
git clone https://github.com/LeilaoMi/hot-news-radar.git
cd hot-news-radar
uv sync --frozen --no-dev
cp docker/.env.example .env && $EDITOR .env  # fill in your channels/keys
uv run python -m trendradar   # 手动跑一轮
```

## 🙏 致谢

核心引擎基于 [TrendRadar](https://github.com/sansan0/TrendRadar) (GPL-3.0) 构建，
感谢原作者 [@sansan0](https://github.com/sansan0) 的优秀工作。
本项目在其基础上做了个性化配置、调度定制与历史归档扩展。

## 📄 License

[GPL-3.0](./LICENSE)，与上游 TrendRadar 保持一致。
