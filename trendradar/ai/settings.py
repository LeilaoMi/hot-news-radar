# coding=utf-8
"""AI 子系统的配置解析（按功能解析模型、温度、超时、缓存路径等）。

为什么放在 AI 模块里而不是 core/loader.py：loader 只产出一部分 AI 键
（如 ``ai_translation.batch_size`` / ``cache_enabled`` 曾被漏读），
各 AI 服务在构造时通过本模块**直接读取 config.yaml 的 ai_* 段与环境变量**，
调用方（__main__ / dispatcher）无需任何改动即可生效。

模型优先级（以 analysis 为例，translation / dedup / filter / daily 同理）::

    env AI_ANALYSIS_MODEL > env AI_MODEL > yaml ai.analysis_model > yaml ai.model

daily（daily 模式下的 AI 分析，产出 AI 日报）额外回退到 analysis 的配置::

    env AI_DAILY_MODEL > env AI_ANALYSIS_MODEL > env AI_MODEL
      > yaml ai.daily_model > yaml ai.analysis_model > yaml ai.model

端点 / Key：env AI_<FEATURE>_API_BASE / AI_<FEATURE>_API_KEY > yaml ai.<feature>_api_base
/ ai.<feature>_api_key > 全局 API_BASE / API_KEY（env AI_API_BASE > yaml ai.api_base）。

温度 / max_tokens / timeout：env AI_<FEATURE>_TEMPERATURE（等）> yaml ai_<feature>.temperature
（等）> 功能默认值（翻译 0.2、去重/筛选 0.1）> 全局 ai.temperature（等）。
"""

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from trendradar.core.logger import get_logger

log = get_logger(__name__)

# 功能名 → (env 前缀, yaml 功能段名, yaml ai 段里的模型键)
FEATURES = {
    "analysis": ("AI_ANALYSIS", "ai_analysis", "analysis_model"),
    "translation": ("AI_TRANSLATION", "ai_translation", "translation_model"),
    "dedup": ("AI_DEDUP", "ai_dedup", "dedup_model"),
    "filter": ("AI_FILTER", "ai_filter", "filter_model"),
    "daily": ("AI_DAILY", "ai_daily", "daily_model"),
}

# 确定性任务默认低温（GLM temperature 取值 [0, 1]，1.0 为随机性上限）
_FEATURE_DEFAULT_TEMPERATURE = {
    "translation": 0.2,
    "dedup": 0.1,
    "filter": 0.1,
}

DEFAULT_CACHE_DB_PATH = "output/ai_cache.db"
DEFAULT_TOTAL_BUDGET_SECONDS = 480.0
DEFAULT_CACHE_TTL_DAYS = 7.0

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}

_yaml_lock = threading.Lock()
_yaml_cache: Dict[str, Dict] = {}
_logged_sources: set = set()


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

