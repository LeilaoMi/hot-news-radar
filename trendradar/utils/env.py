# coding=utf-8
"""环境变量解析的统一入口。

之前布尔环境变量在不同模块里各有一套规则（loader 只认 true/1、logger 认 1/true/yes、
ai/cache 认「非 0/false/no 即真」），同一个 secret 写成 ``yes`` 会在不同地方得到不同结果。
所有新代码统一走这里。
"""

import os
from typing import Optional

TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})
FALSE_VALUES = frozenset({"0", "false", "no", "off", "n"})


def parse_bool(value) -> Optional[bool]:
    """把字符串解析成布尔值；空值返回 None，无法识别的值返回 False 并保留告警给调用方。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in TRUE_VALUES:
        return True
    return False


def env_bool(key: str, default: Optional[bool] = None) -> Optional[bool]:
    """读取布尔环境变量；未设置/空值时返回 default。"""
    parsed = parse_bool(os.environ.get(key))
    return default if parsed is None else parsed


def env_float(key: str) -> Optional[float]:
    """读取浮点环境变量；未设置或非法时返回 None。"""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"[配置] 环境变量 {key}={raw!r} 不是合法数字，已忽略")
        return None
