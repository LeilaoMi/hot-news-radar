# coding=utf-8
"""AI 日报生成测试"""

from datetime import datetime

from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.report.ai_daily import render_ai_daily_html, write_ai_daily

# 固定测试时钟：写盘测试必须显式传 now，否则随真实日期漂移
# （2026-09-09 起曾因未传 now 而日更失败——时间炸弹）。
FROZEN_NOW = datetime(2026, 9, 8, 15, 30)


def _ai_result(**overrides):
    result = AIAnalysisResult(
        core_trends="今天科技板块热度最高。\n\n其次是财经事件，市场反应平淡。",
        sentiment_controversy="舆论整体偏正面。",
        signals="暂无明显异动。",
        rss_insights="",   # 空板块不应渲染
        outlook_strategy="建议持续关注后续进展。",
        success=True,
    )
    for k, v in overrides.items():
        setattr(result, k, v)
    return result


def _stats():
    return [
        {"word": "AI", "count": 12, "percentage": 30.0, "titles": []},
        {"word": "新能源", "count": 8, "percentage": 20.0, "titles": []},
        {"word": "芯片", "count": 5, "percentage": 12.5, "titles": []},
    ]


def _render():
    return render_ai_daily_html(_ai_result(), _stats(), "2026-09-08", "15:30")


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def test_render_contains_sections():
    h = _render()
    assert "AI 热点日报" in h
    assert "核心热点与舆情态势" in h
    assert "舆论风向与争议" in h
    assert "异动与弱信号" in h
    assert "研判与策略建议" in h
    assert "RSS 深度洞察" not in h      # 空板块不渲染
    assert "2026-09-08" in h and "15:30" in h


def test_render_escapes_html_in_ai_text():
    result = _ai_result(core_trends="恶意<script>alert(1)</script>内容")
    h = render_ai_daily_html(result, _stats(), "2026-09-08", "15:30")
    assert "<script>" not in h
    assert "&lt;script&gt;" in h


def test_render_paragraphs_from_blank_lines():
    h = _render()
    # "今天科技板块热度最高。\n\n其次是..." → 两个 <p>
    assert "<p>今天科技板块热度最高。</p>" in h
    assert "<p>其次是财经事件，市场反应平淡。</p>" in h


def test_render_summary_chips_sorted():
    h = _render()
    assert "AI · 12条" in h
    assert "新能源 · 8条" in h
    assert "芯片 · 5条" in h
    assert h.index("AI · 12条") < h.index("新能源 · 8条")   # 降序


def test_render_empty_content_fallback():
    result = _ai_result(
        core_trends="", sentiment_controversy="",
        signals="", rss_insights="", outlook_strategy="",
    )
    h = render_ai_daily_html(result, [], "2026-09-08", "15:30")
    assert "本期 AI 分析无实质内容" in h


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------

def test_write_ai_daily_outputs(tmp_path):
    path = write_ai_daily(_ai_result(), _stats(),
                          output_dir=str(tmp_path), now=FROZEN_NOW)
    assert path is not None
    index = tmp_path / "html" / "ai-daily" / "index.html"
    archive = tmp_path / "html" / "ai-daily" / "2026-09-08.html"
    assert index.exists() and archive.exists()
    assert index.read_text(encoding="utf-8") == archive.read_text(encoding="utf-8")


def test_write_ai_daily_skips_failed_result(tmp_path):
    path = write_ai_daily(
        _ai_result(success=False), _stats(), output_dir=str(tmp_path)
    )
    assert path is None
    assert not (tmp_path / "html" / "ai-daily").exists()


def test_write_ai_daily_none_result(tmp_path):
    assert write_ai_daily(None, _stats(), output_dir=str(tmp_path)) is None


def test_write_ai_daily_overwrites_same_day(tmp_path):
    write_ai_daily(_ai_result(), _stats(),
                   output_dir=str(tmp_path), now=FROZEN_NOW)
    # 同日再次运行：覆盖更新，不报错不堆积
    path = write_ai_daily(_ai_result(signals="更新后的异动"), _stats(),
                          output_dir=str(tmp_path), now=FROZEN_NOW)
    archive = tmp_path / "html" / "ai-daily" / "2026-09-08.html"
    assert "更新后的异动" in archive.read_text(encoding="utf-8")
    assert path is not None
