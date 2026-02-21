# Performance Tuning

## Goals

Choose a profile based on priority:

- max quality transcript
- fastest turnaround
- balanced quality/speed

## ASR backend choices

### `whisper` (`asr` style)

- usually more complete transcript in this project setup
- good default for quality-sensitive sessions

### `whisper_vllm` (`openai` style)

- often faster after warm-up
- quality may differ even with similar model names due to different serving stack

## ASR quality knobs

- `WHISPER_OPENAI_TEMPERATURE=0.0` for deterministic output
- `WHISPER_OPENAI_PROMPT` with character/lore names
- fallback on low quality:
  - `WHISPER_FALLBACK_ON_LOW_QUALITY=true`
  - tune `WHISPER_LOW_QUALITY_MIN_CHARS`
  - tune `WHISPER_LOW_QUALITY_MIN_SEGMENTS`

## Audio preprocessing knobs

- `AUDIO_NORMALIZE=true` can help difficult speech
- `AUDIO_VAD_ENABLED=true` can reduce silence/noise but may alter timing
- `AUDIO_DUAL_PIPELINE_ENABLED=true` keeps better timeline while using processed text pass

Speech-oriented compression profile example:

- `AUDIO_TARGET_CHANNELS=1`
- `AUDIO_TARGET_SAMPLE_RATE=16000`
- `AUDIO_MP3_VBR_QUALITY=5`

## LLM summary speed/quality

- `LLM_WARMUP_ON_START=true` avoids first-call latency spikes
- lower `LLM_TEMPERATURE` for stable structured output
- adjust `LLM_MAX_TOKENS` only as needed to avoid unnecessary generation cost
- keep `SUMMARY_CHUNK_CHARS` tuned for long sessions

## Operational checks

- benchmark ASR:
  - `python scripts/benchmark_whisper.py ...`
- smoke e2e:
  - `python scripts/smoke_e2e.py`
- watch counters:
  - `/chronicle_status`
