# coding=utf-8
"""AI 配置加载回归测试。

锁定 P0-1 修复：yaml ``ai.analysis_model`` / env ``AI_ANALYSIS_MODEL``
必须透传到 ``config["AI"]["ANALYSIS_MODEL"]``。
该键曾因 loader 未透传而静默丢失，导致分析模型配置永不生效。
"""

from trendradar.core.loader import _load_ai_config


def test_analysis_model_from_yaml():
    cfg = _load_ai_config({"ai": {"model": "glm-4.6", "analysis_model": "glm-4.7-flash"}})
    assert cfg["MODEL"] == "glm-4.6"
    assert cfg["ANALYSIS_MODEL"] == "glm-4.7-flash"


def test_analysis_model_env_override(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_MODEL", "env-model")
    cfg = _load_ai_config({"ai": {"analysis_model": "yaml-model"}})
    assert cfg["ANALYSIS_MODEL"] == "env-model"


def test_analysis_model_default_empty():
    """未配置时为空串（沿用全局 MODEL 的语义约定）。"""
    cfg = _load_ai_config({"ai": {"model": "glm-4.6"}})
    assert cfg["ANALYSIS_MODEL"] == ""


def test_analysis_model_env_only(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_MODEL", "env-only-model")
    cfg = _load_ai_config({"ai": {}})
    assert cfg["ANALYSIS_MODEL"] == "env-only-model"


def test_core_keys_still_load():
    """透传新键不得破坏既有键。"""
    cfg = _load_ai_config(
        {
            "ai": {
                "model": "glm-4.6",
                "api_key": "k",
                "api_base": "https://example.com",
                "timeout": 60,
                "temperature": 0.5,
                "max_tokens": 1000,
            }
        }
    )
    assert cfg["MODEL"] == "glm-4.6"
    assert cfg["API_KEY"] == "k"
    assert cfg["API_BASE"] == "https://example.com"
    assert cfg["TIMEOUT"] == 60
    assert cfg["TEMPERATURE"] == 0.5
    assert cfg["MAX_TOKENS"] == 1000
