from __future__ import annotations

from dataclasses import dataclass
import os
import json
from pathlib import Path
import tempfile
import wave

import aiohttp

from .config import Settings


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    segments: list[TranscriptSegment]


class WhisperClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.whisper_base_url
        api_style = (settings.whisper_api_style or "asr").strip().lower()
        self._api_style = api_style if api_style in {"asr", "openai"} else "asr"
        self._asr_path = settings.whisper_asr_path
        self._openai_model = settings.whisper_openai_model
        self._openai_temperature = settings.whisper_openai_temperature
        self._openai_prompt = settings.whisper_openai_prompt
        self._language = settings.whisper_language
        self._task = settings.whisper_task
        self._encode = settings.whisper_encode
        self._warmup_on_start = settings.whisper_warmup_on_start

    async def transcribe_file(self, audio_path: Path) -> str:
        result = await self.transcribe_file_detailed(audio_path)
        return result.text

    async def transcribe_file_detailed(self, audio_path: Path) -> TranscriptResult:
        text = await self._transcribe_once(audio_path, language=self._language)
        if text.text:
            return text

        # Fallback: retry with auto language if configured language produced empty output.
        if self._language and self._language.lower() not in {"auto", "none"}:
            text = await self._transcribe_once(audio_path, language="")
            if text.text:
                return text
        return TranscriptResult(text="", segments=[])

    async def _transcribe_once(self, audio_path: Path, language: str) -> TranscriptResult:
        endpoint = f"{self._base_url}{self._asr_path}"
        params: dict[str, str] = {}
        form = aiohttp.FormData()
        content_type = "audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav"

        if self._api_style == "openai":
            with audio_path.open("rb") as fh:
                form.add_field("file", fh, filename=audio_path.name, content_type=content_type)
                form.add_field("model", self._openai_model)
                form.add_field("response_format", "verbose_json")
                form.add_field("timestamp_granularities[]", "segment")
                form.add_field("temperature", str(self._openai_temperature))
                if self._task:
                    form.add_field("task", self._task)
                if self._openai_prompt:
                    form.add_field("prompt", self._openai_prompt)
                if language:
                    form.add_field("language", language)
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.post(endpoint, data=form, timeout=300) as resp:
                            body = await resp.text()
                            if resp.status >= 400:
                                raise RuntimeError(f"Whisper error {resp.status}: {body[:400]}")
                    except TimeoutError:
                        raise RuntimeError("Whisper request timed out (300s).")
        else:
            params = {
                "task": self._task,
                "encode": str(self._encode).lower(),
                "output": "json",
            }
            if language:
                params["language"] = language
            with audio_path.open("rb") as fh:
                form.add_field("audio_file", fh, filename=audio_path.name, content_type=content_type)
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
            text = body.strip()
            return TranscriptResult(text=text, segments=[])

        # Some variants return segments with per-segment timing/text.
        segments = payload.get("segments")
        parsed_segments: list[TranscriptSegment] = []
        if isinstance(segments, list):
            parts: list[str] = []
            for seg in segments:
                if isinstance(seg, dict):
                    seg_text = seg.get("text")
                    if isinstance(seg_text, str) and seg_text.strip():
                        cleaned = seg_text.strip()
                        parts.append(cleaned)
                        start = seg.get("start")
                        end = seg.get("end")
                        try:
                            start_f = float(start) if start is not None else 0.0
                            end_f = float(end) if end is not None else start_f
                        except (TypeError, ValueError):
                            start_f = 0.0
                            end_f = 0.0
                        parsed_segments.append(
                            TranscriptSegment(
                                start=max(0.0, start_f),
                                end=max(0.0, end_f),
                                text=cleaned,
                            )
                        )
            if parts:
                return TranscriptResult(
                    text=" ".join(parts).strip(),
                    segments=parsed_segments,
                )

        # Standard whisper JSON shape
        text = payload.get("text", "")
        if isinstance(text, str) and text.strip():
            return TranscriptResult(text=text.strip(), segments=parsed_segments)

        if isinstance(text, str):
            return TranscriptResult(text=text.strip(), segments=parsed_segments)
        return TranscriptResult(text=str(text).strip(), segments=parsed_segments)

    async def warmup(self) -> tuple[bool, str]:
        if not self._warmup_on_start:
            return False, "disabled"
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            with wave.open(tmp_path, "wb") as wavf:
                wavf.setnchannels(1)
                wavf.setsampwidth(2)
                wavf.setframerate(16000)
                wavf.writeframes(b"\x00\x00" * 16000)
            await self._transcribe_once(Path(tmp_path), language=self._language)
            return True, "ok"
        except Exception as exc:
            return False, str(exc)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
