from chronicle_keeper.config import load_settings


def test_load_settings_prefers_llm_aliases(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm-alias:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "alias-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.25")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")

    settings = load_settings()
    assert settings.llm_base_url == "http://llm-alias:1234/v1"
    assert settings.llm_model == "alias-model"
    assert settings.llm_temperature == 0.25
    assert settings.llm_max_tokens == 2048


def test_load_settings_defaults(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("MODEL_RUNNER_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_RUNNER_MODEL", raising=False)
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("AUDIO_TARGET_SAMPLE_RATE", "0")
    monkeypatch.setenv("AUDIO_TARGET_CHANNELS", "0")
    monkeypatch.setenv("AUDIO_MP3_VBR_QUALITY", "4")
    monkeypatch.setenv("AUDIO_DUAL_PIPELINE_ENABLED", "false")
    monkeypatch.setenv("AUDIO_VAD_ENABLED", "false")
    monkeypatch.setenv("PUBLISH_PER_SPEAKER_AUDIO", "false")
    # Keep this test deterministic even when local .env exists.
    monkeypatch.setenv("SUMMARY_CHUNK_CHARS", "14000")
    monkeypatch.setenv("RECORDING_ROTATION_SECONDS", "1800")
    monkeypatch.setenv("PROCESSING_TIMEOUT_SECONDS", "7200")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1")

    settings = load_settings()
    assert settings.summary_chunk_chars == 14000
    assert settings.recording_rotation_seconds == 1800
    assert settings.processing_timeout_seconds == 7200
    assert settings.auto_cleanup_enabled is False
    assert settings.auto_cleanup_on_start is False
    assert settings.retention_days == 30
    assert settings.allow_purge_commands is False
    assert settings.audio_target_sample_rate == 0
    assert settings.audio_target_channels == 0
    assert settings.audio_mp3_vbr_quality == 4
    assert settings.audio_dual_pipeline_enabled is False
    assert settings.audio_vad_enabled is False
    assert settings.publish_per_speaker_audio is False
    assert settings.llm_base_url.startswith("http://127.0.0.1:")


def test_load_settings_audio_vad_enabled(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("AUDIO_VAD_ENABLED", "true")

    settings = load_settings()
    assert settings.audio_vad_enabled is True


def test_load_settings_audio_dual_pipeline_enabled(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("AUDIO_DUAL_PIPELINE_ENABLED", "true")

    settings = load_settings()
    assert settings.audio_dual_pipeline_enabled is True
