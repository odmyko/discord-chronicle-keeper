from chronicle_keeper.config import load_settings


def test_load_settings_prefers_llm_aliases(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm-alias:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "alias-model")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://lmstudio:1234/v1")
    monkeypatch.setenv("LMSTUDIO_MODEL", "lmstudio-model")

    settings = load_settings()
    assert settings.lmstudio_base_url == "http://llm-alias:1234/v1"
    assert settings.lmstudio_model == "alias-model"


def test_load_settings_defaults(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("MODEL_RUNNER_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_RUNNER_MODEL", raising=False)
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)
    # Keep this test deterministic even when local .env exists.
    monkeypatch.setenv("SUMMARY_CHUNK_CHARS", "14000")
    monkeypatch.setenv("RECORDING_ROTATION_SECONDS", "1800")
    monkeypatch.setenv("PROCESSING_TIMEOUT_SECONDS", "7200")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")

    settings = load_settings()
    assert settings.summary_chunk_chars == 14000
    assert settings.recording_rotation_seconds == 1800
    assert settings.processing_timeout_seconds == 7200
    assert settings.lmstudio_base_url.startswith("http://127.0.0.1:")
