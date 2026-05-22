"""OpenAI TTS synthesis backend.

Uses httpx to call the OpenAI audio/speech endpoint. No openai SDK required.
"""

from __future__ import annotations

import httpx

from .base import TtsAudio, TtsSynthesisError

_OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAITtsSynthesizer:
    """Speech synthesizer backed by the OpenAI TTS API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._base_url = (base_url or _OPENAI_BASE_URL).rstrip("/")

    async def synthesize(self, text: str) -> TtsAudio:
        """Synthesize speech via the OpenAI audio/speech endpoint.

        Raises:
            ValueError: if text is empty.
            TtsSynthesisError: on API or network failure.
        """
        if not text.strip():
            msg = "Cannot synthesize empty text"
            raise ValueError(msg)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self._base_url}/audio/speech",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "input": text,
                        "voice": self._voice,
                        "response_format": "mp3",
                    },
                    timeout=60.0,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                msg = f"TTS failed: {exc.response.status_code} {exc.response.text}"
                raise TtsSynthesisError(msg) from exc
            except httpx.HTTPError as exc:
                msg = f"TTS failed: {exc}"
                raise TtsSynthesisError(msg) from exc

        audio = response.content
        if not audio:
            raise TtsSynthesisError("No audio bytes received from OpenAI TTS")
        return TtsAudio(data=audio)
