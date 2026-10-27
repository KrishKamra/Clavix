"""Reusable conformance suites for teammate modules.

Subclass the contract for your interface in a ``tests/test_*.py`` file and
override the ``impl`` fixture. Every inherited test then runs against your
implementation::

    import pytest
    from clavix.testing.contracts import GroundingContract
    from clavix.modules.uia_grounding import UIAGrounding

    class TestUIAGrounding(GroundingContract):
        resolve_timeout_sec = 3.0
        known_label = "Start"          # an element that always exists on the test machine

        @pytest.fixture
        def impl(self) -> UIAGrounding:
            return UIAGrounding(timeout_sec=self.resolve_timeout_sec)

A module is not mergeable until its contract suite passes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import ClassVar

import pytest

from clavix.core.state_machine import TRANSITIONS
from clavix.modules.acoustic import AcousticInterface, Transcript
from clavix.modules.feedback import FeedbackInterface
from clavix.modules.grounding import GroundingInterface


class AcousticContract:
    """Conformance tests for :class:`AcousticInterface` implementations.

    The ``impl`` fixture must return an instance that can produce at least
    ``listen_calls`` transcripts without a live microphone (e.g. fed from WAV
    fixtures).
    """

    listen_calls: ClassVar[int] = 1

    @pytest.fixture
    def impl(self) -> AcousticInterface:
        raise NotImplementedError("override the 'impl' fixture")

    def test_implements_interface(self, impl: AcousticInterface) -> None:
        assert isinstance(impl, AcousticInterface)

    def test_listen_returns_well_formed_transcript(self, impl: AcousticInterface) -> None:
        for _ in range(self.listen_calls):
            transcript = asyncio.run(impl.listen())
            assert isinstance(transcript, Transcript)
            assert isinstance(transcript.text, str)
            assert transcript.text == transcript.text.strip(), "strip surrounding whitespace"
            assert transcript.t_asr_ms >= 0.0
            if transcript.confidence is not None:
                assert 0.0 <= transcript.confidence <= 1.0


class GroundingContract:
    """Conformance tests for :class:`GroundingInterface` implementations.

    Attributes:
        resolve_timeout_sec: The implementation's configured wait for elements.
        known_label: A label guaranteed to resolve on the test machine, or
            ``None`` to skip the positive-resolution test.
    """

    resolve_timeout_sec: ClassVar[float] = 5.0
    known_label: ClassVar[str | None] = None

    @pytest.fixture
    def impl(self) -> GroundingInterface:
        raise NotImplementedError("override the 'impl' fixture")

    def test_implements_interface(self, impl: GroundingInterface) -> None:
        assert isinstance(impl, GroundingInterface)

    def test_missing_element_returns_none_within_timeout(self, impl: GroundingInterface) -> None:
        label = f"clavix-nonexistent-{uuid.uuid4().hex}"
        start = time.monotonic()
        assert asyncio.run(impl.resolve_element(label)) is None
        assert time.monotonic() - start <= self.resolve_timeout_sec + 1.0

    def test_known_element_is_well_formed(self, impl: GroundingInterface) -> None:
        if self.known_label is None:
            pytest.skip("no known_label configured")
        element = asyncio.run(impl.resolve_element(self.known_label))
        assert element is not None, f"{self.known_label!r} did not resolve"
        assert element.label and element.control_type
        left, top, right, bottom = element.bounding_rect
        assert left <= right and top <= bottom
        x, y = element.center
        assert left <= x <= right and top <= y <= bottom

    def test_window_titles_are_strings(self, impl: GroundingInterface) -> None:
        try:
            titles = impl.list_window_titles()
        except NotImplementedError:
            pytest.skip("list_window_titles not provided")
        assert isinstance(titles, list)
        assert all(isinstance(t, str) and t for t in titles)


class FeedbackContract:
    """Conformance tests for :class:`FeedbackInterface` implementations.

    Attributes:
        nonblocking_budget_sec: Max time a non-blocking ``speak`` may take.
        blocking_timeout_sec: Max time a short blocking ``speak`` may take.
    """

    nonblocking_budget_sec: ClassVar[float] = 0.5
    blocking_timeout_sec: ClassVar[float] = 30.0

    @pytest.fixture
    def impl(self) -> FeedbackInterface:
        raise NotImplementedError("override the 'impl' fixture")

    def test_implements_interface(self, impl: FeedbackInterface) -> None:
        assert isinstance(impl, FeedbackInterface)

    def test_nonblocking_speak_returns_quickly(self, impl: FeedbackInterface) -> None:
        message = "This is a deliberately long sentence used to check non-blocking speech. " * 5
        start = time.monotonic()
        assert asyncio.run(impl.speak(message, blocking=False)) is None
        assert time.monotonic() - start <= self.nonblocking_budget_sec

    def test_blocking_speak_completes(self, impl: FeedbackInterface) -> None:
        asyncio.run(asyncio.wait_for(impl.speak("Done.", blocking=True), self.blocking_timeout_sec))

    def test_rapid_speak_calls_do_not_raise(self, impl: FeedbackInterface) -> None:
        async def burst() -> None:
            for i in range(5):
                await impl.speak(f"message {i}", blocking=False)

        asyncio.run(burst())

    def test_accepts_every_state_transition(self, impl: FeedbackInterface) -> None:
        async def walk() -> None:
            for source, targets in TRANSITIONS.items():
                for target in targets:
                    await impl.on_state_change(source.value, target.value)

        asyncio.run(walk())

    def test_request_abort_calls_bound_callback(self, impl: FeedbackInterface) -> None:
        calls: list[int] = []
        impl.bind_abort(lambda: calls.append(1))
        impl.request_abort()
        assert calls == [1]
