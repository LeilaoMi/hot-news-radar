<div align="center">

# 📡 Hot News Radar

**全量新闻雷达 —— 不限平台 · 不限关键词 · 不限类目**

每小时聚合 11 个国内热榜 + 8 个国际 RSS 源，AI 自动翻译与分析，
双时段推送到你的微信 / 飞书 / Telegram / 邮箱 / Bark。

[![⏰ 每小时自动运行](https://img.shields.io/badge/⏰_每小时自动运行-GitHub_Actions-2088FF?logo=githubactions)](https://github.com/LeilaoMi/hot-news-radar/actions)
[![📊 历史归档](https://img.shields.io/badge/📊_历史归档-在线浏览-4285F4)](https://leilaomi.github.io/hot-news-radar/reports/archive.html)
[![🤖 AI](https://img.shields.io/badge/🤖_AI-智谱GLM--4--Flash_免费-6E29F7)](https://open.bigmodel.cn)
[![⚖️ License](https://img.shields.io/badge/⚖️_License-GPL--3.0-blue)](LICENSE)

</div>

---

## 目录

- [项目简介](#项目简介)
- [功能总览](#功能总览)
- [数据源清单](#数据源清单)
- [运行机制](#运行机制)
- [每日时间线](#每日时间线)
- [AI 能力说明](#ai-能力说明)
- [历史归档](#历史归档)
- [部署指南](#部署指南)
- [配置详解](#配置详解)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [致谢与许可](#致谢与许可)

---

## 项目简介

这是一个**零成本运行在 GitHub Actions 上的个人新闻雷达**：

- **采集端**：每小时整点 :33 抓取国内 11 大平台热榜（头条、微博、B站、抖音、知乎等），
  以及 BBC、纽约时报、卫报、半岛电视台、联合国新闻等 8 个国际 RSS 源
- **处理端**：不做任何关键词过滤——**所有上榜内容全部保留**；
  英文标题由智谱 GLM-4-Flash 实时翻译为中文
- **输出端**：生成 HTML 快照页存入在线归档库（可回看任意一天），
  并按"早间速览 / 晚间汇总"两个时段推送精选简报

> 与上游 TrendRadar 的核心差异：上游是"关键词监控工具"，本项目是
> **"全量新闻雷达"**——词组配置为空即进入全量模式，任何类目的新闻都不会被漏掉。

## 🧭 站点导航

| 入口 | 说明 |
|---|---|
| [🏠 新闻中心（门户）](https://leilaomi.github.io/hot-news-radar/) | **统一入口**：今日热榜 / 当日汇总 / 历史归档 / 配置编辑器 |
| [🔥 今日实时热榜](https://leilaomi.github.io/hot-news-radar/reports/latest/current.html) | 最近一轮全平台热点（每小时更新，右下角一键展开/收起）|
| [📊 当日汇总](https://leilaomi.github.io/hot-news-radar/reports/latest/daily.html) | 全天累计去重视图 + AI 分析 |
| [🗂️ 历史归档](https://leilaomi.github.io/hot-news-radar/reports/archive.html) | 93 天 1000+ 快照按日期分组，任意页带导航条可跳转 |
| [⚙️ 配置编辑器](https://leilaomi.github.io/hot-news-radar/editor.html) | 网页端编辑时间线 / 关键词 / RSS 源 |

> 全站每一页顶部都有粘性导航条：`🎯 新闻中心 · 🗂 历史 · 📊 当日汇总 · ⚙️ 配置`

## 功能总览

| 功能 | 状态 | 说明 |
|---|:---:|---|
| 国内 11 平台热榜聚合 | ✅ | 头条/百度/华尔街见闻/澎湃/B站/财联社/凤凰/贴吧/微博/抖音/知乎 |
| 国际 8 源 RSS 订阅 | ✅ | BBC/NYT/Guardian/Al Jazeera/联合国中文/HN/Yahoo Finance/FP |
| 全量模式（无关键词过滤） | ✅ | 词组为空 = 显示全部上榜新闻 |
| AI 标题翻译 | ✅ | GLM-4-Flash，英文源自动转中文，实测 2/2 成功 |
| AI 趋势分析简报 | ✅ | 每日两次窗口期生成，上限 300 条素材 |
| 双时段定时推送 | ✅ | 早 07:30~08:30 速览 / 晚 20:30~21:30 全天汇总 |
| 历史快照归档 | ✅ | 每小时一份 HTML 存档 + 按日期分组归档页 |
| 可视化配置编辑器 | ✅ | 浏览器里直接改关键词/时间线/RSS |
| 零服务器成本 | ✅ | 全程跑在 GitHub Actions 免费额度内 |

## 数据源清单

### 国内热榜（11 个）

今日头条 · 百度热搜 · 华尔街见闻 · 澎湃新闻 · bilibili 热搜 · 财联社 · 凤凰网 · 百度贴吧 · 微博热搜 · 抖音热点 · 知乎热榜

> 数据经 [newsnow](https://github.com/ourongxing/newsnow) 公共 API 聚合，支持在
> `config.yaml → platforms.sources` 中增删或自建 API 地址（`api_url` 字段）。

### 国际 RSS（8 个）

| 源 | 类型 | 说明 |
|---|---|---|
| BBC News | 综合国际 | 全球头条 |
| The New York Times | 综合国际 | World 版块 |
| The Guardian | 综合国际 | World 版块 |
| Al Jazeera | 综合国际 | 中东及全球视角 |
| 联合国新闻 | 综合（中文原生） | 联合国系统官方动态 |
| Hacker News | 科技社区 | 创业与技术圈风向 |
| Yahoo Finance | 财经 | 全球市场动态 |
| Foreign Policy | 国际关系 | 深度外交政策分析 |

英文源标题会由 AI 自动翻译为中文后展示。新增源只需编辑
`config/config.yaml → rss.feeds`，填 `id / name / url / max_age_days` 四项即可。

## 运行机制

```
GitHub Actions (cron :33 每小时)
        │
        ▼
┌─ 采集层 ─────────────────────────────┐
│ newsnow API ──► 11 个国内热榜          │
│ RSS Parser  ──► 8 个国际订阅源         │
│   (新鲜度过滤: 仅推送24h内文章)          │
└──────────────┬───────────────────────┘
               ▼
┌─ 处理层 ─────────────────────────────┐
│ frequency_words.txt 为空 → 全量放行     │
│ SQLite 本地存储（guid 去重）            │
│ GLM-4-Flash: 翻译英文标题               │
│ 时间窗内: AI 筛选+趋势分析              │
└──────────────┬───────────────────────┘
               ▼
┌─ 输出层 ─────────────────────────────┐
│ output/html/YYYY-MM-DD/HH-MM.html 快照 │
│ scripts/gen_archive.py → 归档索引页     │
│ git push → GitHub Pages 在线浏览        │
│ 推送渠道: 飞书/TG/邮件/Bark/ntfy...     │
└──────────────────────────────────────┘
```

## 每日时间线

当前启用 **custom「早晚各一次」模板**（`config/timeline.yaml`）：

| 时段 | 窗口 | 行为 |
|---|---|---|
| 默认（窗口外） | 其余时间 | 只采集入库，不打扰 |
| ☀️ 早班速览 | 07:30 ~ 08:30 | 推送当前热榜速览 + AI 分析（仅一次） |
| 🌙 晚间汇总 | 20:30 ~ 21:30 | 推送当日累计汇总 + AI 分析（仅一次） |

修改时间：编辑 `config/timeline.yaml → custom.periods`，或用
[网页编辑器](https://leilaomi.github.io/hot-news-radar/index.html) 直接拖拽。

## AI 能力说明

| 能力 | 模型 | 触发时机 |
|---|---|---|
| 标题翻译 | GLM-4-Flash | 每轮运行时对英文 RSS 源执行 |
| 智能筛选 | GLM-4-Flash | 仅在早晚推送窗口内执行 |
| 趋势分析 | GLM-4-Flash | 仅在早晚推送窗口内执行 |

- 模型路由：`openai/glm-4-flash` + 端点 `https://open.bigmodel.cn/api/paas/v4`
  （走 OpenAI 兼容协议，稳定且不受 LiteLLM 版本影响）
- Key 配置：仓库 Secrets 的 `AI_API_KEY`（[bigmodel.cn 免费注册](https://open.bigmodel.cn)）
- 免费额度完全够用；如需换模型改 `config.yaml → ai.model` 即可
- 分批保护：`batch_size: 100 / batch_interval: 2s`，避免大量标题单次请求溢出

## 历史归档

- **入口**：[新闻中心门户](https://leilaomi.github.io/hot-news-radar/) → 🗂️ 历史归档
- 每小时快照自动存入 `docs/reports/YYYY-MM-DD/HH-MM.html`
- 归档页由 `scripts/gen_archive.py` 在每轮发布时重新生成：
  按日期折叠分组、显示覆盖天数/快照总数/单日峰值统计
- 数据永久保留（受限于仓库体积，未来可加自动清理旧于 N 天的策略）

## 部署指南

1. Fork 或导入本仓库到你的 GitHub 账号
2. 仓库 Settings → Secrets and variables → Actions，添加：

| Secret | 必填 | 用途 |
|---|:---:|---|
| `FEISHU_WEBHOOK_URL` | 推荐 | 飞书群机器人 webhook |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 可选 | Telegram 推送 |
| `BARK_URL` / `NTFY_TOPIC` | 可选 | iOS / Android 免 App 推送 |
| `EMAIL_FROM/PASSWORD/TO/SMTP_SERVER/SMTP_PORT` | 可选 | 邮件推送 |
| `AI_API_KEY` | 推荐 | 智谱 key（免费），启用 AI 翻译/分析 |
| `CLOUDFLARE_API_TOKEN/_ACCOUNT_ID/_PROJECT_NAME` | 可选 | 加配后自动部署 CF Pages，国内访问加速 |

3. Actions 页手动触发 **Get Hot News** 一次验证链路
4. （可选）本地调试：

```bash
git clone https://github.com/LeilaoMi/hot-news-radar.git
cd hot-news-radar
uv sync --frozen --no-dev
cp docker/.env.example .env    # 按需填写
uv run python -m trendradar    # 手动跑一轮
```

Docker 方式见 `docker/docker-compose.yml`。

## 配置详解

核心文件都在 `config/` 下，改完无需重启（下轮运行自动生效）：

| 文件 | 作用 |
|---|---|
| `config.yaml` | 主配置：数据源开关 / RSS / AI / 推送渠道 / 报告样式 |
| `frequency_words.txt` | 关键词组。**当前为空 = 全量模式**；语法见文件内注释（必须词+/ 过滤词！ 正则// 别名=> 上限@） |
| `timeline.yaml` | 调度模板：时段窗口 / 是否分析 / 是否推送 / 星期映射 |
| `ai_analysis_prompt.txt` | AI 分析提示词（v2.0.0，JSON 输出格式） |
| `ai_translation_prompt.txt` | 翻译提示词（v1.2.0） |

常用调整示例：

```yaml
# config.yaml — 只想看某个平台的新闻？
platforms:
  sources:
    - id: "weibo"
      name: "微博"
      enabled: false        # 改成 false 即关闭该平台

# rss — 新增一个源
rss:
  feeds:
    - id: "my-feed"
      name: "我的订阅"
      url: "https://example.com/rss.xml"
      max_age_days: 1       # 只推 24 小时内的
```

## 目录结构

```
hot-news-radar/
├── .github/workflows/
│   ├── crawler.yml          # 主流程：抓取→AI→报告→发布→推送
│   └── clean-crawler.yml    # （已弃用的签到机制占位）
├── trendradar/              # 核心 Python 引擎（来自上游 v6.10.0）
│   ├── ai/                  #   AI 客户端/筛选管线/翻译/格式化
│   ├── core/                #   配置加载/调度器/频率词解析
│   ├── crawler/             #   热榜抓取 + RSS 解析
│   ├── notification/        #   多渠道推送分发
│   ├── report/              #   HTML/Markdown 报告渲染
│   ├── storage/             #   SQLite 存储 + guid 去重
│   └── commands/            #   doctor/status/version 运维命令
├── mcp_server/              # MCP Server（可接 Cherry Studio 等）
├── scripts/
│   └── gen_archive.py       # 归档索引页生成器（本项目新增）
├── config/                  # 所有用户配置（见上表）
├── docs/
│   ├── index.html           # 站点门户（新闻中心）
│   ├── editor.html          # 可视化配置编辑器
│   ├── reports/             # 每小时快照存档（自动增长）
│   └── assets/              # 编辑器静态资源
├── docker/                  # Docker 部署相关
├── index.html               # 最新报告页（Actions 自动更新）
└── version*                 # 引擎版本追踪文件
```

## 常见问题

**Q: 为什么叫"全量模式"？和关键词监控有什么区别？**
A: 引擎逻辑是"词组为空 → 所有标题匹配"。上游 TrendRadar 设计初衷是监控特定话题，
本项目把词组清空，让它变成一个纯粹的新闻聚合雷达，任何热点都不漏。
想恢复定向监控？往 `frequency_words.txt` 加词组就行，语法见文件内注释。

**Q: 每小时跑一次会不会超 GitHub Actions 免费额度？**
A: 公共仓库 Actions 完全免费；私有仓库每月 2000 分钟，本任务单次约 3 分钟，
每小时一次 ≈ 2160 分钟/月，建议保持公共仓库或降到每 2 小时一次（改 cron 第一个字段）。

**Q: AI 是必须的吗？**
A: 不是。不配 `AI_API_KEY` 时系统回退到纯关键词/无过滤展示，只是没有翻译和分析。

**Q: 为什么有些天快照少？**
A: 快照数量取决于当天的调度窗口与 GitHub Actions 偶发的排队延迟，属正常现象。

**Q: 如何彻底删除签到机制残留？**
A: 已删除 workflow 内的检查逻辑，`.github/workflows/clean-crawler.yml`
只剩注释占位，可直接删除该文件不影响任何功能。

## 致谢与许可

- **引擎**：[TrendRadar](https://github.com/sansan0/TrendRadar) by [@sansan0](https://github.com/sansan0)（GPL-3.0）——
  没有这个优秀的开源项目就没有本雷达
- **热榜数据**：[newsnow](https://github.com/ourongxing/newsnow) 公共聚合 API
- **AI**：[智谱 BigModel](https://open.bigmodel.cn) 提供的免费 GLM-4-Flash

本项目遵循 [GPL-3.0](./LICENSE) 开源，与上游保持一致。
