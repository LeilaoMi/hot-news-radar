<div align="center">

# 📡 Hot News Radar

**Full-coverage news radar — no platform, keyword or category limits**

11 Chinese hot lists + 8 international RSS feeds, with AI translation, semantic dedup,
trend analysis and a daily briefing, published as a static site.

[![Get Hot News](https://github.com/LeilaoMi/hot-news-radar/actions/workflows/crawler.yml/badge.svg)](https://github.com/LeilaoMi/hot-news-radar/actions/workflows/crawler.yml)
[![Tests](https://github.com/LeilaoMi/hot-news-radar/actions/workflows/test.yml/badge.svg)](https://github.com/LeilaoMi/hot-news-radar/actions/workflows/test.yml)
[![Site](https://img.shields.io/badge/🌐_Site-news.leilaomi.abrdns.com-4285F4)](https://news.leilaomi.abrdns.com/)
[![License](https://img.shields.io/badge/⚖️_License-GPL--3.0-blue)](LICENSE)

</div>

---

The full Chinese documentation is in [README.md](README.md). This page is a summary.

## What it does

- **Collects** 11 Chinese hot lists (via [newsnow](https://github.com/ourongxing/newsnow)) and 8 RSS feeds
  (BBC, NYT, Guardian, Al Jazeera, UN News, Foreign Policy, Hacker News, Yahoo Finance).
- **Keeps everything**: an empty `config/frequency_words.txt` means full-coverage mode.
  Upstream [TrendRadar](https://github.com/sansan0/TrendRadar) is a keyword monitor; this fork is a news radar.
- **AI** (free GLM-4-Flash, `openai/glm-4-flash-250414`): RSS title translation with strict numbered alignment,
  multi-source semantic dedup, trend analysis and an AI daily page. All AI work in one run shares a 480 s budget
  with a circuit breaker; truncated or invalid output counts as failure and is never cached.
- **Publishes** per-run snapshots, a daily digest, the AI daily page, a trends page and RSS / JSON feeds to the
  `reports` branch, served by GitHub Pages at [news.leilaomi.abrdns.com](https://news.leilaomi.abrdns.com/).

## How it runs

- Workflow `crawler.yml`, cron `5,35 * * * *`. GitHub actually fires about 4–6 times a day, 4–7 minutes each.
- Windows (Asia/Shanghai): morning 06:00–10:00 (current mode) and evening 19:00–23:59 (daily mode),
  analysis and push once per window. No notification channel is configured on the live site.
- Cross-run state (`output/news`, `output/rss`, `output/ai_cache.db`) is kept with `actions/cache`,
  so once-per-window, new-item detection, daily totals and the AI cache work across runs.
- A stale daily page is re-rendered with `RENDER_ONLY=true` (no crawl, no AI, no push).
- Exit codes: `0` success, `1` fatal (all platforms failed, storage or render failure, config error),
  `2` platform failures above `PLATFORM_FAILURE_THRESHOLD`.
- `sync_reports_branch.py` pushes with `--force-with-lease` against a base commit, so concurrent runs
  cannot drop each other's snapshots. Hourly snapshots are kept 90 days.

## Quick start

```bash
git clone https://github.com/LeilaoMi/hot-news-radar.git
cd hot-news-radar
uv sync --frozen
uv run python -m trendradar          # one run
uv run python -m trendradar --doctor # health check
uv run --with pytest pytest          # 556 tests
```

For GitHub Actions, fork the repo, add `AI_API_KEY` (optional) and set Pages to the `reports` branch.
Notification channels, S3, Cloudflare Pages and ntfy failure alerts are all optional secrets.

## Docker and MCP

```bash
cd docker && cp .env.example .env    # MCP container requires MCP_AUTH_TOKEN (16+ chars)
docker compose -f docker-compose-build.yml up -d --build
```

Images build locally (`trendradar:local`, `trendradar-mcp:local`), run as non-root with a HEALTHCHECK.
The MCP server (27 tools) binds `127.0.0.1` by default, requires a bearer token on non-loopback addresses
and validates Host/Origin against DNS rebinding. See [README-MCP-FAQ-EN.md](README-MCP-FAQ-EN.md).

## Security

http/https-only link allowlist in every report and push, CSP on report pages, escaped editor output,
masked secrets in logs, least-privilege CI with SHA-pinned actions, and a dependency set with no known
advisories. Never install litellm 1.82.7 or 1.82.8 (known malicious releases).

## Known limits

- GitHub's schedule drifts; on some days no run lands in the evening window.
- `NTFY_TOPIC` is used both for failure alerts and for full report pushes; don't set it if you only want alerts.

## License

GPL-3.0, same as upstream TrendRadar.
