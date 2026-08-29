#!/usr/bin/env python3
"""重构回归检查：把「拆分前」的行为固化成基准，每次改动后重跑比对。

拆分 NewsAnalyzer 这类大手术，没有基准等于闭眼开车。本脚本把三类检查打包：
  1. 单元测试（行为正确性的主要防线）
  2. 语法检查（所有 .py 文件能编译）
  3. 导入检查（模块间依赖没被拆断，且不产生副作用崩溃）

第四项「完整运行」代价高（要真实抓取），默认关闭，用 --full 开启。

退出码：0 = 全部通过；1 = 有失败项。
"""
import argparse
import compileall
import importlib
import io
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# 拆分过程中重点盯的模块，导入失败通常意味着循环依赖或漏改 import
IMPORT_TARGETS = [
    "trendradar",
    "trendradar.__main__",
    "trendradar.context",
    "trendradar.core.analyzer",
    "trendradar.core.loader",
    "trendradar.core.scheduler",
    "trendradar.storage.base",
    "trendradar.storage.local",
    "trendradar.storage.remote",
    "trendradar.storage.sqlite_mixin",
    "trendradar.storage.manager",
    "trendradar.crawler.rss.fetcher",
    "trendradar.report.html",
    "trendradar.notification.dispatcher",
    "trendradar.notification.senders",
    "trendradar.notification.splitter",
    "trendradar.ai.analyzer",
    "trendradar.ai.filter_pipeline",
    "trendradar.utils.time",
    "trendradar.utils.url",
    "trendradar.core.frequency",
]

results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


def run_tests():
    print("\n[1/4] 单元测试")
    t0 = time.time()
    r = subprocess.run(
        [PY, "-m", "pytest", "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = (r.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else "(无输出)"
    ok = r.returncode == 0
    record(f"pytest  ({time.time() - t0:.1f}s)", ok, summary)
    if not ok:
        print((r.stdout or "")[-2000:])
    return ok


def run_syntax():
    print("\n[2/4] 语法检查")
    buf = io.StringIO()
    ok = compileall.compile_dir(
        str(ROOT / "trendradar"), quiet=2, force=True,
    )
    # compile_dir 会把输出打到 stdout，这里屏蔽掉噪音
    _ = buf.getvalue()
    files = list((ROOT / "trendradar").rglob("*.py"))
    record(f"compileall trendradar/  ({len(files)} 个文件)", ok)
    return ok


def run_imports():
    print("\n[3/4] 导入检查")
    sys.path.insert(0, str(ROOT))
    all_ok = True
    for mod in IMPORT_TARGETS:
        t0 = time.time()
        try:
            importlib.import_module(mod)
            dt = time.time() - t0
            # 导入超过 1 秒说明有重型依赖被提到了模块顶层（曾导致启动慢 7.6s）
            slow = "  ⚠️ 导入耗时偏高" if dt > 1.0 else ""
            record(f"import {mod}", True, f"{dt * 1000:.0f}ms{slow}")
        except Exception as e:
            record(f"import {mod}", False, f"{type(e).__name__}: {e}")
            all_ok = False
    return all_ok


def run_full():
    print("\n[4/4] 完整运行（真实抓取）")
    env = dict(os.environ)
    env["SCHEDULE_ENABLED"] = "false"
    env["REPORT_MODE"] = "daily"
    t0 = time.time()
    r = subprocess.run(
        [PY, "-m", "trendradar"], cwd=ROOT, capture_output=True, text=True,
        env=env, timeout=900,
    )
    out = (r.stdout or "") + (r.stderr or "")
    ok = r.returncode == 0
    record(f"python -m trendradar  ({time.time() - t0:.0f}s)", ok, f"exit={r.returncode}")

    # 抓报告头，作为内容层面的基准
    import re
    for kw in ("报告类型", "生成时间"):
        m = re.search(rf"{kw}.{{0,80}}?", out)
        if m:
            print(f"      {m.group(0)[:90]}")
    if not ok:
        print(out[-2500:])
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="额外跑一次完整抓取（慢）")
    args = ap.parse_args()

    print("=" * 78)
    print(f"回归检查 · {ROOT.name}")
    print("=" * 78)

    run_tests()
    run_syntax()
    run_imports()
    if args.full:
        run_full()

    failed = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 78)
    print(f"结果：{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print("  ❌", n)
        print("=" * 78)
        return 1
    print("✅ 全部通过")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
