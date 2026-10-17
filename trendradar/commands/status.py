# coding=utf-8
"""调度状态查看命令"""

from typing import Dict

from trendradar import __version__
from trendradar.context import AppContext


def handle_status_commands(config: Dict) -> None:
    """显示当前调度状态"""
    ctx = AppContext(config)

    print("=" * 60)
    print(f"TrendRadar v{__version__} 调度状态")
    print("=" * 60)

    try:
        scheduler = ctx.create_scheduler()
        schedule = scheduler.resolve()

        now = ctx.get_time()
        date_str = ctx.format_date()

        print(f"\n⏰ 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} ({ctx.timezone})")
        print(f"📅 当前日期: {date_str}")

        print("\n📋 调度信息:")
        print(f"  日计划: {schedule.day_plan}")
        if schedule.period_key:
            print(f"  当前时间段: {schedule.period_name or schedule.period_key} ({schedule.period_key})")
        else:
            print("  当前时间段: 无（使用默认配置）")

        print("\n🔧 行为开关:")
        print(f"  采集数据: {'✅ 是' if schedule.collect else '❌ 否'}")
        print(f"  AI 分析:  {'✅ 是' if schedule.analyze else '❌ 否'}")
        print(f"  推送通知: {'✅ 是' if schedule.push else '❌ 否'}")
        print(f"  报告模式: {schedule.report_mode}")
        print(f"  AI 模式:  {schedule.ai_mode}")

        once_key = schedule.once_key
        if once_key and (schedule.once_analyze or schedule.once_push):
            print("\n🔁 一次性控制（按窗口计，跨午夜时段归属窗口起始日）:")
            if schedule.once_analyze:
                if schedule.period_key:
                    already_analyzed = scheduler.already_executed(schedule.period_key, "analyze", date_str)
                    print(f"  AI 分析:  仅一次 {'(本窗口已执行 ⚠️)' if already_analyzed else '(本窗口未执行 ✅)'}")
                else:
                    print("  AI 分析:  仅一次（默认时段的分析去重由 AI 层负责，按时间段生效）")
            else:
                print("  AI 分析:  不限次数")
            if schedule.once_push:
                already_pushed = scheduler.already_executed(once_key, "push", date_str)
                print(f"  推送通知: 仅一次 {'(本窗口已执行 ⚠️)' if already_pushed else '(本窗口未执行 ✅)'}")
            else:
                print("  推送通知: 不限次数")

    except Exception as e:
        print(f"\n❌ 获取调度状态失败: {e}")

    print("\n" + "=" * 60)

    ctx.cleanup()
