# Operations Runbook

## Start

Classic ASR profile:

```bash
docker compose --profile asr up -d --build
```

vLLM profile:

```bash
docker compose --profile vllm up -d --build
```

## Switch backend safely

Recommended:

```bash
python scripts/switch_asr_backend.py --backend asr --up
python scripts/switch_asr_backend.py --backend vllm --up
```

Manual:

1. `docker compose stop whisper whisper_vllm bot`
2. Update `.env` backend keys
3. `docker compose --profile <asr|vllm> up -d --build`

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

```bash
docker compose logs -f bot
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
