"""Acoustic ingestion contract (owner: Person 2 — Faster-Whisper / VAD).

The core only depends on :class:`AcousticInterface`. A production implementation
should capture microphone audio, gate it with VAD, transcribe with
Faster-Whisper and report the ASR latency it measured.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Transcript:
    """A recognised utterance.

    Attributes:
        text: Final transcription.
        t_asr_ms: Time from end-of-speech to final transcript, in milliseconds.
        confidence: Optional recogniser confidence in ``[0, 1]``.
    """

    text: str
    t_asr_ms: float = 0.0
    confidence: Optional[float] = None


class AcousticInterface(ABC):
    """Produces one transcript per user utterance."""

    @abstractmethod
    async def listen(self) -> Transcript:
        """Block until the next utterance is captured and transcribed."""


class ScriptedAcoustic(AcousticInterface):
    """Replays predefined commands as transcripts (tests and benchmarks)."""

    def __init__(self, commands: Iterable[str], t_asr_ms: float = 0.0) -> None:
        self._queue: deque[str] = deque(commands)
        self._t_asr_ms = t_asr_ms

    async def listen(self) -> Transcript:
        """Return the next scripted command.

        Raises:
            EOFError: When the script is exhausted.
        """
        if not self._queue:
            raise EOFError("no scripted commands left")
        return Transcript(text=self._queue.popleft(), t_asr_ms=self._t_asr_ms)


class ConsoleAcoustic(AcousticInterface):
    """Reads typed commands from stdin; a stand-in until the ASR module lands."""

    def __init__(self, prompt: str = "clavix> ") -> None:
        self._prompt = prompt

    async def listen(self) -> Transcript:
        """Read one line from the terminal without blocking the event loop.

        Raises:
            EOFError: When stdin is closed.
        """
        try:
            text = await asyncio.to_thread(input, self._prompt)
        except EOFError:
            raise EOFError("EOF on console input") from None
        return Transcript(text=text.strip(), t_asr_ms=0.0)
