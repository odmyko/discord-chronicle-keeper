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
        text = await self._transcribe_once(audio_path, language=self._language)
        if text:
            return text

        # Fallback: retry with auto language if configured language produced empty output.
        if self._language and self._language.lower() not in {"auto", "none"}:
            text = await self._transcribe_once(audio_path, language="")
            if text:
                return text
        return ""

    async def _transcribe_once(self, audio_path: Path, language: str) -> str:
        endpoint = f"{self._base_url}{self._asr_path}"
        params = {
            "task": self._task,
            "encode": str(self._encode).lower(),
            "output": "json",
        }
        if language:
            params["language"] = language

        form = aiohttp.FormData()
        with audio_path.open("rb") as fh:
            form.add_field(
                "audio_file",
                fh,
                filename=audio_path.name,
                content_type="audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav",
            )
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(endpoint, params=params, data=form, timeout=300) as resp:
                        body = await resp.text()
                        if resp.status >= 400:
                            raise RuntimeError(f"Whisper error {resp.status}: {body[:400]}")
                except TimeoutError:
                    raise RuntimeError("Whisper request timed out (300s).")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body.strip()

        # Standard whisper JSON shape
        text = payload.get("text", "")
        if isinstance(text, str) and text.strip():
            return text.strip()

        # Some variants return only segments with per-segment text.
        segments = payload.get("segments")
        if isinstance(segments, list):
            parts: list[str] = []
            for seg in segments:
                if isinstance(seg, dict):
                    seg_text = seg.get("text")
                    if isinstance(seg_text, str) and seg_text.strip():
                        parts.append(seg_text.strip())
            if parts:
                return " ".join(parts).strip()

        if isinstance(text, str):
            return text.strip()
        return str(text).strip()
