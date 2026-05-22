"""TTS subpackage — text-to-speech synthesis providers.

Public re-exports, shared text-preparation utility, and provider factory.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Iterable

# ccslack.config only imports stdlib + dotenv + utils — no cycle risk.
from ccslack.config import config
from .base import SpeechSynthesizer, TtsAudio, TtsSynthesisError

_PAGINATION_RE = re.compile(r"\n\n\[\d+/\d+\]$")
_USER_PREFIX = "\U0001f464 "


def _make_edge() -> SpeechSynthesizer:
    # Lazy: optional dep; only load edge_tts when provider=edge
    from .edge import EdgeTtsSynthesizer

    return EdgeTtsSynthesizer(voice=config.tts_voice)


def _make_openai() -> SpeechSynthesizer:
    # Key resolution: CCSLACK_TTS_API_KEY → OPENAI_API_KEY.
    # CCSLACK_LLM_API_KEY is intentionally excluded — it may be a non-OpenAI key.
    api_key = config.tts_api_key or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        msg = "No API key for OpenAI TTS: set CCSLACK_TTS_API_KEY or OPENAI_API_KEY"
        raise ValueError(msg)
    # Lazy: keep openai module load deferred until this provider is selected
    from .openai import OpenAITtsSynthesizer

    return OpenAITtsSynthesizer(
        api_key=api_key,
        model=config.tts_model,
        voice=config.tts_voice,
    )


_PROVIDERS: dict[str, Callable[[], SpeechSynthesizer]] = {
    "edge": _make_edge,
    "openai": _make_openai,
}


def prepare_tts_text(parts: Iterable[str]) -> str:
    """Merge message parts into a clean, plain-text string for TTS."""
    # Lazy: avoids pulling in Telegram at import time
    from ccslack.entity_formatting import convert_to_entities

    cleaned_parts: list[str] = []
    for part in parts:
        cleaned = _PAGINATION_RE.sub("", part).strip()
        if cleaned:
            cleaned_parts.append(cleaned)
    combined = "\n".join(cleaned_parts)
    if combined.startswith(_USER_PREFIX):
        combined = combined[len(_USER_PREFIX) :]
    plain_text, _entities = convert_to_entities(combined)
    return plain_text.strip()


def get_synthesizer() -> SpeechSynthesizer | None:
    """Return a SpeechSynthesizer based on config, or None if TTS is disabled.

    Returns None if tts_provider is not configured (empty string).
    """
    provider = config.tts_provider
    if not provider:
        return None

    factory = _PROVIDERS.get(provider)
    if factory is None:
        msg = f"Unknown TTS provider: {provider!r}. Supported: {list(_PROVIDERS)}"
        raise ValueError(msg)

    return factory()


__all__ = [
    "SpeechSynthesizer",
    "TtsAudio",
    "TtsSynthesisError",
    "get_synthesizer",
    "prepare_tts_text",
]
