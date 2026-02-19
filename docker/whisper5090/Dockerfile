FROM onerahmet/openai-whisper-asr-webservice:latest-gpu

# Rebuild torch stack for CUDA 12.8 (RTX 50xx compatibility).
RUN /app/.venv/bin/pip uninstall -y torch torchvision torchaudio \
    && /app/.venv/bin/pip install --index-url https://download.pytorch.org/whl/cu128 \
       "torch==2.8.*" "torchaudio==2.8.*" "torchvision==0.23.*"

