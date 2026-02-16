from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, UTC
from io import BytesIO
from pathlib import Path
import re

import discord

from .lmstudio_client import LMStudioClient
from .whisper_client import WhisperClient


def sanitize_name(value: str) -> str:
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^A-Za-z0-9_.-]", "", value)
    return value or "unknown"


@dataclass
class SpeakerTranscript:
    user_id: int
    speaker_name: str
    audio_path: Path
    transcript: str


@dataclass
class SessionArtifacts:
    session_dir: Path
    full_transcript: str
    summary_markdown: str
    speaker_transcripts: list[SpeakerTranscript]


class SessionProcessor:
    def __init__(self, base_data_dir: Path, whisper: WhisperClient, lmstudio: LMStudioClient) -> None:
        self._base_data_dir = base_data_dir
        self._whisper = whisper
        self._lmstudio = lmstudio

    async def process_sink(
        self,
        guild: discord.Guild,
        sink: discord.sinks.Sink,
    ) -> SessionArtifacts:
        print(f"[processor] start guild={guild.id} tracks={len(sink.audio_data)}")
        now = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        session_dir = self._base_data_dir / "sessions" / str(guild.id) / now
        audio_dir = session_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        speaker_items: list[SpeakerTranscript] = []

        for user_id, audio_data in sink.audio_data.items():
            member = guild.get_member(int(user_id))
            speaker_name = member.display_name if member else f"user_{user_id}"
            base_name = f"{sanitize_name(speaker_name)}_{user_id}"
            wav_path = audio_dir / f"{base_name}.wav"

            file_obj = audio_data.file
            if isinstance(file_obj, BytesIO):
                file_obj.seek(0)
            elif hasattr(file_obj, "seek"):
                file_obj.seek(0)
            wav_path.write_bytes(file_obj.read())

            print(f"[processor] prepared audio speaker={speaker_name} user_id={user_id} file={wav_path.name}")
            compressed_path = await self._compress_audio(wav_path)
            print(f"[processor] transcribe start speaker={speaker_name} file={compressed_path.name}")
            transcript = await self._whisper.transcribe_file(compressed_path)
            print(f"[processor] transcribe done speaker={speaker_name} chars={len(transcript)}")
            speaker_items.append(
                SpeakerTranscript(
                    user_id=int(user_id),
                    speaker_name=speaker_name,
                    audio_path=compressed_path,
                    transcript=transcript,
                )
            )

        speaker_items.sort(key=lambda item: item.speaker_name.lower())
        full_transcript = self._build_transcript_markdown(speaker_items)
        (session_dir / "full_transcript.md").write_text(full_transcript, encoding="utf-8")

        print(f"[processor] lmstudio summarize start chars={len(full_transcript)}")
        summary_markdown = await self._lmstudio.generate_summary(full_transcript)
        (session_dir / "summary.md").write_text(summary_markdown, encoding="utf-8")
        print(f"[processor] done session_dir={session_dir}")

        return SessionArtifacts(
            session_dir=session_dir,
            full_transcript=full_transcript,
            summary_markdown=summary_markdown,
            speaker_transcripts=speaker_items,
        )

    async def _compress_audio(self, wav_path: Path) -> Path:
        mp3_path = wav_path.with_suffix(".mp3")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(wav_path),
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(mp3_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print("[processor] ffmpeg not found in PATH, keeping WAV output")
            return wav_path
        code = await proc.wait()
        if code == 0 and mp3_path.exists():
            wav_path.unlink(missing_ok=True)
            print(f"[processor] compressed to mp3: {mp3_path.name}")
            return mp3_path
        print(f"[processor] ffmpeg compression failed (code={code}), keeping WAV output")
        return wav_path

    @staticmethod
    def _build_transcript_markdown(items: list[SpeakerTranscript]) -> str:
        lines = ["# Full Transcript", ""]
        for item in items:
            lines.append(f"## {item.speaker_name} (`{item.user_id}`)")
            lines.append(item.transcript or "_[no speech detected]_")
            lines.append("")
        return "\n".join(lines).strip() + "\n"
