#!/usr/bin/env python3
"""注入脚本回归测试 —— 防止 gen_archive.py 里的内联 JS 再次退化。

历史教训：COLLAPSE_JS 曾是一个 1300 字符的单行字符串，同时藏着三个缺陷
（缺分号 / forEach 括号顺序写反 / 按钮未 appendChild），因为没有任何校验，
语法错误被静默注入到几百个快照页里，浏览器控制台报
"Unexpected token 'var'"，折叠按钮从未出现过。

本测试做三件事：
  1. 用 node --check 校验每个内联 script 块的语法（node 不存在则跳过）
  2. 校验导航条相对前缀在各层级下的计算结果
  3. 校验 COLLAPSE_JS 确实把按钮挂到了 DOM 上

用法: python3 scripts/test_injected_js.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_archive as ga  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  -- {detail}" if detail else ""))


def main():
    node = shutil.which("node")

    # ---- 1. 语法校验 ----
    print("\n== 1. 内联脚本语法校验 ==")
    blocks = {}
    for name in ("COLLAPSE_JS", "SEARCH_JS", "SEARCHBAR_JS"):
        for i, code in enumerate(re.findall(r"<script>(.*?)</script>", getattr(ga, name), re.S)):
            blocks[f"{name}[{i}]" if i else name] = code

    check("至少提取到 3 段脚本", len(blocks) >= 3, f"{len(blocks)} 段")

    if node:
        with tempfile.TemporaryDirectory() as td:
            for label, code in blocks.items():
                f = Path(td) / (re.sub(r"\W", "_", label) + ".js")
                f.write_text(code, encoding="utf-8")
                r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
                err = (r.stderr or "").strip().splitlines()
                check(f"{label} 语法通过", r.returncode == 0, err[-1] if err else "")
    else:
        print("  [SKIP] 未找到 node，跳过语法校验")

    # ---- 2. 折叠按钮必须真的挂到 DOM ----
    print("\n== 2. 折叠按钮挂载检查 ==")
    check("COLLAPSE_JS 含 appendChild",
          "document.body.appendChild(btn)" in ga.COLLAPSE_JS,
          "缺失会导致按钮永远不显示")

    # ---- 3. 导航条相对前缀 ----
    print("\n== 3. 导航条相对前缀（GitHub Pages 子路径部署）==")
    cases = [
        ("docs/reports/archive.html", "../"),
        ("docs/reports/latest/daily.html", "../../"),
        ("docs/reports/2025-08-29/08-00.html", "../../"),
        ("docs/reports/archive-daily/2025-05-01-daily.html", "../../"),
        ("docs/index.html", "./"),
    ]
    for rel, expect in cases:
        nav = ga._nav_for(Path(rel))
        got = re.search(r'href="([^"]*)"', nav).group(1)
        check(f"{rel} -> {expect}", got == expect, f"实际 {got}")

    # 导航条不得再出现绝对路径
    has_abs = any('href="/' in ga._nav_for(Path(c)) for c, _ in cases)
    check("导航条无绝对路径", not has_abs)

    n_fail = sum(1 for ok in results if not ok)
    print(f"\n{'=' * 56}\n通过 {len(results) - n_fail}/{len(results)}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