def parse_bool(value: Any) -> Optional[bool]:
    """三态布尔解析：识别 {1,true,yes,on} / {0,false,no,off}，其它返回 None。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def env_bool(key: str) -> Optional[bool]:
    """环境变量布尔值；未设置返回 None，无法识别时打 warning 并返回 None（回退到 yaml）。"""
    raw = os.environ.get(key, "")
    if not raw.strip():
        return None
    parsed = parse_bool(raw)
    if parsed is None:
        log.warning(f"[AI配置] 环境变量 {key}={raw!r} 无法识别为布尔值，已忽略（可用 true/false/1/0/yes/no/on/off）")
    return parsed


def env_str(key: str) -> str:
    return os.environ.get(key, "").strip()


def env_float(key: str) -> Optional[float]:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning(f"[AI配置] 环境变量 {key}={raw!r} 不是数字，已忽略")
        return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    f = _to_float(value)
    return int(f) if f is not None else None


# ---------------------------------------------------------------------------
# 原始 yaml（loader 未产出的 AI 键从这里读）
# ---------------------------------------------------------------------------

def config_path() -> str:
    return os.environ.get("CONFIG_PATH", "config/config.yaml")


def raw_config() -> Dict:
    """读取原始 config.yaml（按路径缓存）；任何失败返回 {}。"""
    path = config_path()
    with _yaml_lock:
        if path in _yaml_cache:
            return _yaml_cache[path]
        data: Dict = {}
        try:
            p = Path(path)
            if p.exists():
                import yaml
                loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
        except Exception as e:  # 配置读失败不能拖垮 AI 主流程
            log.warning(f"[AI配置] 读取 {path} 失败，使用默认值: {type(e).__name__}: {e}")
        _yaml_cache[path] = data
        return data


def raw_section(name: str) -> Dict:
    sec = raw_config().get(name)
    return sec if isinstance(sec, dict) else {}


def reset_cache() -> None:
    """测试用：清空 yaml 缓存与已打印的来源日志。"""
    with _yaml_lock:
        _yaml_cache.clear()
    _logged_sources.clear()


# ---------------------------------------------------------------------------
# 模型 / 端点优先级
# ---------------------------------------------------------------------------

def _cfg_get(ai_config: Dict, *keys: str) -> Any:
    for k in keys:
        v = ai_config.get(k)
        if v not in (None, ""):
            return v
    return None


def resolve_model(ai_config: Dict, feature: str) -> Tuple[str, str]:
    """按优先级解析功能模型，返回 (model, 来源描述)。"""
    env_prefix, _section, yaml_key = FEATURES[feature]
    ai_yaml = raw_section("ai")

    chain = [env_prefix]
    if feature == "daily":
        chain.append("AI_ANALYSIS")
    for prefix in chain:
        v = env_str(f"{prefix}_MODEL")
        if v:
            return v, f"env {prefix}_MODEL"
    v = env_str("AI_MODEL")
    if v:
        return v, "env AI_MODEL"

    yaml_keys = [yaml_key] + (["analysis_model"] if feature == "daily" else [])
    for k in yaml_keys:
        # loader 产出的大写键（如 ANALYSIS_MODEL）在 env 未设置时就是 yaml 值
        v = _cfg_get(ai_config, k.upper(), k)
        if v:
            return str(v), f"yaml ai.{k}"
        v = ai_yaml.get(k)
        if v:
            return str(v), f"yaml ai.{k}"

    v = _cfg_get(ai_config, "MODEL", "model") or ai_yaml.get("model")
    if v:
        return str(v), "yaml ai.model"
    return "", "未配置"


def _resolve_endpoint(ai_config: Dict, feature: str, kind: str) -> Tuple[str, str]:
    """kind = API_BASE / API_KEY。"""
    env_prefix = FEATURES[feature][0]
    v = env_str(f"{env_prefix}_{kind}")
    if v:
        return v, f"env {env_prefix}_{kind}"
    yaml_key = f"{feature}_{kind.lower()}"
    v = raw_section("ai").get(yaml_key)
    if v:
        return str(v), f"yaml ai.{yaml_key}"
    v = env_str(f"AI_{kind}")
    if v:
        return v, f"env AI_{kind}"
    v = _cfg_get(ai_config, kind, kind.lower())
    if v:
        return str(v), f"yaml ai.{kind.lower()}"
    return "", "未配置"


def _feature_param(feature: str, name: str, cast) -> Optional[Any]:
    env_prefix, section, _ = FEATURES[feature]
    raw = env_str(f"{env_prefix}_{name.upper()}")
    if raw:
        v = cast(raw)
        if v is not None:
            return v
    return cast(raw_section(section).get(name))


def cache_db_path(ai_config: Optional[Dict] = None) -> str:
    """AI 缓存库路径：env AI_CACHE_DB > ai.CACHE_DB_PATH > yaml ai.cache_db_path > output/ai_cache.db。"""
    v = env_str("AI_CACHE_DB")
    if v:
        return v
    if ai_config:
        v = _cfg_get(ai_config, "CACHE_DB_PATH", "cache_db_path")
        if v:
            return str(v)
    v = raw_section("ai").get("cache_db_path")
    return str(v) if v else DEFAULT_CACHE_DB_PATH


def cache_ttl_seconds() -> float:
    """缓存 TTL：env AI_CACHE_TTL_DAYS > yaml ai.cache_ttl_days > 7 天（<=0 表示永不过期）。"""
    v = env_float("AI_CACHE_TTL_DAYS")
    if v is None:
        v = _to_float(raw_section("ai").get("cache_ttl_days"))
    if v is None:
        v = DEFAULT_CACHE_TTL_DAYS
    return v * 86400.0 if v > 0 else 0.0


def feature_ai_config(ai_config: Dict, feature: str) -> Dict:
    """返回某功能专用的 AI 客户端配置副本（模型/端点/温度/超时/缓存路径均已按优先级解析）。"""
    cfg = dict(ai_config or {})
    model, model_src = resolve_model(cfg, feature)
    base, base_src = _resolve_endpoint(cfg, feature, "API_BASE")
    key, _key_src = _resolve_endpoint(cfg, feature, "API_KEY")
    if model:
        cfg["MODEL"] = model
    cfg["API_BASE"] = base
    if key:
        cfg["API_KEY"] = key

    temp = _feature_param(feature, "temperature", _to_float)
    if temp is None:
        temp = _FEATURE_DEFAULT_TEMPERATURE.get(feature)
    if temp is not None:
        cfg["TEMPERATURE"] = temp
    max_tokens = _feature_param(feature, "max_tokens", _to_int)
    if max_tokens is not None:
        cfg["MAX_TOKENS"] = max_tokens
    timeout = _feature_param(feature, "timeout", _to_float)
    if timeout is not None and timeout > 0:
        cfg["TIMEOUT"] = timeout

    if "EXTRA_PARAMS" not in cfg:
        extra = raw_section("ai").get("extra_params")
        if isinstance(extra, dict):
            cfg["EXTRA_PARAMS"] = extra
    cfg["CACHE_DB_PATH"] = cache_db_path(cfg)
    cfg["FEATURE"] = feature
    cfg["MODEL_SOURCE"] = model_src
    cfg["API_BASE_SOURCE"] = base_src

    sig = (feature, model, model_src, base_src)
    if sig not in _logged_sources:
        _logged_sources.add(sig)
        log.info(f"[AI配置] {feature}: 模型 {model or '-'}（来源 {model_src}），端点来源 {base_src}")
        if model_src.startswith("env") and base_src.startswith("yaml") and base:
            log.warning(
                f"[AI配置] {feature}: 模型来自环境变量但端点来自 yaml（{base_src}），"
                "如模型与端点不属于同一服务商请同时设置 AI_API_BASE"
            )
    return cfg
