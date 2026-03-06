# Troubleshooting

## `/chronicle_reprocess_last` fails to connect to ASR

Symptoms:

- timeout / disconnect during ASR stage
- no progress after `asr_start` log line

Checks:

- verify GPU is available in the runtime env
- verify `QWEN3_ASR_MODEL` is downloaded and loadable
- lower `QWEN3_ASR_MAX_INFERENCE_BATCH_SIZE` if memory pressure is high
- use `QWEN3_ASR_DTYPE=float16` on NVIDIA/Windows by default

## Bot hangs on startup or slash commands time out

- check `docker compose logs -f bot` (or `bot_docker_llm`)
- verify Discord intents and channel permissions
- verify `DISCORD_BOT_TOKEN` is valid
- if `LLM_WARMUP_ON_START=true`, verify LLM endpoint is reachable

## Voice decode errors (`corrupted stream`, opus decode)

- occasional errors are expected on network jitter
- repeated bursts indicate unstable voice path
- inspect `/chronicle_status` reconnect/decode-burst counters
- tune:
  - `VOICE_DECODE_BURST_WINDOW_SECONDS`
  - `VOICE_DECODE_BURST_THRESHOLD`
  - `VOICE_DECODE_BURST_COOLDOWN_SECONDS`

## No speech detected / weak transcript

- check raw audio quality in `data/sessions/.../audio`
- try `AUDIO_NORMALIZE=true`
- compare with `AUDIO_VAD_ENABLED=false` vs `true`
- if segments are noisy or empty, try `AUDIO_DUAL_PIPELINE_ENABLED=true` as fallback mode

## LLM summary quality degraded

- verify correct `LLM_MODEL`
- lower `LLM_TEMPERATURE` (`0.1-0.2` typical)
- tune `LLM_MAX_TOKENS` for your model/context window
- if off-topic sessions are common, enable context gate:
  - `SUMMARY_CONTEXT_RELEVANCE_GATE=true`
  - `SUMMARY_CONTEXT_MIN_RELEVANCE=0.40` (adjust as needed)

## Discord upload errors for audio artifacts

- free-tier Discord upload limits may reject large `mixed_session.mp3`
- artifacts remain on disk even when upload fails
- increase compression (higher `AUDIO_MP3_VBR_QUALITY` value) if needed
