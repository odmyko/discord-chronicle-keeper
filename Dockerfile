# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG TORCH_VERSION=""
ARG TORCH_WHL_INDEX_URL=""

# ffmpeg is required for WAV -> MP3 compression.
# git is required because py-cord is installed from a git URL.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$TORCH_VERSION" ] && [ -n "$TORCH_WHL_INDEX_URL" ]; then \
      pip install "torch==${TORCH_VERSION}" --index-url "${TORCH_WHL_INDEX_URL}"; \
    fi
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY chronicle_keeper ./chronicle_keeper
COPY README.md ./

CMD ["python", "-m", "chronicle_keeper.bot"]
