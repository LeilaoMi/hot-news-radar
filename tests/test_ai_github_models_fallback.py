from trendradar.ai.client import AIClient


def test_no_token_no_github_fallback(monkeypatch):
    monkeypatch.delenv("GITHUB_MODELS_TOKEN", raising=False)
    c = AIClient({"MODEL": "openai/glm-4-flash", "FALLBACK_MODELS": ["openai/x"]})
    assert c.fallback_models == ["openai/x"]


def test_token_appends_github_models(monkeypatch):
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "t")
    monkeypatch.delenv("GITHUB_MODELS_MODEL", raising=False)
    c = AIClient({"MODEL": "openai/glm-4-flash"})
    assert c.fallback_models == [
        {"model": "openai/openai/gpt-4.1-mini", "api_base": "https://models.github.ai/inference", "api_key": "t"}
    ]
    assert c._attempts(0) == 2
