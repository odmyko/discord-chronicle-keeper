FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg is required for WAV -> MP3 compression.
# git is required because py-cord is installed from a git URL.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY chronicle_keeper ./chronicle_keeper
COPY README.md ./

CMD ["python", "-m", "chronicle_keeper.bot"]

