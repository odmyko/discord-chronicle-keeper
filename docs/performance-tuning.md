# Performance Tuning

## Goals

Choose profile by priority:

- maximum transcript quality
- fastest turnaround
- balanced mode for long sessions

## Qwen3-ASR knobs

- `QWEN3_ASR_MODEL`:
  - `Qwen/Qwen3-ASR-1.7B` is default baseline
- `QWEN3_ASR_DTYPE`:
  - `float16` is usually best stable GPU default on Windows/NVIDIA
  - `bfloat16` can be tested if stack supports it well
- `QWEN3_ASR_ATTN_IMPLEMENTATION`:
  - `auto` default
  - `sdpa` for predictable compatibility
  - `flash_attention_2` for faster inference when FA2 is installed
- `QWEN3_ASR_MAX_INFERENCE_BATCH_SIZE`:
  - raise on high-VRAM GPUs for throughput
  - lower if you see OOM or unstable latency
- `QWEN3_ASR_MAX_NEW_TOKENS`:
  - keep high enough to avoid transcript truncation

## Audio preprocessing knobs

- `AUDIO_NORMALIZE=true` can improve hard/noisy speech
- `AUDIO_VAD_ENABLED=true` trims long silence but may shift timing
- `AUDIO_DUAL_PIPELINE_ENABLED=true` runs extra fallback ASR pass for robustness on difficult audio

Speech-oriented compression profile:

- `AUDIO_TARGET_CHANNELS=1`
- `AUDIO_TARGET_SAMPLE_RATE=16000`
- `AUDIO_MP3_VBR_QUALITY=5`

## LLM summary speed/quality

- `LLM_WARMUP_ON_START=true` reduces first summary latency
- lower `LLM_TEMPERATURE` for more stable structure
- tune `LLM_MAX_TOKENS` for long sessions

## Operational checks

- smoke test:
  - `python scripts/smoke_e2e.py`
- one-file Qwen env benchmark:
  - `python scripts/benchmark_qwen_envs.py --audio <path>`
- runtime counters:
  - `/chronicle_status`
