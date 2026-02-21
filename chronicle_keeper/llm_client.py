from __future__ import annotations

import aiohttp
import re

from .config import Settings


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
        self._warmup_on_start = settings.llm_warmup_on_start

    @staticmethod
    def _narrative_style_instruction(language_name: str) -> str:
        return (
            "Style requirements:\n"
            f"- Write naturally and vividly in {language_name}.\n"
            "- Keep facts grounded in transcript content; do not invent events.\n"
            "- Prefer concise but atmospheric phrasing suitable for fantasy RPG sessions.\n"
            "- In 'Player-Facing Chronicle Post', use an engaging narrative tone with 2-4 short paragraphs.\n"
            "- You may add light dramatic phrasing, but preserve factual accuracy.\n"
        )

    async def _chat(self, system_prompt: str, user_prompt: str) -> str:
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
            async with session.post(endpoint, json=payload, timeout=600) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"LLM error {resp.status}: {body}")

        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"Unexpected LLM response: {body}")

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
            section_positions.append((match.start(), match.end(), match.group(1).strip()))

        extracted: dict[str, str] = {}
        for idx, (_, title_end, title) in enumerate(section_positions):
            next_start = section_positions[idx + 1][0] if idx + 1 < len(section_positions) else len(text)
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

    async def generate_summary(self, transcript_text: str, language: str = "ru") -> str:
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
            "Transcript chunk:\n"
            f"{chunk_text}"
        )
        return await self._chat(system_prompt, user_prompt)

    async def combine_chunk_summaries(self, chunk_summaries_markdown: str, language: str = "ru") -> str:
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
            "Chunk summaries:\n"
            f"{chunk_summaries_markdown}"
        )
        raw = await self._chat(system_prompt, user_prompt)
        return self._normalize_summary_markdown(raw, lang)

    async def warmup(self) -> tuple[bool, str]:
        if not self._warmup_on_start:
            return False, "disabled"
        try:
            _ = await self._chat(
                "You are a health-check assistant. Reply with exactly: OK",
                "OK",
            )
            return True, "ok"
        except Exception as exc:
            return False, str(exc)
