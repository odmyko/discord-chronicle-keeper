from __future__ import annotations

import aiohttp

from .config import Settings


class LMStudioClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.lmstudio_base_url
        self._model = settings.lmstudio_model
        self._temperature = settings.lmstudio_temperature
        self._max_tokens = settings.lmstudio_max_tokens

    async def generate_summary(self, transcript_text: str) -> str:
        endpoint = f"{self._base_url}/chat/completions"
        system_prompt = (
            "You are an assistant for a tabletop RPG game master. "
            "Produce a structured session summary and a short player-facing chronicle post."
        )
        user_prompt = (
            "Using the transcript below, generate a markdown response with sections:\n"
            "1) # Session Summary\n"
            "2) # Key Events\n"
            "3) # NPCs and Factions\n"
            "4) # Open Threads\n"
            "5) # Player-Facing Chronicle Post\n\n"
            "Transcript:\n"
            f"{transcript_text}"
        )

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
                    raise RuntimeError(f"LM Studio error {resp.status}: {body}")

        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"Unexpected LM Studio response: {body}")
