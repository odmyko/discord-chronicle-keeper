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

from .asr import ASRClient
from .llm_client import LLMClient
from .metrics import RuntimeMetrics

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
    mixed_audio_path: Path | None


class SavedAudioEntry(NamedTuple):
    path: Path
    speaker_name: str
    user_id: int
    segment_index: int


class SessionProcessor:
    _TRANSCRIPT_NOISE_PATTERNS = (
        re.compile(r"\bПродолжение следует(?:\.\.\.|[.!?])?", re.IGNORECASE),
        re.compile(
            r"\bСубтитры\s+(?:создавал|сделал|делал)\s+[A-Za-zА-Яа-я0-9_.-]+",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bДобавил\s+субтитры\s+[A-Za-zА-Яа-я0-9_.-]+",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bСпасибо за субтитры\s+(?:[A-Za-zА-Яа-я0-9_.-]+\s*){1,4}",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:Редактор|Корректор)\s+субтитров?\s+[A-Za-zА-Яа-я0-9_.-]+",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:Редактор|Корректор)\s+[А-ЯA-Z]\.[А-ЯA-Z][А-Яа-яA-Za-z-]+",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:ДимаTorzok|DimaTorzok|Dima\s+Torzok|Дима\s+Torzok)\b",
            re.IGNORECASE,
        ),
    )

    def __init__(
        self,
        base_data_dir: Path,
        asr: ASRClient,
        llm: LLMClient,
        audio_dual_pipeline_enabled: bool = False,
        audio_normalize: bool = False,
        audio_vad_enabled: bool = False,
        audio_target_sample_rate: int = 0,
        audio_target_channels: int = 0,
        audio_mp3_vbr_quality: int = 4,
        summary_context_relevance_gate: bool = False,
        summary_context_min_relevance: float = 0.40,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self._base_data_dir = base_data_dir
        self._asr = asr
        self._llm = llm
        self._audio_dual_pipeline_enabled = audio_dual_pipeline_enabled
        self._audio_normalize = audio_normalize
        self._audio_vad_enabled = audio_vad_enabled
        self._audio_target_sample_rate = max(0, audio_target_sample_rate)
        self._audio_target_channels = max(0, audio_target_channels)
        self._audio_mp3_vbr_quality = min(9, max(0, audio_mp3_vbr_quality))
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
    def _collapse_consecutive_repeats(
        values: list[str], *, min_run_length: int = 3
    ) -> list[str]:
        if not values:
            return []
        collapsed: list[str] = []
        run: list[str] = [values[0]]
        for value in values[1:]:
            if value == run[-1]:
                run.append(value)
                continue
            if len(run) >= min_run_length:
                collapsed.append(run[0])
            else:
                collapsed.extend(run)
            run = [value]
        if len(run) >= min_run_length:
            collapsed.append(run[0])
        else:
            collapsed.extend(run)
        return collapsed

    @classmethod
    def _clean_transcript_text(cls, text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return ""

        for pattern in cls._TRANSCRIPT_NOISE_PATTERNS:
            cleaned = pattern.sub(" ", cleaned)

        cleaned = re.sub(
            r"\b([A-Za-zА-Яа-яЁё]{1,20})(?:[\s,.;:!?-]+\1){4,}\b", r"\1", cleaned
        )
        cleaned = re.sub(r"([.!?…])(?:\s*\1){2,}", r"\1", cleaned)

        parts = re.split(r"(?<=[.!?…])\s+|\n+", cleaned)
        normalized_parts: list[tuple[str, str]] = []
        for part in parts:
            piece = part.strip(" \t\r\n-–,;:")
            if not piece:
                continue
            norm = re.sub(r"[^\wа-яё]+", " ", piece.lower(), flags=re.IGNORECASE)
            norm = re.sub(r"\s+", " ", norm).strip()
            if not norm:
                continue
            normalized_parts.append((piece, norm))

        collapsed: list[str] = []
        run_pieces: list[str] = []
        run_norm = ""
        for piece, norm in normalized_parts:
            if run_pieces and norm == run_norm:
                run_pieces.append(piece)
                continue
            if run_pieces:
                if len(run_pieces) >= 3 and len(run_norm) <= 80:
                    collapsed.append(run_pieces[0])
                else:
                    collapsed.extend(run_pieces)
            run_pieces = [piece]
            run_norm = norm
        if run_pieces:
            if len(run_pieces) >= 3 and len(run_norm) <= 80:
                collapsed.append(run_pieces[0])
            else:
                collapsed.extend(run_pieces)
        cleaned = " ".join(collapsed)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

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
        audio_dir.mkdir(parents=True, exist_ok=True)
        transcript_dir.mkdir(parents=True, exist_ok=True)

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
                    content_result = await self._asr.transcribe_file_detailed(
                        compressed_path
                    )
                except Exception:
                    self._observe_metric("asr_transcribe", asr_started, False)
                    raise
                self._observe_metric("asr_transcribe", asr_started, True)
                transcript = self._clean_transcript_text(content_result.text)
                logger.debug(
                    "[processor] transcribe done speaker=%s chars=%s",
                    speaker_name,
                    len(transcript),
                )
                if not transcript:
                    cleaned_segments = [
                        self._clean_transcript_text(seg.text)
                        for seg in content_result.segments
                    ]
                    cleaned_segments = [seg for seg in cleaned_segments if seg]
                    if cleaned_segments:
                        transcript = " ".join(cleaned_segments).strip()
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
        full_transcript = self._build_transcript_markdown(merged_items)
        (session_dir / "full_transcript.md").write_text(
            full_transcript, encoding="utf-8"
        )
        full_transcript_txt = self._build_transcript_text(merged_items)
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

        summary_input = self._build_llm_input_from_transcript_files(
            transcript_dir=transcript_dir,
            fallback_full_transcript=full_transcript,
        )
        checkpoint["summary_chunks_total"] = 1
        checkpoint["summary_chunks_done"] = 0
        self._write_checkpoint(checkpoint_path, checkpoint)
        logger.info("[processor] summarize start input_chars=%s", len(summary_input))

        llm_started = time.perf_counter()
        try:
            summary_markdown = await self._llm.generate_summary(
                summary_input,
                language=summary_language,
                session_context=effective_session_context,
                name_hints=effective_name_hints,
            )
        except Exception:
            self._observe_metric("llm_summarize", llm_started, False)
            self._observe_metric("session_process", process_started, False)
            raise
        self._observe_metric("llm_summarize", llm_started, True)
        checkpoint["summary_chunks_done"] = 1
        self._write_checkpoint(checkpoint_path, checkpoint)

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
        audio_subdir: str | None = None,
    ) -> SessionArtifacts:
        reprocess_started = time.perf_counter()
        selected_audio_subdir = (audio_subdir or "").strip()
        if selected_audio_subdir:
            audio_dir = session_dir / selected_audio_subdir
        else:
            preferred_vad_dir = session_dir / "audio_vad"
            if self._audio_vad_enabled and preferred_vad_dir.exists():
                audio_dir = preferred_vad_dir
                selected_audio_subdir = "audio_vad"
            else:
                audio_dir = session_dir / "audio"
                selected_audio_subdir = "audio"
        transcript_dir = session_dir / "transcripts"
        checkpoint_path = session_dir / "processing_state.json"
        if not audio_dir.exists() or not audio_dir.is_dir():
            self._observe_metric("session_reprocess", reprocess_started, False)
            raise RuntimeError(f"Session audio directory not found: {audio_dir}")

        transcript_dir.mkdir(parents=True, exist_ok=True)

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
                "audio_source_subdir": selected_audio_subdir,
            }
        )
        self._write_checkpoint(checkpoint_path, checkpoint)

        logger.info(
            "[reprocess] start session_dir=%s tracks=%s audio_subdir=%s",
            session_dir,
            len(entries),
            selected_audio_subdir,
        )

        speaker_items: list[SpeakerTranscript] = []
        speaker_transcript_chunks: OrderedDict[tuple[int, str], list[str]] = (
            OrderedDict()
        )
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
                transcript_result = await self._asr.transcribe_file_detailed(entry.path)
            except Exception:
                self._observe_metric("asr_transcribe", asr_started, False)
                self._observe_metric("session_reprocess", reprocess_started, False)
                raise
            self._observe_metric("asr_transcribe", asr_started, True)
            transcript = self._clean_transcript_text(transcript_result.text)
            logger.debug(
                "[reprocess] transcribe done speaker=%s user_id=%s chars=%s",
                entry.speaker_name,
                entry.user_id,
                len(transcript),
            )
            if not transcript:
                cleaned_segments = [
                    self._clean_transcript_text(seg.text)
                    for seg in transcript_result.segments
                ]
                cleaned_segments = [seg for seg in cleaned_segments if seg]
                if cleaned_segments:
                    transcript = " ".join(cleaned_segments).strip()
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
        full_transcript = self._build_transcript_markdown(merged_items)
        (session_dir / "full_transcript.md").write_text(
            full_transcript, encoding="utf-8"
        )
        full_transcript_txt = self._build_transcript_text(merged_items)
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

        checkpoint["status"] = "summarizing"
        checkpoint["summary_chunks_total"] = 1
        checkpoint["summary_chunks_done"] = 0
        self._write_checkpoint(checkpoint_path, checkpoint)
        summary_input = self._build_llm_input_from_transcript_files(
            transcript_dir=transcript_dir,
            fallback_full_transcript=full_transcript,
        )
        llm_started = time.perf_counter()
        try:
            summary_markdown = await self._llm.generate_summary(
                summary_input,
                language=summary_language,
                session_context=effective_session_context,
                name_hints=effective_name_hints,
            )
        except Exception:
            self._observe_metric("llm_summarize", llm_started, False)
            self._observe_metric("session_reprocess", reprocess_started, False)
            raise
        self._observe_metric("llm_summarize", llm_started, True)
        checkpoint["summary_chunks_done"] = 1
        self._write_checkpoint(checkpoint_path, checkpoint)

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

    async def resummarize_saved_session(
        self,
        session_dir: Path,
        summary_language: str = "ru",
        session_context: str = "",
        name_hints: str = "",
    ) -> SessionArtifacts:
        reprocess_started = time.perf_counter()
        transcript_dir = session_dir / "transcripts"
        checkpoint_path = session_dir / "processing_state.json"
        full_transcript_md_path = session_dir / "full_transcript.md"
        full_transcript_txt_path = session_dir / "full_transcript.txt"

        if not session_dir.exists() or not session_dir.is_dir():
            self._observe_metric("session_reprocess", reprocess_started, False)
            raise RuntimeError(f"Session directory not found: {session_dir}")
        if not transcript_dir.exists() and not full_transcript_md_path.exists():
            self._observe_metric("session_reprocess", reprocess_started, False)
            raise RuntimeError(
                f"No transcripts found in {session_dir} (expected transcripts/ or full_transcript.md)."
            )

        checkpoint: dict = {}
        if checkpoint_path.exists():
            try:
                loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    checkpoint = loaded
            except Exception:
                checkpoint = {}
        checkpoint["status"] = "summarizing"
        checkpoint["summary_chunks_total"] = 1
        checkpoint["summary_chunks_done"] = 0
        self._write_checkpoint(checkpoint_path, checkpoint)

        full_transcript = ""
        if full_transcript_md_path.exists():
            full_transcript = full_transcript_md_path.read_text(
                encoding="utf-8", errors="ignore"
            ).strip()
        elif full_transcript_txt_path.exists():
            full_transcript = full_transcript_txt_path.read_text(
                encoding="utf-8", errors="ignore"
            ).strip()

        summary_input = self._build_llm_input_from_transcript_files(
            transcript_dir=transcript_dir,
            fallback_full_transcript=full_transcript,
        )
        if not summary_input.strip():
            self._observe_metric("session_reprocess", reprocess_started, False)
            raise RuntimeError(f"No transcript content found in {session_dir}.")
        if not full_transcript:
            full_transcript = summary_input

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

        llm_started = time.perf_counter()
        try:
            summary_markdown = await self._llm.generate_summary(
                summary_input,
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
        if not full_transcript_txt_path.exists():
            full_transcript_txt_path.write_text(full_transcript, encoding="utf-8")

        mixed_audio_path: Path | None = None
        for candidate in (
            session_dir / "audio" / "mixed_session.mp3",
            session_dir / "audio_vad" / "mixed_session.mp3",
        ):
            if candidate.exists():
                mixed_audio_path = candidate
                break

        checkpoint["summary_language_used"] = summary_language
        checkpoint["summary_chunks_done"] = 1
        checkpoint["final_summary_done"] = True
        checkpoint["status"] = "done"
        self._write_checkpoint(checkpoint_path, checkpoint)
        self._observe_metric("session_reprocess", reprocess_started, True)

        return SessionArtifacts(
            session_dir=session_dir,
            full_transcript=full_transcript,
            full_transcript_txt_path=full_transcript_txt_path,
            summary_markdown=summary_markdown,
            summary_path=summary_path,
            speaker_transcripts=[],
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

        def _concat_line(path: Path) -> str:
            # Concat demuxer paths are resolved relative to concat file location.
            # Use absolute normalized path to avoid accidental double-prefix resolution.
            normalized = path.resolve().as_posix().replace("'", "'\\''")
            return f"file '{normalized}'"

        concat_file.write_text(
            "\n".join(_concat_line(p) for p in segment_mix_paths),
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
        # Retry once with captured stderr for actionable diagnostics.
        proc_retry = await asyncio.create_subprocess_exec(
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
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc_retry.communicate()
        retry_code = proc_retry.returncode
        logger.warning(
            "[processor] failed to build mixed session audio (code=%s retry_code=%s): %s",
            code,
            retry_code,
            (stderr.decode("utf-8", errors="ignore").strip()[:600] if stderr else ""),
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

    @classmethod
    def _build_transcript_markdown(
        cls,
        items: list[SpeakerTranscript],
    ) -> str:
        lines = ["# Full Transcript", ""]
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
    ) -> str:
        lines = ["Full Transcript", ""]
        lines.append("Speaker Buckets")
        lines.append("")
        for speaker_item in items:
            lines.append(f"{speaker_item.speaker_name} ({speaker_item.user_id})")
            lines.append(speaker_item.transcript or "[no speech detected]")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _build_llm_input_from_transcript_files(
        *,
        transcript_dir: Path,
        fallback_full_transcript: str,
    ) -> str:
        files = sorted(transcript_dir.glob("*.md"))
        if not files:
            return fallback_full_transcript
        parts: list[str] = []
        for idx, file_path in enumerate(files, start=1):
            body = file_path.read_text(encoding="utf-8", errors="ignore").strip()
            if not body:
                body = "_[no speech detected]_"
            parts.append(f"## Chunk {idx}: {file_path.stem}\n{body}")
        return "\n\n".join(parts).strip()
