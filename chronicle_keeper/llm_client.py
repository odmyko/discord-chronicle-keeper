from __future__ import annotations

import asyncio
import aiohttp
import logging
import re

from .config import Settings

logger = logging.getLogger(__name__)


class LLMClient:
    _SUMMARY_SECTIONS = [
        "Session Summary",
        "Key Events",
        "NPCs and Factions",
        "Open Threads",
        "Player-Facing Chronicle Post",
    ]

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.llm_base_url
        self._model = settings.llm_model
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_tokens
        self._chronicle_min_words = int(
            getattr(settings, "llm_chronicle_min_words", 180)
        )
        self._chronicle_max_words = int(
            getattr(settings, "llm_chronicle_max_words", 320)
        )
        self._warmup_on_start = settings.llm_warmup_on_start
        self._lmstudio_auto_load = getattr(settings, "lmstudio_auto_load", False)
        self._lmstudio_control_base_url = getattr(
            settings, "lmstudio_control_base_url", ""
        )
        self._lmstudio_control_load_path = getattr(
            settings, "lmstudio_control_load_path", "/api/v1/models/load"
        )
        self._lmstudio_control_timeout_seconds = float(
            getattr(settings, "lmstudio_control_timeout_seconds", 180.0)
        )
        self._lmstudio_auto_load_wait_seconds = float(
            getattr(settings, "lmstudio_auto_load_wait_seconds", 1.5)
        )

    @staticmethod
    def _context_block(session_context: str, name_hints: str) -> str:
        parts: list[str] = []
        if session_context.strip():
            parts.append(
                "Session context from DM (canonical background unless transcript directly contradicts):\n"
                f"{session_context.strip()}"
            )
        if name_hints.strip():
            parts.append(
                "Canonical names and roles hints (prefer these spellings/roles):\n"
                f"{name_hints.strip()}"
            )
        if not parts:
            return ""
        return "\n\n".join(parts) + "\n\n"

    def _narrative_style_instruction(self, language_name: str) -> str:
        return (
            "Style requirements:\n"
            f"- Write naturally and vividly in {language_name}.\n"
            "- Keep facts grounded in transcript content; do not invent events.\n"
            f"- Use only {language_name}; do not insert foreign words, transliteration, or mixed-language fragments.\n"
            "- Prefer concise but atmospheric phrasing suitable for fantasy RPG sessions.\n"
            "- In 'Player-Facing Chronicle Post', use an engaging narrative tone with 3-5 medium paragraphs.\n"
            f"- Keep 'Player-Facing Chronicle Post' around {self._chronicle_min_words}-{self._chronicle_max_words} words total.\n"
            "- If transcript quality is noisy or details are uncertain, use cautious phrasing (e.g., 'likely', 'possibly') instead of hard claims.\n"
            "- You may add light dramatic phrasing, but preserve factual accuracy.\n"
        )

    async def _chat(
        self, system_prompt: str, user_prompt: str, timeout_seconds: int = 600
    ) -> str:
        endpoint = f"{self._base_url}/chat/completions"
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        async with aiohttp.ClientSession() as session:
            auto_load_attempted = False
            while True:
                async with session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        if (
                            not auto_load_attempted
                            and self._is_no_models_loaded_error(resp.status, body)
                            and await self._try_lmstudio_auto_load(session)
                        ):
                            auto_load_attempted = True
                            if self._lmstudio_auto_load_wait_seconds > 0:
                                await asyncio.sleep(
                                    self._lmstudio_auto_load_wait_seconds
                                )
                            continue
                        raise RuntimeError(f"LLM error {resp.status}: {body}")
                break

        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"Unexpected LLM response: {body}")

    @staticmethod
    def _is_no_models_loaded_error(status: int, body: object) -> bool:
        if status < 400:
            return False
        text = str(body).lower()
        return ("no models loaded" in text) or ("load a model" in text)

    async def _try_lmstudio_auto_load(self, session: aiohttp.ClientSession) -> bool:
        if not self._lmstudio_auto_load:
            return False
        if not self._lmstudio_control_base_url:
            return False
        model_name = (self._model or "").strip()
        if not model_name:
            return False
        endpoint = (
            f"{self._lmstudio_control_base_url}{self._lmstudio_control_load_path}"
        )
        payload_candidates = (
            {"model": model_name},
            {"identifier": model_name},
            {"name": model_name},
        )
        for payload in payload_candidates:
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(
                        total=self._lmstudio_control_timeout_seconds
                    ),
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status < 400:
                        logger.info(
                            "[llm] lmstudio auto-load requested model=%s endpoint=%s",
                            model_name,
                            endpoint,
                        )
                        return True
                    body_text = str(body).lower()
                    if "already loaded" in body_text:
                        return True
            except Exception:
                continue
        logger.warning(
            "[llm] lmstudio auto-load failed model=%s endpoint=%s",
            model_name,
            endpoint,
        )
        return False

    @classmethod
    def _empty_section_message(cls, language: str) -> str:
        messages = {
            "en": "_No details were produced by the model for this section._",
            "uk": "_Для цього розділу модель не надала деталей._",
            "ru": "_Для этого раздела модель не выдала деталей._",
        }
        return messages.get(language, messages["ru"])

    @classmethod
    def _normalize_summary_markdown(cls, raw: str, language: str) -> str:
        text = (raw or "").strip()
        if not text:
            text = ""

        # Collect existing top-level sections from model output.
        section_positions = []
        for match in re.finditer(r"(?m)^#\s+(.+?)\s*$", text):
            section_positions.append(
                (match.start(), match.end(), match.group(1).strip())
            )

        extracted: dict[str, str] = {}
        for idx, (_, title_end, title) in enumerate(section_positions):
            next_start = (
                section_positions[idx + 1][0]
                if idx + 1 < len(section_positions)
                else len(text)
            )
            body = text[title_end:next_start].strip()
            normalized_title = title.lower()
            for required in cls._SUMMARY_SECTIONS:
                if normalized_title == required.lower():
                    extracted[required] = body
                    break

        fallback_msg = cls._empty_section_message(language)
        lines: list[str] = []
        for title in cls._SUMMARY_SECTIONS:
            lines.append(f"# {title}")
            body = extracted.get(title, "").strip()
            lines.append(body if body else fallback_msg)
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    async def generate_summary(
        self,
        transcript_text: str,
        language: str = "ru",
        session_context: str = "",
        name_hints: str = "",
    ) -> str:
        lang = (language or "ru").lower().strip()
        if lang not in {"en", "uk", "ru"}:
            lang = "ru"

        language_names = {"en": "English", "uk": "Ukrainian", "ru": "Russian"}
        style_instruction = self._narrative_style_instruction(language_names[lang])
        system_prompt = (
            "You are an assistant for a tabletop RPG game master. "
            f"Write all output strictly in {language_names[lang]}."
        )
        user_prompt = (
            "Using the transcript below, generate markdown with EXACT top-level headers in this exact order:\n"
            "# Session Summary\n"
            "# Key Events\n"
            "# NPCs and Factions\n"
            "# Open Threads\n"
            "# Player-Facing Chronicle Post\n\n"
            "Do not add extra top-level headers. Keep bullet lists concise.\n\n"
            f"{style_instruction}\n"
            f"Return all text in {language_names[lang]}.\n\n"
            f"{self._context_block(session_context, name_hints)}"
            "Transcript:\n"
            f"{transcript_text}"
        )
        raw = await self._chat(system_prompt, user_prompt)
        return self._normalize_summary_markdown(raw, lang)

    async def generate_chunk_summary(
        self,
        chunk_text: str,
        chunk_index: int,
        total_chunks: int,
        language: str = "ru",
        session_context: str = "",
        name_hints: str = "",
    ) -> str:
        lang = (language or "ru").lower().strip()
        if lang not in {"en", "uk", "ru"}:
            lang = "ru"
        language_names = {"en": "English", "uk": "Ukrainian", "ru": "Russian"}
        system_prompt = (
            "You summarize one chunk of a tabletop RPG transcript. "
            f"Write strictly in {language_names[lang]}."
        )
        user_prompt = (
            f"Chunk {chunk_index}/{total_chunks} of a long session transcript.\n"
            "Return concise markdown with:\n"
            "- Key events\n"
            "- Important NPCs/factions\n"
            "- Open threads\n"
            "- Notable player actions\n\n"
            f"{self._context_block(session_context, name_hints)}"
            "Transcript chunk:\n"
            f"{chunk_text}"
        )
        return await self._chat(system_prompt, user_prompt)

    async def combine_chunk_summaries(
        self,
        chunk_summaries_markdown: str,
        language: str = "ru",
        session_context: str = "",
        name_hints: str = "",
    ) -> str:
        lang = (language or "ru").lower().strip()
        if lang not in {"en", "uk", "ru"}:
            lang = "ru"
        language_names = {"en": "English", "uk": "Ukrainian", "ru": "Russian"}
        style_instruction = self._narrative_style_instruction(language_names[lang])
        system_prompt = (
            "You are an assistant for a tabletop RPG game master. "
            f"Write all output strictly in {language_names[lang]}."
        )
        user_prompt = (
            "Using the chunk summaries below, generate markdown with EXACT top-level headers in this exact order:\n"
            "# Session Summary\n"
            "# Key Events\n"
            "# NPCs and Factions\n"
            "# Open Threads\n"
            "# Player-Facing Chronicle Post\n\n"
            "Do not add extra top-level headers. Keep bullet lists concise.\n\n"
            f"{style_instruction}\n"
            f"Return all text in {language_names[lang]}.\n\n"
            f"{self._context_block(session_context, name_hints)}"
            "Chunk summaries:\n"
            f"{chunk_summaries_markdown}"
        )
        raw = await self._chat(system_prompt, user_prompt)
        return self._normalize_summary_markdown(raw, lang)

    async def warmup(self) -> tuple[bool, str]:
        if not self._warmup_on_start:
            return False, "disabled"
        timeout_seconds = 20
        if self._lmstudio_auto_load and self._lmstudio_control_base_url:
            timeout_seconds = max(
                timeout_seconds,
                int(self._lmstudio_control_timeout_seconds + 30),
            )
        try:
            _ = await self._chat(
                "You are a health-check assistant. Reply with exactly: OK",
                "OK",
                timeout_seconds=timeout_seconds,
            )
            return True, "ok"
        except Exception as exc:
            return False, str(exc)

    async def assess_context_relevance(
        self,
        transcript_excerpt: str,
        session_context: str,
        name_hints: str,
        *,
        language: str = "ru",
    ) -> tuple[float, str]:
        prompt_language = {
            "ru": "Russian",
            "uk": "Ukrainian",
            "en": "English",
        }.get((language or "ru").lower().strip(), "Russian")
        system_prompt = (
            "You classify relevance between transcript and campaign context. "
            "Return only one line in this exact format: SCORE=<0..1>;LABEL=<RELEVANT|OFFTOPIC>;REASON=<short text>."
        )
        user_prompt = (
            f"Language hint for transcript: {prompt_language}\n\n"
            "Campaign context:\n"
            f"{session_context.strip() or '[empty]'}\n\n"
            "Name hints:\n"
            f"{name_hints.strip() or '[empty]'}\n\n"
            "Transcript excerpt:\n"
            f"{transcript_excerpt.strip()[:12000]}\n\n"
            "Classify whether campaign context should influence summary."
        )
        raw = await self._chat(system_prompt, user_prompt, timeout_seconds=120)
        text = (raw or "").strip()
        score = 0.0
        label = "OFFTOPIC"
        reason = text
        score_m = re.search(r"SCORE\s*=\s*([01](?:\.\d+)?)", text, re.IGNORECASE)
        if score_m:
            try:
                score = min(1.0, max(0.0, float(score_m.group(1))))
            except ValueError:
                score = 0.0
        label_m = re.search(r"LABEL\s*=\s*(RELEVANT|OFFTOPIC)", text, re.IGNORECASE)
        if label_m:
            label = label_m.group(1).upper()
        reason_m = re.search(r"REASON\s*=\s*(.+)$", text, re.IGNORECASE)
        if reason_m:
            reason = reason_m.group(1).strip()
        if label == "RELEVANT" and score <= 0.0:
            score = 0.5
        return score, reason
