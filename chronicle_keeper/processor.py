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
import time
from typing import NamedTuple

import discord

from .llm_client import LLMClient
from .metrics import RuntimeMetrics
from .whisper_client import WhisperClient, TranscriptSegment

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
class TimelineEntry:
    segment_index: int
    start_seconds: float
    end_seconds: float
    user_id: int
    speaker_name: str
    text: str


@dataclass
class SessionArtifacts:
    session_dir: Path
    full_transcript: str
    full_transcript_txt_path: Path
    summary_markdown: str
    summary_path: Path
    speaker_transcripts: list[SpeakerTranscript]
    mixed_audio_path: Path | None


class SavedAudioEntry(NamedTuple):
    path: Path
    speaker_name: str
    user_id: int
    segment_index: int


class SessionProcessor:
    def __init__(
        self,
        base_data_dir: Path,
        whisper: WhisperClient,
        llm: LLMClient,
        audio_dual_pipeline_enabled: bool = False,
        audio_normalize: bool = False,
        audio_vad_enabled: bool = False,
        audio_target_sample_rate: int = 0,
        audio_target_channels: int = 0,
        audio_mp3_vbr_quality: int = 4,
        summary_chunk_chars: int = 14000,
        summary_context_relevance_gate: bool = False,
        summary_context_min_relevance: float = 0.40,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self._base_data_dir = base_data_dir
        self._whisper = whisper
        self._llm = llm
        self._audio_dual_pipeline_enabled = audio_dual_pipeline_enabled
        self._audio_normalize = audio_normalize
        self._audio_vad_enabled = audio_vad_enabled
        self._audio_target_sample_rate = max(0, audio_target_sample_rate)
        self._audio_target_channels = max(0, audio_target_channels)
        self._audio_mp3_vbr_quality = min(9, max(0, audio_mp3_vbr_quality))
        self._summary_chunk_chars = max(4000, summary_chunk_chars)
        self._summary_context_relevance_gate = bool(summary_context_relevance_gate)
        self._summary_context_min_relevance = min(
            1.0, max(0.0, float(summary_context_min_relevance))
        )
        self._metrics = metrics

    def _observe_metric(self, stage: str, started: float, ok: bool) -> None:
        if self._metrics is None:
            return
        self._metrics.observe(stage, time.perf_counter() - started, ok)

    @staticmethod
    def _summary_relevance_excerpt(full_transcript: str, limit: int = 12000) -> str:
        text = (full_transcript or "").strip()
        if len(text) <= limit:
            return text
        part = max(1000, limit // 3)
        middle_start = max(0, len(text) // 2 - part // 2)
        middle_end = min(len(text), middle_start + part)
        return (
            text[:part]
            + "\n\n...[middle omitted]...\n\n"
            + text[middle_start:middle_end]
            + "\n\n...[tail omitted]...\n\n"
            + text[-part:]
        )

    async def _resolve_effective_summary_context(
        self,
        *,
        full_transcript: str,
        summary_language: str,
        session_context: str,
        name_hints: str,
        checkpoint: dict,
        checkpoint_path: Path,
    ) -> tuple[str, str]:
        if not self._summary_context_relevance_gate:
            checkpoint["summary_context_applied"] = bool(
                session_context.strip() or name_hints.strip()
            )
            self._write_checkpoint(checkpoint_path, checkpoint)
            return session_context, name_hints
        if not (session_context.strip() or name_hints.strip()):
            checkpoint["summary_context_applied"] = False
            checkpoint["summary_context_relevance_score"] = None
            checkpoint["summary_context_gate_reason"] = "empty_context"
            self._write_checkpoint(checkpoint_path, checkpoint)
            return session_context, name_hints

        excerpt = self._summary_relevance_excerpt(full_transcript)
        gate_started = time.perf_counter()
        try:
            score, reason = await self._llm.assess_context_relevance(
                excerpt,
                session_context,
                name_hints,
                language=summary_language,
            )
            self._observe_metric("llm_context_gate", gate_started, True)
        except Exception as exc:
            self._observe_metric("llm_context_gate", gate_started, False)
            checkpoint["summary_context_applied"] = True
            checkpoint["summary_context_relevance_score"] = None
            checkpoint["summary_context_gate_reason"] = f"gate_failed:{exc}"
            self._write_checkpoint(checkpoint_path, checkpoint)
            logger.warning(
                "[processor] context gate failed, applying context by default: %s",
                exc,
            )
            return session_context, name_hints

        apply_context = score >= self._summary_context_min_relevance
        checkpoint["summary_context_applied"] = apply_context
        checkpoint["summary_context_relevance_score"] = score
        checkpoint["summary_context_gate_reason"] = reason
        self._write_checkpoint(checkpoint_path, checkpoint)
        logger.info(
            "[processor] context gate score=%.3f threshold=%.3f applied=%s reason=%s",
            score,
            self._summary_context_min_relevance,
            apply_context,
            reason,
        )
        if not apply_context:
            return "", ""
        return session_context, name_hints

    async def process_sink(
        self,
        guild: discord.Guild,
        sink: discord.sinks.Sink,
        summary_language: str = "ru",
        session_context: str = "",
        name_hints: str = "",
        campaign_id: str = "",
        campaign_name: str = "",
    ) -> SessionArtifacts:
        return await self.process_sinks(
            guild,
            [sink],
            summary_language=summary_language,
            session_context=session_context,
            name_hints=name_hints,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
        )

    async def save_recording_segment(
        self,
        guild: discord.Guild,
        sink: discord.sinks.Sink,
        session_dir: Path,
        segment_index: int,
    ) -> list[Path]:
        if not getattr(sink, "audio_data", None):
            return []

        audio_dir = session_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        written_paths: list[Path] = []

        for user_id, audio_data in sink.audio_data.items():
            member = guild.get_member(int(user_id))
            speaker_name = (
                getattr(member, "display_name", None)
                or getattr(member, "name", None)
                or f"user_{user_id}"
            )
            base_name = (
                f"{sanitize_name(speaker_name)}_{user_id}_seg{segment_index:03d}"
            )
            wav_path = audio_dir / f"{base_name}.wav"
            file_obj = audio_data.file
            if isinstance(file_obj, BytesIO):
                file_obj.seek(0)
            elif hasattr(file_obj, "seek"):
                file_obj.seek(0)
            wav_path.write_bytes(file_obj.read())
            compressed_path = await self._compress_audio(wav_path)
            written_paths.append(compressed_path)

        return written_paths

    async def process_sinks(
        self,
        guild: discord.Guild,
        sinks: list[discord.sinks.Sink],
        summary_language: str = "ru",
        session_context: str = "",
        name_hints: str = "",
        campaign_id: str = "",
        campaign_name: str = "",
    ) -> SessionArtifacts:
        process_started = time.perf_counter()
        valid_sinks = [s for s in sinks if getattr(s, "audio_data", None)]
        if not valid_sinks:
            self._observe_metric("session_process", process_started, False)
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
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "summary_language_used": summary_language,
            "session_context_used": session_context,
            "name_hints_used": name_hints,
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
        speaker_transcript_chunks: OrderedDict[tuple[int, str], list[str]] = (
            OrderedDict()
        )
        timeline_entries: list[TimelineEntry] = []
        segment_audio_paths: dict[int, list[Path]] = {}
        segment_index = 0

        for sink in valid_sinks:
            segment_index += 1
            for user_id, audio_data in sink.audio_data.items():
                member = guild.get_member(int(user_id))
                speaker_name = (
                    getattr(member, "display_name", None)
                    or getattr(member, "name", None)
                    or f"user_{user_id}"
                )
                base_name = (
                    f"{sanitize_name(speaker_name)}_{user_id}_seg{segment_index:03d}"
                )
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
                timeline_result = None
                if self._audio_dual_pipeline_enabled:
                    logger.debug(
                        "[processor] transcribe timeline(raw) start speaker=%s file=%s",
                        speaker_name,
                        wav_path.name,
                    )
                    asr_started = time.perf_counter()
                    try:
                        timeline_result = await self._whisper.transcribe_file_detailed(
                            wav_path
                        )
                    except Exception:
                        self._observe_metric("asr_transcribe", asr_started, False)
                        raise
                    self._observe_metric("asr_transcribe", asr_started, True)
                compress_started = time.perf_counter()
                try:
                    compressed_path = await self._compress_audio(wav_path)
                except Exception:
                    self._observe_metric("audio_compress", compress_started, False)
                    raise
                self._observe_metric("audio_compress", compress_started, True)
                logger.debug(
                    "[processor] transcribe content(clean) start speaker=%s file=%s",
                    speaker_name,
                    compressed_path.name,
                )
                asr_started = time.perf_counter()
                try:
                    content_result = await self._whisper.transcribe_file_detailed(
                        compressed_path
                    )
                except Exception:
                    self._observe_metric("asr_transcribe", asr_started, False)
                    raise
                self._observe_metric("asr_transcribe", asr_started, True)
                transcript = content_result.text
                if not transcript and timeline_result is not None:
                    transcript = timeline_result.text
                logger.debug(
                    "[processor] transcribe done speaker=%s chars=%s",
                    speaker_name,
                    len(transcript),
                )
                timeline_segments = (
                    timeline_result.segments
                    if timeline_result is not None
                    else content_result.segments
                )
                timeline_entries.extend(
                    self._timeline_entries_from_segments(
                        timeline_segments,
                        segment_index=segment_index,
                        user_id=int(user_id),
                        speaker_name=speaker_name,
                    )
                )
                transcript_path = transcript_dir / f"{base_name}.md"
                transcript_path.write_text(
                    transcript or "_[no speech detected]_", encoding="utf-8"
                )
                speaker_items.append(
                    SpeakerTranscript(
                        user_id=int(user_id),
                        speaker_name=speaker_name,
                        audio_path=compressed_path,
                        transcript=transcript,
                    )
                )
                segment_audio_paths.setdefault(segment_index, []).append(
                    compressed_path
                )
                key = (int(user_id), speaker_name)
                speaker_transcript_chunks.setdefault(key, []).append(
                    transcript or "_[no speech detected]_"
                )
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
        timeline_entries.sort(
            key=lambda item: (
                item.segment_index,
                item.start_seconds,
                item.end_seconds,
                item.user_id,
            )
        )
        full_transcript = self._build_transcript_markdown(
            merged_items, timeline_entries
        )
        (session_dir / "full_transcript.md").write_text(
            full_transcript, encoding="utf-8"
        )
        full_transcript_txt = self._build_transcript_text(
            merged_items, timeline_entries
        )
        full_transcript_txt_path = session_dir / "full_transcript.txt"
        full_transcript_txt_path.write_text(full_transcript_txt, encoding="utf-8")
        checkpoint["status"] = "summarizing"
        self._write_checkpoint(checkpoint_path, checkpoint)

        (
            effective_session_context,
            effective_name_hints,
        ) = await self._resolve_effective_summary_context(
            full_transcript=full_transcript,
            summary_language=summary_language,
            session_context=session_context,
            name_hints=name_hints,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
        )

        chunks = self._split_transcript_for_summary(
            full_transcript, self._summary_chunk_chars
        )
        checkpoint["summary_chunks_total"] = len(chunks)
        self._write_checkpoint(checkpoint_path, checkpoint)
        logger.info(
            "[processor] summarize start chars=%s chunks=%s",
            len(full_transcript),
            len(chunks),
        )

        if len(chunks) <= 1:
            llm_started = time.perf_counter()
            try:
                summary_markdown = await self._llm.generate_summary(
                    full_transcript,
                    language=summary_language,
                    session_context=effective_session_context,
                    name_hints=effective_name_hints,
                )
            except Exception:
                self._observe_metric("llm_summarize", llm_started, False)
                self._observe_metric("session_process", process_started, False)
                raise
            self._observe_metric("llm_summarize", llm_started, True)
        else:
            chunk_summaries: list[str] = []
            for idx, chunk in enumerate(chunks, start=1):
                chunk_summary_path = summary_chunks_dir / f"chunk_{idx:03d}.md"
                if chunk_summary_path.exists():
                    chunk_summary = chunk_summary_path.read_text(encoding="utf-8")
                else:
                    llm_started = time.perf_counter()
                    try:
                        chunk_summary = await self._llm.generate_chunk_summary(
                            chunk,
                            chunk_index=idx,
                            total_chunks=len(chunks),
                            language=summary_language,
                            session_context=effective_session_context,
                            name_hints=effective_name_hints,
                        )
                    except Exception:
                        self._observe_metric("llm_summarize", llm_started, False)
                        self._observe_metric("session_process", process_started, False)
                        raise
                    self._observe_metric("llm_summarize", llm_started, True)
                    chunk_summary_path.write_text(chunk_summary, encoding="utf-8")
                chunk_summaries.append(f"## Chunk {idx}\n{chunk_summary}")
                checkpoint["summary_chunks_done"] = idx
                self._write_checkpoint(checkpoint_path, checkpoint)

            combined = "\n\n".join(chunk_summaries)
            (session_dir / "chunk_summaries.md").write_text(combined, encoding="utf-8")
            llm_started = time.perf_counter()
            try:
                summary_markdown = await self._llm.combine_chunk_summaries(
                    combined,
                    language=summary_language,
                    session_context=effective_session_context,
                    name_hints=effective_name_hints,
                )
            except Exception:
                self._observe_metric("llm_summarize", llm_started, False)
                self._observe_metric("session_process", process_started, False)
                raise
            self._observe_metric("llm_summarize", llm_started, True)

        summary_path = session_dir / "summary.md"
        summary_path.write_text(summary_markdown, encoding="utf-8")
        mix_started = time.perf_counter()
        try:
            mixed_audio_path = await self._build_mixed_session_audio(
                audio_dir, segment_audio_paths
            )
        except Exception:
            self._observe_metric("audio_mix", mix_started, False)
            self._observe_metric("session_process", process_started, False)
            raise
        self._observe_metric("audio_mix", mix_started, True)
        checkpoint["final_summary_done"] = True
        checkpoint["status"] = "done"
        self._write_checkpoint(checkpoint_path, checkpoint)
        logger.info("[processor] done session_dir=%s", session_dir)
        self._observe_metric("session_process", process_started, True)

        return SessionArtifacts(
            session_dir=session_dir,
            full_transcript=full_transcript,
            full_transcript_txt_path=full_transcript_txt_path,
            summary_markdown=summary_markdown,
            summary_path=summary_path,
            speaker_transcripts=speaker_items,
            mixed_audio_path=mixed_audio_path,
        )

    async def reprocess_saved_session(
        self,
        session_dir: Path,
        summary_language: str = "ru",
        session_context: str = "",
        name_hints: str = "",
        campaign_id: str = "",
        campaign_name: str = "",
    ) -> SessionArtifacts:
        reprocess_started = time.perf_counter()
        audio_dir = session_dir / "audio"
        transcript_dir = session_dir / "transcripts"
        summary_chunks_dir = session_dir / "summary_chunks"
        checkpoint_path = session_dir / "processing_state.json"
        if not audio_dir.exists() or not audio_dir.is_dir():
            self._observe_metric("session_reprocess", reprocess_started, False)
            raise RuntimeError(f"Session audio directory not found: {audio_dir}")

        transcript_dir.mkdir(parents=True, exist_ok=True)
        summary_chunks_dir.mkdir(parents=True, exist_ok=True)

        entries = self._collect_saved_audio_entries(audio_dir)
        if not entries:
            self._observe_metric("session_reprocess", reprocess_started, False)
            raise RuntimeError(f"No supported audio files found in {audio_dir}")
        checkpoint: dict = {}
        if checkpoint_path.exists():
            try:
                loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    checkpoint = loaded
            except Exception:
                checkpoint = {}
        checkpoint.update(
            {
                "status": "transcribing",
                "segments_total": len(entries),
                "persisted_segments": len(entries),
                "total_tracks": len(entries),
            }
        )
        self._write_checkpoint(checkpoint_path, checkpoint)

        logger.info(
            "[reprocess] start session_dir=%s tracks=%s",
            session_dir,
            len(entries),
        )

        speaker_items: list[SpeakerTranscript] = []
        speaker_transcript_chunks: OrderedDict[tuple[int, str], list[str]] = (
            OrderedDict()
        )
        timeline_entries: list[TimelineEntry] = []
        segment_audio_paths: dict[int, list[Path]] = {}
        for entry in entries:
            logger.debug(
                "[reprocess] transcribe start speaker=%s user_id=%s file=%s",
                entry.speaker_name,
                entry.user_id,
                entry.path.name,
            )
            asr_started = time.perf_counter()
            try:
                transcript_result = await self._whisper.transcribe_file_detailed(
                    entry.path
                )
            except Exception:
                self._observe_metric("asr_transcribe", asr_started, False)
                self._observe_metric("session_reprocess", reprocess_started, False)
                raise
            self._observe_metric("asr_transcribe", asr_started, True)
            transcript = transcript_result.text
            logger.debug(
                "[reprocess] transcribe done speaker=%s user_id=%s chars=%s",
                entry.speaker_name,
                entry.user_id,
                len(transcript),
            )
            timeline_entries.extend(
                self._timeline_entries_from_segments(
                    transcript_result.segments,
                    segment_index=entry.segment_index,
                    user_id=entry.user_id,
                    speaker_name=entry.speaker_name,
                )
            )
            transcript_path = transcript_dir / f"{entry.path.stem}.md"
            transcript_path.write_text(
                transcript or "_[no speech detected]_", encoding="utf-8"
            )
            speaker_items.append(
                SpeakerTranscript(
                    user_id=entry.user_id,
                    speaker_name=entry.speaker_name,
                    audio_path=entry.path,
                    transcript=transcript,
                )
            )
            segment_audio_paths.setdefault(entry.segment_index, []).append(entry.path)
            key = (entry.user_id, entry.speaker_name)
            speaker_transcript_chunks.setdefault(key, []).append(
                transcript or "_[no speech detected]_"
            )

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
        timeline_entries.sort(
            key=lambda item: (
                item.segment_index,
                item.start_seconds,
                item.end_seconds,
                item.user_id,
            )
        )
        full_transcript = self._build_transcript_markdown(
            merged_items, timeline_entries
        )
        (session_dir / "full_transcript.md").write_text(
            full_transcript, encoding="utf-8"
        )
        full_transcript_txt = self._build_transcript_text(
            merged_items, timeline_entries
        )
        full_transcript_txt_path = session_dir / "full_transcript.txt"
        full_transcript_txt_path.write_text(full_transcript_txt, encoding="utf-8")

        (
            effective_session_context,
            effective_name_hints,
        ) = await self._resolve_effective_summary_context(
            full_transcript=full_transcript,
            summary_language=summary_language,
            session_context=session_context,
            name_hints=name_hints,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
        )

        chunks = self._split_transcript_for_summary(
            full_transcript, self._summary_chunk_chars
        )
        checkpoint["status"] = "summarizing"
        checkpoint["summary_chunks_total"] = len(chunks)
        self._write_checkpoint(checkpoint_path, checkpoint)
        if len(chunks) <= 1:
            llm_started = time.perf_counter()
            try:
                summary_markdown = await self._llm.generate_summary(
                    full_transcript,
                    language=summary_language,
                    session_context=effective_session_context,
                    name_hints=effective_name_hints,
                )
            except Exception:
                self._observe_metric("llm_summarize", llm_started, False)
                self._observe_metric("session_reprocess", reprocess_started, False)
                raise
            self._observe_metric("llm_summarize", llm_started, True)
        else:
            chunk_summaries: list[str] = []
            for idx, chunk in enumerate(chunks, start=1):
                chunk_summary_path = summary_chunks_dir / f"chunk_{idx:03d}.md"
                llm_started = time.perf_counter()
                try:
                    chunk_summary = await self._llm.generate_chunk_summary(
                        chunk,
                        chunk_index=idx,
                        total_chunks=len(chunks),
                        language=summary_language,
                        session_context=effective_session_context,
                        name_hints=effective_name_hints,
                    )
                except Exception:
                    self._observe_metric("llm_summarize", llm_started, False)
                    self._observe_metric("session_reprocess", reprocess_started, False)
                    raise
                self._observe_metric("llm_summarize", llm_started, True)
                chunk_summary_path.write_text(chunk_summary, encoding="utf-8")
                chunk_summaries.append(f"## Chunk {idx}\n{chunk_summary}")
                checkpoint["summary_chunks_done"] = idx
                self._write_checkpoint(checkpoint_path, checkpoint)

            combined = "\n\n".join(chunk_summaries)
            (session_dir / "chunk_summaries.md").write_text(combined, encoding="utf-8")
            llm_started = time.perf_counter()
            try:
                summary_markdown = await self._llm.combine_chunk_summaries(
                    combined,
                    language=summary_language,
                    session_context=effective_session_context,
                    name_hints=effective_name_hints,
                )
            except Exception:
                self._observe_metric("llm_summarize", llm_started, False)
                self._observe_metric("session_reprocess", reprocess_started, False)
                raise
            self._observe_metric("llm_summarize", llm_started, True)

        summary_path = session_dir / "summary.md"
        summary_path.write_text(summary_markdown, encoding="utf-8")
        mix_started = time.perf_counter()
        try:
            mixed_audio_path = await self._build_mixed_session_audio(
                audio_dir, segment_audio_paths
            )
        except Exception:
            self._observe_metric("audio_mix", mix_started, False)
            self._observe_metric("session_reprocess", reprocess_started, False)
            raise
        self._observe_metric("audio_mix", mix_started, True)
        checkpoint["final_summary_done"] = True
        checkpoint["status"] = "done"
        self._write_checkpoint(checkpoint_path, checkpoint)
        logger.info(
            "[reprocess] done session_dir=%s transcript_file=%s",
            session_dir,
            full_transcript_txt_path.name,
        )
        self._observe_metric("session_reprocess", reprocess_started, True)
        return SessionArtifacts(
            session_dir=session_dir,
            full_transcript=full_transcript,
            full_transcript_txt_path=full_transcript_txt_path,
            summary_markdown=summary_markdown,
            summary_path=summary_path,
            speaker_transcripts=speaker_items,
            mixed_audio_path=mixed_audio_path,
        )

    async def _compress_audio(self, wav_path: Path) -> Path:
        mp3_path = wav_path.with_suffix(".mp3")
        ffmpeg_args = [
            "ffmpeg",
            "-y",
            "-i",
            str(wav_path),
        ]
        if self._audio_target_channels > 0:
            ffmpeg_args.extend(["-ac", str(self._audio_target_channels)])
        if self._audio_target_sample_rate > 0:
            ffmpeg_args.extend(["-ar", str(self._audio_target_sample_rate)])
        audio_filters: list[str] = []
        if self._audio_normalize:
            audio_filters.append("highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11")
        if self._audio_vad_enabled:
            # Conservative silence trimming for speech: keep short pauses, trim longer silence.
            audio_filters.append(
                "silenceremove=start_periods=1:start_duration=0.25:start_threshold=-45dB:"
                "stop_periods=-1:stop_duration=0.50:stop_threshold=-45dB"
            )
        if audio_filters:
            ffmpeg_args.extend(["-af", ",".join(audio_filters)])
        ffmpeg_args.extend(
            [
                "-codec:a",
                "libmp3lame",
                "-q:a",
                str(self._audio_mp3_vbr_quality),
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
            logger.info(
                "[processor] compressed to mp3: %s normalize=%s vad=%s",
                mp3_path.name,
                self._audio_normalize,
                self._audio_vad_enabled,
            )
            return mp3_path
        logger.warning(
            "[processor] ffmpeg compression failed (code=%s), keeping WAV output", code
        )
        return wav_path

    @staticmethod
    def _write_checkpoint(path: Path, payload: dict) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _parse_saved_audio_filename(path: Path) -> SavedAudioEntry | None:
        match = re.match(
            r"^(?P<speaker>.+)_(?P<uid>\d+)(?:_seg(?P<seg>\d{3}))?$",
            path.stem,
        )
        if not match:
            return None
        speaker_name = match.group("speaker").replace("_", " ").strip()
        if not speaker_name:
            speaker_name = "unknown"
        try:
            user_id = int(match.group("uid"))
        except ValueError:
            return None
        segment_index = int(match.group("seg")) if match.group("seg") else 0
        return SavedAudioEntry(
            path=path,
            speaker_name=speaker_name,
            user_id=user_id,
            segment_index=segment_index,
        )

    @classmethod
    def _collect_saved_audio_entries(cls, audio_dir: Path) -> list[SavedAudioEntry]:
        supported_ext = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}
        entries: list[SavedAudioEntry] = []
        fallback_index = 0
        for path in sorted(audio_dir.glob("*")):
            if not path.is_file() or path.suffix.lower() not in supported_ext:
                continue
            if path.stem.lower() == "mixed_session":
                continue
            parsed = cls._parse_saved_audio_filename(path)
            if parsed is None:
                fallback_index += 1
                parsed = SavedAudioEntry(
                    path=path,
                    speaker_name="unknown",
                    user_id=0,
                    segment_index=fallback_index,
                )
            entries.append(parsed)
        entries.sort(
            key=lambda e: (
                e.user_id,
                e.speaker_name.lower(),
                e.segment_index,
                e.path.name,
            )
        )
        return entries

    async def _build_mixed_session_audio(
        self,
        audio_dir: Path,
        segment_audio_paths: dict[int, list[Path]],
    ) -> Path | None:
        if not segment_audio_paths:
            return None
        mixed_output = audio_dir / "mixed_session.mp3"
        temp_dir = audio_dir / "_mixed_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        segment_mix_paths: list[Path] = []

        for seg_idx in sorted(segment_audio_paths.keys()):
            inputs = [p for p in segment_audio_paths[seg_idx] if p.exists()]
            if not inputs:
                continue
            out_path = temp_dir / f"mixed_seg{seg_idx:03d}.mp3"
            if len(inputs) == 1:
                ok = await self._transcode_to_mp3(inputs[0], out_path)
            else:
                ok = await self._mix_tracks_to_mp3(inputs, out_path)
            if ok and out_path.exists():
                segment_mix_paths.append(out_path)

        if not segment_mix_paths:
            return None

        concat_file = temp_dir / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in segment_mix_paths),
            encoding="utf-8",
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-codec:a",
                "libmp3lame",
                "-q:a",
                str(self._audio_mp3_vbr_quality),
                str(mixed_output),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("[processor] ffmpeg not found in PATH, mixed audio disabled")
            return None
        code = await proc.wait()
        if code == 0 and mixed_output.exists():
            logger.info("[processor] built mixed session audio: %s", mixed_output.name)
            return mixed_output
        logger.warning(
            "[processor] failed to build mixed session audio (code=%s)", code
        )
        return None

    async def _transcode_to_mp3(self, input_path: Path, output_path: Path) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-codec:a",
                "libmp3lame",
                "-q:a",
                str(self._audio_mp3_vbr_quality),
                str(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        return (await proc.wait()) == 0 and output_path.exists()

    async def _mix_tracks_to_mp3(self, inputs: list[Path], output_path: Path) -> bool:
        cmd = ["ffmpeg", "-y"]
        for path in inputs:
            cmd.extend(["-i", str(path)])
        cmd.extend(
            [
                "-filter_complex",
                f"amix=inputs={len(inputs)}:duration=longest:normalize=0",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                str(self._audio_mp3_vbr_quality),
                str(output_path),
            ]
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        return (await proc.wait()) == 0 and output_path.exists()

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
    def _timeline_entries_from_segments(
        segments: list[TranscriptSegment],
        *,
        segment_index: int,
        user_id: int,
        speaker_name: str,
    ) -> list[TimelineEntry]:
        entries: list[TimelineEntry] = []
        for seg in segments:
            if not seg.text.strip():
                continue
            entries.append(
                TimelineEntry(
                    segment_index=max(0, segment_index),
                    start_seconds=max(0.0, seg.start),
                    end_seconds=max(0.0, seg.end),
                    user_id=user_id,
                    speaker_name=speaker_name,
                    text=seg.text.strip(),
                )
            )
        return entries

    @staticmethod
    def _format_ts(seconds: float) -> str:
        total_ms = max(0, int(seconds * 1000))
        minutes, remainder_ms = divmod(total_ms, 60_000)
        secs, ms = divmod(remainder_ms, 1000)
        return f"{minutes:02d}:{secs:02d}.{ms:03d}"

    @classmethod
    def _build_transcript_markdown(
        cls,
        items: list[SpeakerTranscript],
        timeline: list[TimelineEntry],
    ) -> str:
        lines = ["# Full Transcript", ""]
        lines.append("## Chronological Timeline (Approximate)")
        if timeline:
            for item in timeline:
                lines.append(
                    (
                        f"- [seg{item.segment_index:03d} "
                        f"{cls._format_ts(item.start_seconds)}-{cls._format_ts(item.end_seconds)}] "
                        f"**{item.speaker_name}** (`{item.user_id}`): {item.text}"
                    )
                )
        else:
            lines.append("_No timed segments available from Whisper for this session._")
        lines.append("")
        lines.append("## Speaker Buckets")
        lines.append("")
        for speaker_item in items:
            lines.append(f"## {speaker_item.speaker_name} (`{speaker_item.user_id}`)")
            lines.append(speaker_item.transcript or "_[no speech detected]_")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    @classmethod
    def _build_transcript_text(
        cls,
        items: list[SpeakerTranscript],
        timeline: list[TimelineEntry],
    ) -> str:
        lines = ["Full Transcript", ""]
        lines.append("Chronological Timeline (Approximate)")
        if timeline:
            for item in timeline:
                lines.append(
                    (
                        f"[seg{item.segment_index:03d} "
                        f"{cls._format_ts(item.start_seconds)}-{cls._format_ts(item.end_seconds)}] "
                        f"{item.speaker_name} ({item.user_id}): {item.text}"
                    )
                )
        else:
            lines.append("No timed segments available from Whisper for this session.")
        lines.append("")
        lines.append("Speaker Buckets")
        lines.append("")
        for speaker_item in items:
            lines.append(f"{speaker_item.speaker_name} ({speaker_item.user_id})")
            lines.append(speaker_item.transcript or "[no speech detected]")
            lines.append("")
        return "\n".join(lines).strip() + "\n"
