from chronicle_keeper.config import config_doctor_issues, load_settings


def test_load_settings_prefers_llm_aliases(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm-alias:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "alias-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.25")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("LLM_WARMUP_ON_START", "true")

    settings = load_settings()
    assert settings.llm_base_url == "http://llm-alias:1234/v1"
    assert settings.llm_model == "alias-model"
    assert settings.llm_temperature == 0.25
    assert settings.llm_max_tokens == 2048
    assert settings.llm_warmup_on_start is True


def test_load_settings_defaults(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("MODEL_RUNNER_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_RUNNER_MODEL", raising=False)
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LLM_WARMUP_ON_START", "false")
    monkeypatch.setenv("WHISPER_API_STYLE", "asr")
    monkeypatch.setenv("WHISPER_OPENAI_MODEL", "openai/whisper-large-v3-turbo")
    monkeypatch.setenv("WHISPER_OPENAI_TEMPERATURE", "0.0")
    monkeypatch.setenv("WHISPER_OPENAI_PROMPT", "")
    monkeypatch.setenv("WHISPER_WARMUP_ON_START", "false")
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
    assert settings.whisper_api_style == "asr"
    assert settings.whisper_openai_model == "openai/whisper-large-v3-turbo"
    assert settings.whisper_fallback_enabled is False
    assert settings.whisper_fallback_base_url == ""
    assert settings.whisper_fallback_asr_path == "/asr"
    assert settings.llm_base_url.startswith("http://127.0.0.1:")
    assert settings.llm_warmup_on_start is False


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


def test_load_settings_whisper_openai_style(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("WHISPER_API_STYLE", "openai")
    monkeypatch.setenv("WHISPER_ASR_PATH", "/v1/audio/transcriptions")
    monkeypatch.setenv("WHISPER_OPENAI_MODEL", "openai/whisper-large-v3-turbo")
    monkeypatch.setenv("WHISPER_OPENAI_TEMPERATURE", "0")
    monkeypatch.setenv("WHISPER_OPENAI_PROMPT", "names: Mykola, Aria")
    monkeypatch.setenv("WHISPER_WARMUP_ON_START", "true")

    settings = load_settings()
    assert settings.whisper_api_style == "openai"
    assert settings.whisper_asr_path == "/v1/audio/transcriptions"
    assert settings.whisper_openai_model == "openai/whisper-large-v3-turbo"
    assert settings.whisper_openai_temperature == 0.0
    assert settings.whisper_openai_prompt == "names: Mykola, Aria"
    assert settings.whisper_warmup_on_start is True


def test_load_settings_corrects_whisper_style_path_mismatch(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("WHISPER_API_STYLE", "openai")
    monkeypatch.setenv("WHISPER_ASR_PATH", "/asr")
    settings = load_settings()
    assert settings.whisper_api_style == "openai"
    assert settings.whisper_asr_path == "/v1/audio/transcriptions"


def test_load_settings_whisper_fallback_values(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("WHISPER_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("WHISPER_FALLBACK_BASE_URL", "http://fallback:9000")
    monkeypatch.setenv("WHISPER_FALLBACK_API_STYLE", "asr")
    monkeypatch.setenv("WHISPER_FALLBACK_ASR_PATH", "/asr")
    settings = load_settings()
    assert settings.whisper_fallback_enabled is True
    assert settings.whisper_fallback_base_url == "http://fallback:9000"
    assert settings.whisper_fallback_api_style == "asr"
    assert settings.whisper_fallback_asr_path == "/asr"


def test_config_doctor_detects_same_primary_and_fallback(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("WHISPER_BASE_URL", "http://same:9000")
    monkeypatch.setenv("WHISPER_API_STYLE", "asr")
    monkeypatch.setenv("WHISPER_ASR_PATH", "/asr")
    monkeypatch.setenv("WHISPER_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("WHISPER_FALLBACK_BASE_URL", "http://same:9000")
    monkeypatch.setenv("WHISPER_FALLBACK_API_STYLE", "asr")
    monkeypatch.setenv("WHISPER_FALLBACK_ASR_PATH", "/asr")
    settings = load_settings()
    issues = config_doctor_issues(settings)
    assert any("fallback target matches primary" in issue.lower() for issue in issues)
