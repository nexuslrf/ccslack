"""Base types for TTS synthesis providers.

Defines the Protocol and result types that all SpeechSynthesizer implementations
must follow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class TtsSynthesisError(Exception):
    """Raised by any SpeechSynthesizer when synthesis fails in a known way."""


@dataclass(frozen=True, slots=True)
class TtsAudio:
    """Synthesized TTS audio payload."""

    data: bytes
    filename: str = "reply.mp3"


class SpeechSynthesizer(Protocol):
    """Protocol for TTS synthesis backends."""

    async def synthesize(self, text: str) -> TtsAudio:
        """Synthesize speech from plain text, returning audio bytes.

        Raises TtsSynthesisError on known backend failures.
        """
        ...
