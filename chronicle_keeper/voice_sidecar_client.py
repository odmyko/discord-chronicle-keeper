from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass(frozen=True)
class VoiceSidecarClient:
    base_url: str
    token: str = ""
    timeout_seconds: float = 15.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.5

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/status")

    async def session_status(self, guild_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/v1/sessions/{str(guild_id)}/status")

    async def start_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        for key in ("guild_id", "voice_channel_id", "text_channel_id", "requested_by"):
            value = normalized.get(key)
            if value is None or value == "":
                continue
            normalized[key] = str(value)
        return await self._request("POST", "/v1/sessions/start", normalized)

    async def rotate_session(
        self, guild_id: int, reason: str = "manual"
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/sessions/rotate",
            {"guild_id": str(guild_id), "reason": reason},
        )

    async def stop_session(
        self, guild_id: int, reason: str = "manual"
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/sessions/stop",
            {"guild_id": str(guild_id), "reason": reason},
        )

    async def _request(
        self, method: str, route: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{route}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers: dict[str, str] = {}
        if self.token:
            headers["X-Sidecar-Token"] = self.token
        attempts = max(1, int(self.retry_attempts))
        delay = max(0.05, float(self.retry_backoff_seconds))
        last_error: Exception | None = None
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for attempt in range(1, attempts + 1):
                try:
                    async with session.request(method, url, json=payload) as response:
                        body = await response.text()
                        if response.status >= 400:
                            snippet = body.strip().replace("\n", " ")
                            if len(snippet) > 280:
                                snippet = f"{snippet[:277]}..."
                            error = RuntimeError(
                                f"sidecar {method} {route} failed: {response.status} {snippet}"
                            )
                            # Retry only clearly transient classes.
                            if response.status in {429, 502, 503, 504}:
                                last_error = error
                                if attempt < attempts:
                                    await asyncio.sleep(delay * attempt)
                                    continue
                            raise error
                        try:
                            return await response.json()
                        except Exception as exc:
                            raise RuntimeError(
                                f"sidecar {method} {route} returned non-json response"
                            ) from exc
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_error = exc
                    if attempt < attempts:
                        await asyncio.sleep(delay * attempt)
                        continue
                    detail = str(exc) or exc.__class__.__name__
                    raise RuntimeError(
                        f"sidecar {method} {route} network failure after {attempts} attempts: {detail}"
                    ) from exc
        if last_error is not None:
            raise RuntimeError(
                f"sidecar {method} {route} failed after {attempts} attempts: {last_error}"
            )
        raise RuntimeError(f"sidecar {method} {route} failed unexpectedly")
