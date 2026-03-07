from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import wave
from pathlib import Path

from .asr import TranscriptResult, TranscriptSegment
from .config import Settings

logger = logging.getLogger(__name__)


class VibeVoiceASRClient:
    def __init__(self, settings: Settings) -> None:
        self._python = settings.vibevoice_python
        self._script = settings.vibevoice_script
        self._model = settings.vibevoice_model
        self._dtype = settings.vibevoice_dtype
        self._language = settings.asr_language
        self._max_new_tokens = settings.vibevoice_max_new_tokens
        self._warmup_on_start = settings.vibevoice_warmup_on_start

    async def transcribe_file(self, audio_path: Path) -> str:
        result = await self.transcribe_file_detailed(audio_path)
        return result.text

    async def transcribe_file_detailed(self, audio_path: Path) -> TranscriptResult:
        payload = await asyncio.to_thread(self._run_cli_sync, audio_path)
        text = str(payload.get("text", "") or "").strip()
        segments: list[TranscriptSegment] = []
        for row in payload.get("segments", []) or []:
            try:
                start = float(row.get("start", 0.0))
                end = float(row.get("end", 0.0))
                seg_text = str(row.get("text", "") or "").strip()
                if seg_text:
                    segments.append(
                        TranscriptSegment(start=start, end=end, text=seg_text)
                    )
            except Exception:
                continue
        return TranscriptResult(text=text, segments=segments)

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
            await self.transcribe_file_detailed(Path(tmp_path))
            return True, "ok"
        except Exception as exc:
            return False, str(exc)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _run_cli_sync(self, audio_path: Path) -> dict:
        script_path = Path(self._script)
        if not script_path.exists():
            raise RuntimeError(f"VibeVoice script not found: {script_path}")

        cmd = [
            self._python,
            str(script_path),
            "--audio",
            str(audio_path),
            "--model",
            self._model,
            "--dtype",
            self._dtype,
            "--max-new-tokens",
            str(self._max_new_tokens),
            "--json",
        ]
        if self._language:
            cmd.extend(["--language", self._language])

        logger.info(
            "[vibevoice-asr] run model=%s audio=%s python=%s",
            self._model,
            audio_path.name,
            self._python,
        )
        completed = __import__("subprocess").run(
            cmd,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            details = stderr or stdout or f"exit code {completed.returncode}"
            raise RuntimeError(f"VibeVoice subprocess failed: {details}")

        raw = (completed.stdout or "").strip()
        if not raw:
            raise RuntimeError("VibeVoice subprocess returned empty output")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid JSON from VibeVoice subprocess: {exc}"
            ) from exc
