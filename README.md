# TrendRadar @ Zo Computer

部署在 **Zo Computer**（绕过 GitHub Actions 被限制问题），全功能在线。

## 🌐 访问入口

- **Web 报告归档**：https://trendradar-web-truepuma.zocomputer.io/
- **最新增量分析**：https://trendradar-web-truepuma.zocomputer.io/latest/incremental.html
- **微信推送**：每小时自动检查，有匹配新闻就推（Server酱 → 服务通知）

## 📦 已部署的 Zo 服务

| 服务 ID | 名称 | 类型 | 作用 |
|---|---|---|---|
| `svc_F8OxTGOgFso` | `trendradar-crawler` | process | 每小时跑一次 `python -m trendradar`，无尽循环 |
| `svc_rFL_kKOUisY` | `trendradar-web` | http | 提供 HTML 报告归档 Web 界面 |

## 🤖 AI 分析架构

由于 Gemini 在国内 0 配额，改走 **Cloudflare Workers AI**（你已有的资源）：

```
TrendRadar (Python)
  → 走 LiteLLM OpenAI 兼容协议
  → POST https://news-digest.horjane.workers.dev/ai/v1/chat/completions
  → Worker 上的 /ai 代理（OpenAI 协议 → env.AI.run 转换）
  → Cloudflare Workers AI (Llama 3.3 70B Instruct)
  → 结果原路返回
```

- 模型：`@cf/meta/llama-3.3-70b-instruct-fp8-fast`
- 鉴权：Bearer Token (Worker secret `AI_PROXY_KEY`)
- 不走 leilaomi.cc.cd 域名（CF 默认 Bot Fight Mode 会拦 OpenAI Python SDK 的 UA），改走 workers.dev 子域

## 🔑 环境配置

所有 secret 都在本地 `.env` 文件（mode 600，只 root 可读）：

| 变量 | 用途 |
|---|---|
| `AI_API_KEY` | 24-byte hex，TrendRadar → CF AI 代理鉴权 |
| `AI_API_BASE` | https://news-digest.horjane.workers.dev/ai/v1 |
| `AI_MODEL` | openai/meta/llama-3.3-70b-instruct-fp8-fast |
| `GENERIC_WEBHOOK_URL` | Server酱 SendKey URL |
| `GENERIC_WEBHOOK_TEMPLATE` | `{"title":"{title}","desp":"{content}"}` |

## 📰 已启用的数据源

11 个国内热榜聚合器：
- 微博热搜 / 贴吧 / 今日头条 / 知乎 / 抖音 / B站
- 百度热搜 / 澎湃 / 凤凰 / 财联社 / 华尔街见闻

## 🔍 关键词主题（民生 + 科技 + 时事 三套）

民生：房价 / 物价 / 工资 / 就业 / 医保 / 社保 / 养老 / 教育 / 食品安全 / 公共卫生 / 出行 / 城市民生 / 公平正义  
科技：DeepSeek / 华为 / 中国 AI / 国产新能源车 / 特斯拉马斯克 / 国产机器人 / 字节腾讯阿里 / 新能源 / 航天 / 前沿科技 / 自动驾驶  
时事：宏观经济 / 反腐 / 中美 / 中俄 / 国际冲突 / 周边外交

详见 `config/frequency_words.txt`

## 🔄 运维操作

```bash
# 查看 crawler 实时日志
tail -f /dev/shm/trendradar-crawler.log

# 重启 crawler（修改 .env 后需要）
# 通过 Zo 工具：update_user_service(service_id='svc_F8OxTGOgFso')

# 手动跑一次
cd /home/workspace/Projects/trendradar
set -a; source .env; set +a
uv run python -m trendradar
```

## ⚙️ 后续可选

- **绑定自定义域名** `trend.leilaomi.cc.cd`：需要 Zo 付费版（免费版 0 个 custom domain 配额），或通过 CF Worker 反代  
- **重启 GitHub Actions**：等 GitHub 客服回复你的 ticket 后再说，那边和这边互不冲突，到时候可以二选一
