# Operations Runbook

## Choose Runtime Mode

LLM mode only (ASR is always local Qwen3-ASR in current build):

1. `bot` + LM Studio/external OpenAI-compatible endpoint (default)
2. `bot_docker_llm` + Docker model runner (`docker-llm` profile)
3. Optional `bot_sidecar` + `voice_sidecar` (`voice-sidecar` profile, for DAVE/E2EE channels)

## Start

LM Studio / external LLM endpoint:

```bash
docker compose up -d --build --remove-orphans
```

Docker model runner:

```bash
docker compose --profile docker-llm up -d --build --remove-orphans --scale bot=0
```

Sidecar API/runtime only (no bot):

```bash
docker compose --profile voice-sidecar up -d --build voice_sidecar
curl http://127.0.0.1:8081/health
```

Bot + sidecar together (DAVE/E2EE mode):

```bash
docker compose --profile voice-sidecar up -d --build --remove-orphans --scale bot=0
```
(This runs `bot_sidecar` + `voice_sidecar`; default `bot` is disabled to avoid duplicate Discord login.)

Bot + sidecar + GPU ASR:

```bash
docker compose --profile voice-sidecar-gpu up -d --build --remove-orphans --scale bot=0
```

Runtime note:
- With sidecar mode enabled, bot startup syncs active recording state from sidecar API
  (fallback: `data/runtime/voice_sidecar_state.json`) and restores rotation loop metadata.

## Stop

```bash
docker compose down
```

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
2. `/chronicle_campaign_create` (once per campaign)
3. `/chronicle_campaign_use`
4. `/chronicle_start`
5. `/chronicle_stop`

Operational commands:

- `/chronicle_status`
- `/chronicle_reconnect`
- `/chronicle_reprocess_last`
- `/chronicle_reprocess`

## Logs

LM Studio / external LLM mode:

```bash
docker compose logs -f bot
```

Docker model runner mode:

```bash
docker compose logs -f bot_docker_llm
```

Sidecar mode:

```bash
docker compose logs -f voice_sidecar
docker compose logs -f bot_sidecar
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
