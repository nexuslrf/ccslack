"""Edge TTS synthesis backend.

Uses the community-maintained edge-tts package (optional dep).
Module-level conditional import: edge-tts is imported at module load time inside a
try/except ImportError block; the package is only required when CCSLACK_TTS_PROVIDER=edge.
"""

from __future__ import annotations

from typing import Any

import structlog

from .base import TtsAudio, TtsSynthesisError

logger = structlog.get_logger()

_edge_tts_available = False
Communicate: Any = None

try:
    from edge_tts import Communicate
    from edge_tts.exceptions import (
        NoAudioReceived,
        UnexpectedResponse,
        UnknownResponse,
        WebSocketError,
    )

    _edge_tts_available = True
except ImportError:
    NoAudioReceived = UnexpectedResponse = UnknownResponse = WebSocketError = Exception  # type: ignore[misc,assignment]


_EDGE_TTS_ERRORS = (
    NoAudioReceived,
    UnexpectedResponse,
    UnknownResponse,
    WebSocketError,
)


class EdgeTtsSynthesizer:
    """Speech synthesizer backed by edge-tts."""

    def __init__(self, voice: str) -> None:
        self._voice = voice

    async def synthesize(self, text: str) -> TtsAudio:
        """Synthesize speech from plain text.

        Raises:
            ValueError: if text is empty.
            TtsSynthesisError: on any edge-tts backend failure.
        """
        if not text.strip():
            msg = "Cannot synthesize empty text"
            raise ValueError(msg)

        if not _edge_tts_available or Communicate is None:
            raise ImportError(
                "edge-tts is not installed. Install it with: pip install ccslack[tts]"
            )

        communicate = Communicate(text, voice=self._voice)
        audio = bytearray()
        try:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    data = chunk.get("data")
                    if isinstance(data, (bytes, bytearray)):
                        audio.extend(data)
                    else:
                        logger.warning(
                            "Unexpected audio chunk payload type: %s", type(data)
                        )
        except _EDGE_TTS_ERRORS as exc:
            raise TtsSynthesisError(str(exc)) from exc

        if not audio:
            raise TtsSynthesisError("No audio bytes received from Edge TTS")
        return TtsAudio(data=bytes(audio))
