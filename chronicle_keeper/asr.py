from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


class ASRClient(Protocol):
    async def transcribe_file(self, audio_path: Path) -> str: ...

    async def transcribe_file_detailed(self, audio_path: Path) -> TranscriptResult: ...

    async def warmup(self) -> tuple[bool, str]: ...


def create_asr_client(settings: Settings) -> ASRClient:
    if settings.asr_backend == "vibevoice_asr":
        from .vibevoice_asr_client import VibeVoiceASRClient

        return VibeVoiceASRClient(settings)

    from .qwen_asr_client import Qwen3ASRClient

    return Qwen3ASRClient(settings)
