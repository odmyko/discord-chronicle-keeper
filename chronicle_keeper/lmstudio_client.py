from __future__ import annotations

import aiohttp

from .config import Settings


class LMStudioClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.lmstudio_base_url
        self._model = settings.lmstudio_model
        self._temperature = settings.lmstudio_temperature
        self._max_tokens = settings.lmstudio_max_tokens

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

    async def generate_summary(self, transcript_text: str, language: str = "ru") -> str:
        lang = (language or "ru").lower().strip()
        if lang not in {"en", "uk", "ru"}:
            lang = "ru"

        language_names = {"en": "English", "uk": "Ukrainian", "ru": "Russian"}
        system_prompt = (
            "You are an assistant for a tabletop RPG game master. "
            f"Write all output strictly in {language_names[lang]}."
        )
        user_prompt = (
            "Using the transcript below, generate a markdown response with sections:\n"
            "1) # Session Summary\n"
            "2) # Key Events\n"
            "3) # NPCs and Factions\n"
            "4) # Open Threads\n"
            "5) # Player-Facing Chronicle Post\n\n"
            f"Return all text in {language_names[lang]}.\n\n"
            "Transcript:\n"
            f"{transcript_text}"
        )
        return await self._chat(system_prompt, user_prompt)

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
        system_prompt = (
            "You are an assistant for a tabletop RPG game master. "
            f"Write all output strictly in {language_names[lang]}."
        )
        user_prompt = (
            "Using the chunk summaries below, generate a markdown response with sections:\n"
            "1) # Session Summary\n"
            "2) # Key Events\n"
            "3) # NPCs and Factions\n"
            "4) # Open Threads\n"
            "5) # Player-Facing Chronicle Post\n\n"
            f"Return all text in {language_names[lang]}.\n\n"
            "Chunk summaries:\n"
            f"{chunk_summaries_markdown}"
        )
        return await self._chat(system_prompt, user_prompt)
