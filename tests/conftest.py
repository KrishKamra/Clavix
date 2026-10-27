"""Shared pytest fixtures. Nothing here touches the real desktop."""

from __future__ import annotations

from typing import Any

import pytest

from clavix.core.planner import MockLLMClient, Planner
from clavix.core.state_machine import ClavixAgent
from clavix.core.verifier import StateVerifier
from clavix.execution.dispatcher import CommandSafetyPolicy, Dispatcher
from clavix.testing.fakes import FakeGrounding, FakeNativeOps, RecordingFeedback


@pytest.fixture
def native() -> FakeNativeOps:
    return FakeNativeOps()


@pytest.fixture
def feedback() -> RecordingFeedback:
    return RecordingFeedback()


@pytest.fixture
def grounding() -> FakeGrounding:
    return FakeGrounding()


@pytest.fixture
def verifier() -> StateVerifier:
    return StateVerifier(
        timeout_sec=0.2,
        poll_interval_sec=0.05,
        window_titles=lambda: ["GitHub - Mozilla Firefox", "Untitled - Notepad"],
        process_names=lambda: ["explorer.exe", "notepad.exe"],
    )


@pytest.fixture
def dispatcher(grounding: FakeGrounding, feedback: RecordingFeedback, native: FakeNativeOps) -> Dispatcher:
    return Dispatcher(
        grounding,
        feedback,
        native,
        enable_visual_fallback=True,
        safety=CommandSafetyPolicy([r"\bshutdown\b"]),
    )


@pytest.fixture
def make_agent(dispatcher: Dispatcher, verifier: StateVerifier, feedback: RecordingFeedback):
    """Factory: ``make_agent(*llm_responses, **agent_kwargs) -> (agent, llm)``."""

    def _make(*responses: Any, **kwargs: Any) -> tuple[ClavixAgent, MockLLMClient]:
        llm = MockLLMClient(responses)
        agent = ClavixAgent(
            planner=Planner(llm, max_retries=kwargs.pop("planner_retries", 1)),
            dispatcher=dispatcher,
            verifier=verifier,
            feedback=feedback,
            **kwargs,
        )
        return agent, llm

    return _make
