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
    monkeypatch.setenv("RECORDING_ROTATION_SECONDS", "1800")
    monkeypatch.setenv("PROCESSING_TIMEOUT_SECONDS", "7200")
    monkeypatch.setenv("AUDIO_TARGET_SAMPLE_RATE", "0")
    monkeypatch.setenv("AUDIO_TARGET_CHANNELS", "0")
    monkeypatch.setenv("AUDIO_MP3_VBR_QUALITY", "4")
    monkeypatch.setenv("AUDIO_DUAL_PIPELINE_ENABLED", "false")
    monkeypatch.setenv("AUDIO_VAD_ENABLED", "false")
    monkeypatch.setenv("PUBLISH_PER_SPEAKER_AUDIO", "false")
    monkeypatch.setenv("VOICE_DECODE_BURST_WINDOW_SECONDS", "15")
    monkeypatch.setenv("VOICE_DECODE_BURST_THRESHOLD", "8")
    monkeypatch.setenv("VOICE_DECODE_BURST_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("ASR_LANGUAGE", "ru")

    settings = load_settings()
    assert settings.asr_backend == "qwen3_asr"
    assert settings.asr_language == "ru"
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
    assert settings.voice_decode_burst_window_seconds == 15
    assert settings.voice_decode_burst_threshold == 8
    assert settings.voice_decode_burst_cooldown_seconds == 60


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


def test_load_settings_qwen3_asr_backend(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("QWEN3_ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")
    monkeypatch.setenv("QWEN3_ASR_DTYPE", "bfloat16")
    monkeypatch.setenv("QWEN3_ASR_ATTN_IMPLEMENTATION", "flash_attention_2")
    monkeypatch.setenv("QWEN3_ASR_MAX_NEW_TOKENS", "2048")
    monkeypatch.setenv("QWEN3_ASR_MAX_INFERENCE_BATCH_SIZE", "16")
    monkeypatch.setenv("QWEN3_ASR_WARMUP_ON_START", "true")

    settings = load_settings()
    assert settings.asr_backend == "qwen3_asr"
    assert settings.qwen_asr_model == "Qwen/Qwen3-ASR-1.7B"
    assert settings.qwen_asr_dtype == "bfloat16"
    assert settings.qwen_asr_attn_implementation == "flash_attention_2"
    assert settings.qwen_asr_max_new_tokens == 2048
    assert settings.qwen_asr_max_inference_batch_size == 16
    assert settings.qwen_asr_warmup_on_start is True


def test_config_doctor_detects_invalid_qwen_values(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("QWEN3_ASR_DTYPE", "bad")
    monkeypatch.setenv("QWEN3_ASR_ATTN_IMPLEMENTATION", "bad")

    settings = load_settings()
    issues = config_doctor_issues(settings)
    assert any("QWEN3_ASR_DTYPE" in issue for issue in issues)
    assert any("QWEN3_ASR_ATTN_IMPLEMENTATION" in issue for issue in issues)
