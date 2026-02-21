# Troubleshooting

## `/chronicle_reprocess_last` fails with DNS/host error

Symptom:

- `Cannot connect to host whisper_vllm:8000` or similar

Cause:

- backend config mismatch (`WHISPER_API_STYLE` / `BOT_WHISPER_BASE_URL` / `WHISPER_ASR_PATH`)

Fix:

- for `asr`: `http://whisper:9000` + `/asr`
- for `openai`: `http://whisper_vllm:8000` + `/v1/audio/transcriptions`
- use `scripts/switch_asr_backend.py`

## Bot says app is thinking / no response on start

- check `docker compose logs -f bot`
- verify Discord intents and channel permissions
- verify warmup is not failing repeatedly

## Voice decode errors (`corrupted stream`, opus decode)

- occasional packets can be noisy; frequent errors indicate unstable voice path
- check reconnect counters via `/chronicle_status`
- test with `RECORDING_ROTATION_SECONDS=0`

## No speech detected

- verify source audio quality
- test with `AUDIO_NORMALIZE=true`
- test with/without `AUDIO_VAD_ENABLED`
- compare ASR backends with `scripts/benchmark_whisper.py`

## Whisper 500 errors

- inspect Whisper container logs
- validate model path / engine configuration
- for custom CT2 models, confirm required files and compatibility

## vLLM first start is very slow

- expected due model load + compile/warmup
- keep container running between sessions
- enable bot warmup (`WHISPER_WARMUP_ON_START=true`)

## Forbidden / Missing Access in Discord post-processing

- ensure bot has access to target text channel
- verify channel binding with `/chronicle_setup_channels`

