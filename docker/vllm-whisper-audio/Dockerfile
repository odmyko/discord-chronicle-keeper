FROM vllm/vllm-openai:latest

# Enable audio/transcription support for OpenAI-compatible /v1/audio/transcriptions.
# vLLM requires optional audio extras (librosa/soundfile stack).
RUN python3 -m pip install --no-cache-dir "vllm[audio]" librosa soundfile
