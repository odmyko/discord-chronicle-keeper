# Operations Runbook

## Choose Runtime Mode

Pick one LLM mode first:

1. `bot` + LM Studio (default service)
2. `bot_docker_llm` + Docker model runner (`docker-llm` profile)

Then pick one ASR backend:

1. Classic Whisper `/asr` (`asr` profile, service `whisper`)
2. vLLM OpenAI transcription (`vllm` profile, service `whisper_vllm`)

## Start

LM Studio + classic ASR:

```bash
docker compose --profile asr up -d --build --remove-orphans
```

LM Studio + vLLM ASR:

```bash
docker compose --profile vllm up -d --build --remove-orphans
```

Docker model runner + classic ASR:

```bash
docker compose --profile docker-llm --profile asr up -d --build --remove-orphans --scale bot=0
```

Docker model runner + vLLM ASR:

```bash
docker compose --profile docker-llm --profile vllm up -d --build --remove-orphans --scale bot=0
```

## Switch backend safely

Recommended:

```bash
python scripts/switch_asr_backend.py --backend asr --up
python scripts/switch_asr_backend.py --backend vllm --up
```

Manual:

1. Stop old backend service:
   - LM Studio mode: `docker compose stop whisper whisper_vllm bot`
   - Docker LLM mode: `docker compose stop whisper whisper_vllm bot_docker_llm`
2. Update `.env` backend keys
3. Start target profile (commands from **Start** section)

## Pre-session health check

```bash
python scripts/smoke_e2e.py
```

Optional target file:

```bash
python scripts/smoke_e2e.py --audio data/sessions/<guild>/<session>/audio/mixed_session.mp3
```

## Session flow in Discord

1. `/chronicle_setup_channels`
2. `/chronicle_start`
3. `/chronicle_stop`

Operational commands:

- `/chronicle_status`
- `/chronicle_reconnect`
- `/chronicle_reprocess_last`

## Logs

LM Studio mode:

```bash
docker compose logs -f bot
docker compose logs -f whisper
docker compose logs -f whisper_vllm
```

Docker LLM mode:

```bash
docker compose logs -f bot_docker_llm
docker compose logs -f whisper
docker compose logs -f whisper_vllm
```

## Cleanup and retention

- automatic cleanup via env flags
- manual: `/chronicle_cleanup_now`
- destructive purge commands require `ALLOW_PURGE_COMMANDS=true`

## Backup

Backup:

```bash
tar -czf chronicle-backup-$(date +%Y%m%d_%H%M%S).tar.gz data/guild_settings.json data/sessions
```

Restore:

```bash
tar -xzf chronicle-backup-YYYYMMDD_HHMMSS.tar.gz
```
