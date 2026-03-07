from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import tempfile
import wave
from typing import Any

from .asr import TranscriptResult
from .config import Settings

logger = logging.getLogger(__name__)


class Qwen3ASRClient:
    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.qwen_asr_model
        self._dtype_name = settings.qwen_asr_dtype
        self._attn_implementation = settings.qwen_asr_attn_implementation
        self._max_new_tokens = settings.qwen_asr_max_new_tokens
        self._max_inference_batch_size = settings.qwen_asr_max_inference_batch_size
        self._language = settings.asr_language
        self._warmup_on_start = settings.qwen_asr_warmup_on_start
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    async def transcribe_file(self, audio_path: Path) -> str:
        result = await self.transcribe_file_detailed(audio_path)
        return result.text

    async def transcribe_file_detailed(self, audio_path: Path) -> TranscriptResult:
        await self._ensure_model()
        return await asyncio.to_thread(self._transcribe_sync, audio_path)

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

    async def _ensure_model(self) -> None:
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            self._model = await asyncio.to_thread(self._load_model_sync)

    def _load_model_sync(self) -> Any:
        os.environ.setdefault("TORCH_COMPILE_DISABLE", "1")
        os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

        import torch
        from qwen_asr import Qwen3ASRModel

        dtype = self._resolve_dtype(torch)
        device_map = "cuda:0" if torch.cuda.is_available() else "cpu"
        load_kwargs: dict[str, Any] = {
            "max_inference_batch_size": self._max_inference_batch_size,
            "max_new_tokens": self._max_new_tokens,
        }
        load_kwargs["dtype"] = dtype
        load_kwargs["device_map"] = device_map
        if self._attn_implementation != "auto":
            load_kwargs["attn_implementation"] = self._attn_implementation
        logger.info(
            "[qwen3-asr] loading model backend=transformers model=%s device=%s dtype=%s attn=%s",
            self._model_name,
            device_map,
            str(dtype).replace("torch.", ""),
            self._attn_implementation,
        )
        return Qwen3ASRModel.from_pretrained(self._model_name, **load_kwargs)

    def _resolve_dtype(self, torch_module: Any) -> Any:
        name = self._dtype_name.strip().lower()
        if name == "auto":
            if self._attn_implementation == "flash_attention_2":
                return (
                    torch_module.float16
                    if torch_module.cuda.is_available()
                    else torch_module.float32
                )
            return (
                torch_module.bfloat16
                if torch_module.cuda.is_available()
                else torch_module.float32
            )
        if name == "bfloat16":
            return torch_module.bfloat16
        if name == "float16":
            return torch_module.float16
        if name == "float32":
            return torch_module.float32
        raise RuntimeError(f"Unsupported Qwen3-ASR dtype: {self._dtype_name}")

    def _transcribe_sync(self, audio_path: Path) -> TranscriptResult:
        assert self._model is not None
        language = self._normalize_language(self._language)
        result = self._model.transcribe(
            audio=str(audio_path),
            language=language,
            return_time_stamps=False,
        )[0]
        text = str(getattr(result, "text", "") or "").strip()
        language = getattr(result, "language", None)
        logger.debug(
            "[qwen3-asr] transcribed file=%s chars=%s language=%s",
            audio_path.name,
            len(text),
            language,
        )
        return TranscriptResult(text=text, segments=[])

    @staticmethod
    def _normalize_language(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        mapping = {
            "ru": "Russian",
            "en": "English",
            "uk": "Ukrainian",
            "ua": "Ukrainian",
        }
        lower = normalized.lower()
        if lower in mapping:
            return mapping[lower]
        # Qwen accepts title-case names like "Russian", "English", etc.
        return normalized
