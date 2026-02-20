from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, UTC
from io import BytesIO
import json
import logging
from pathlib import Path
import re
from collections import OrderedDict

import discord

from .lmstudio_client import LMStudioClient
from .whisper_client import WhisperClient

logger = logging.getLogger(__name__)


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
    full_transcript_txt_path: Path
    summary_markdown: str
    summary_path: Path
    speaker_transcripts: list[SpeakerTranscript]


class SessionProcessor:
    def __init__(
        self,
        base_data_dir: Path,
        whisper: WhisperClient,
        lmstudio: LMStudioClient,
        audio_normalize: bool = False,
        summary_chunk_chars: int = 14000,
    ) -> None:
        self._base_data_dir = base_data_dir
        self._whisper = whisper
        self._lmstudio = lmstudio
        self._audio_normalize = audio_normalize
        self._summary_chunk_chars = max(4000, summary_chunk_chars)

    async def process_sink(
        self,
        guild: discord.Guild,
        sink: discord.sinks.Sink,
        summary_language: str = "ru",
    ) -> SessionArtifacts:
        return await self.process_sinks(guild, [sink], summary_language=summary_language)

    async def process_sinks(
        self,
        guild: discord.Guild,
        sinks: list[discord.sinks.Sink],
        summary_language: str = "ru",
    ) -> SessionArtifacts:
        valid_sinks = [s for s in sinks if getattr(s, "audio_data", None)]
        if not valid_sinks:
            raise RuntimeError("No audio data captured in any recording segment.")

        logger.info(
            "[processor] start guild=%s segments=%s tracks=%s",
            guild.id,
            len(valid_sinks),
            sum(len(s.audio_data) for s in valid_sinks),
        )
        now = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        session_dir = self._base_data_dir / "sessions" / str(guild.id) / now
        audio_dir = session_dir / "audio"
        transcript_dir = session_dir / "transcripts"
        checkpoint_path = session_dir / "processing_state.json"
        summary_chunks_dir = session_dir / "summary_chunks"
        audio_dir.mkdir(parents=True, exist_ok=True)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        summary_chunks_dir.mkdir(parents=True, exist_ok=True)

        checkpoint = {
            "guild_id": guild.id,
            "started_at_utc": now,
            "status": "transcribing",
            "segments_total": len(valid_sinks),
            "total_tracks": sum(len(s.audio_data) for s in valid_sinks),
            "transcribed_tracks": 0,
            "summary_chunks_total": 0,
            "summary_chunks_done": 0,
            "final_summary_done": False,
        }
        self._write_checkpoint(checkpoint_path, checkpoint)

        speaker_items: list[SpeakerTranscript] = []
        speaker_transcript_chunks: OrderedDict[tuple[int, str], list[str]] = OrderedDict()
        segment_index = 0

        for sink in valid_sinks:
            segment_index += 1
            for user_id, audio_data in sink.audio_data.items():
                member = guild.get_member(int(user_id))
                speaker_name = member.display_name if member else f"user_{user_id}"
                base_name = f"{sanitize_name(speaker_name)}_{user_id}_seg{segment_index:03d}"
                wav_path = audio_dir / f"{base_name}.wav"

                file_obj = audio_data.file
                if isinstance(file_obj, BytesIO):
                    file_obj.seek(0)
                elif hasattr(file_obj, "seek"):
                    file_obj.seek(0)
                wav_path.write_bytes(file_obj.read())

                logger.debug(
                    "[processor] prepared audio speaker=%s user_id=%s file=%s",
                    speaker_name,
                    user_id,
                    wav_path.name,
                )
                compressed_path = await self._compress_audio(wav_path)
                logger.debug(
                    "[processor] transcribe start speaker=%s file=%s",
                    speaker_name,
                    compressed_path.name,
                )
                transcript = await self._whisper.transcribe_file(compressed_path)
                logger.debug(
                    "[processor] transcribe done speaker=%s chars=%s",
                    speaker_name,
                    len(transcript),
                )
                transcript_path = transcript_dir / f"{base_name}.md"
                transcript_path.write_text(transcript or "_[no speech detected]_", encoding="utf-8")
                speaker_items.append(
                    SpeakerTranscript(
                        user_id=int(user_id),
                        speaker_name=speaker_name,
                        audio_path=compressed_path,
                        transcript=transcript,
                    )
                )
                key = (int(user_id), speaker_name)
                speaker_transcript_chunks.setdefault(key, []).append(transcript or "_[no speech detected]_")
                checkpoint["transcribed_tracks"] = len(speaker_items)
                self._write_checkpoint(checkpoint_path, checkpoint)

        merged_items: list[SpeakerTranscript] = []
        for (user_id, speaker_name), chunks in speaker_transcript_chunks.items():
            merged_items.append(
                SpeakerTranscript(
                    user_id=user_id,
                    speaker_name=speaker_name,
                    audio_path=Path(""),
                    transcript="\n\n".join(chunks).strip(),
                )
            )
        merged_items.sort(key=lambda item: item.speaker_name.lower())
        full_transcript = self._build_transcript_markdown(merged_items)
        (session_dir / "full_transcript.md").write_text(full_transcript, encoding="utf-8")
        full_transcript_txt = self._build_transcript_text(merged_items)
        full_transcript_txt_path = session_dir / "full_transcript.txt"
        full_transcript_txt_path.write_text(full_transcript_txt, encoding="utf-8")
        checkpoint["status"] = "summarizing"
        self._write_checkpoint(checkpoint_path, checkpoint)

        chunks = self._split_transcript_for_summary(full_transcript, self._summary_chunk_chars)
        checkpoint["summary_chunks_total"] = len(chunks)
        self._write_checkpoint(checkpoint_path, checkpoint)
        logger.info(
            "[processor] summarize start chars=%s chunks=%s",
            len(full_transcript),
            len(chunks),
        )

        if len(chunks) <= 1:
            summary_markdown = await self._lmstudio.generate_summary(full_transcript, language=summary_language)
        else:
            chunk_summaries: list[str] = []
            for idx, chunk in enumerate(chunks, start=1):
                chunk_summary_path = summary_chunks_dir / f"chunk_{idx:03d}.md"
                if chunk_summary_path.exists():
                    chunk_summary = chunk_summary_path.read_text(encoding="utf-8")
                else:
                    chunk_summary = await self._lmstudio.generate_chunk_summary(
                        chunk,
                        chunk_index=idx,
                        total_chunks=len(chunks),
                        language=summary_language,
                    )
                    chunk_summary_path.write_text(chunk_summary, encoding="utf-8")
                chunk_summaries.append(f"## Chunk {idx}\n{chunk_summary}")
                checkpoint["summary_chunks_done"] = idx
                self._write_checkpoint(checkpoint_path, checkpoint)

            combined = "\n\n".join(chunk_summaries)
            (session_dir / "chunk_summaries.md").write_text(combined, encoding="utf-8")
            summary_markdown = await self._lmstudio.combine_chunk_summaries(
                combined,
                language=summary_language,
            )

        summary_path = session_dir / "summary.md"
        summary_path.write_text(summary_markdown, encoding="utf-8")
        checkpoint["final_summary_done"] = True
        checkpoint["status"] = "done"
        self._write_checkpoint(checkpoint_path, checkpoint)
        logger.info("[processor] done session_dir=%s", session_dir)

        return SessionArtifacts(
            session_dir=session_dir,
            full_transcript=full_transcript,
            full_transcript_txt_path=full_transcript_txt_path,
            summary_markdown=summary_markdown,
            summary_path=summary_path,
            speaker_transcripts=speaker_items,
        )

    async def _compress_audio(self, wav_path: Path) -> Path:
        mp3_path = wav_path.with_suffix(".mp3")
        ffmpeg_args = [
            "ffmpeg",
            "-y",
            "-i",
            str(wav_path),
        ]
        if self._audio_normalize:
            ffmpeg_args.extend(
                [
                    "-af",
                    "highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11",
                ]
            )
        ffmpeg_args.extend(
            [
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(mp3_path),
            ]
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("[processor] ffmpeg not found in PATH, keeping WAV output")
            return wav_path
        code = await proc.wait()
        if code == 0 and mp3_path.exists():
            wav_path.unlink(missing_ok=True)
            if self._audio_normalize:
                logger.info("[processor] normalized + compressed to mp3: %s", mp3_path.name)
            else:
                logger.info("[processor] compressed to mp3: %s", mp3_path.name)
            return mp3_path
        logger.warning("[processor] ffmpeg compression failed (code=%s), keeping WAV output", code)
        return wav_path

    @staticmethod
    def _write_checkpoint(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _split_transcript_for_summary(text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        current: list[str] = []
        size = 0
        # Preserve speaker section boundaries where possible.
        for line in text.splitlines(keepends=True):
            line_len = len(line)
            if size + line_len > max_chars and current:
                chunks.append("".join(current))
                current = [line]
                size = line_len
            else:
                current.append(line)
                size += line_len
        if current:
            chunks.append("".join(current))
        return chunks

    @staticmethod
    def _build_transcript_markdown(items: list[SpeakerTranscript]) -> str:
        lines = ["# Full Transcript", ""]
        for item in items:
            lines.append(f"## {item.speaker_name} (`{item.user_id}`)")
            lines.append(item.transcript or "_[no speech detected]_")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _build_transcript_text(items: list[SpeakerTranscript]) -> str:
        lines = ["Full Transcript", ""]
        for item in items:
            lines.append(f"{item.speaker_name} ({item.user_id})")
            lines.append(item.transcript or "[no speech detected]")
            lines.append("")
        return "\n".join(lines).strip() + "\n"
