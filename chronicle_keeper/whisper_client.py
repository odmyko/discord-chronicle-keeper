from __future__ import annotations

import json
from pathlib import Path

import aiohttp

from .config import Settings


class WhisperClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.whisper_base_url
        self._asr_path = settings.whisper_asr_path
        self._language = settings.whisper_language
        self._task = settings.whisper_task
        self._encode = settings.whisper_encode

    async def transcribe_file(self, audio_path: Path) -> str:
        endpoint = f"{self._base_url}{self._asr_path}"
        params = {
            "task": self._task,
            "language": self._language,
            "encode": str(self._encode).lower(),
            "output": "json",
        }

        form = aiohttp.FormData()
        with audio_path.open("rb") as fh:
            form.add_field(
                "audio_file",
                fh,
                filename=audio_path.name,
                content_type="audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav",
            )
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, params=params, data=form, timeout=600) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        raise RuntimeError(f"Whisper error {resp.status}: {body[:400]}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body.strip()

        text = payload.get("text", "")
        if not isinstance(text, str):
            return str(text)
        return text.strip()

