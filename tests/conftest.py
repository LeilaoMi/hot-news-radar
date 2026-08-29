"""pytest 公共配置。

项目根加入 sys.path，使 `import trendradar` 在 tests/ 下可用
（pytest 的 prepend importmode 只会把 tests/ 本身加入路径）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
