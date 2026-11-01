"""Composition root, component loading, and contract suites for built-in modules."""

from __future__ import annotations

import pytest

from clavix.config import ClavixConfig, ComponentConfig, load_config
from clavix.modules.acoustic import AcousticInterface, ScriptedAcoustic
from clavix.modules.feedback import ConsoleFeedback, FeedbackInterface
from clavix.modules.feedback_tts import TTSFeedback
from clavix.modules.grounding import GroundingInterface, StubGrounding, UIElement
from clavix.modules.grounding_uia import UIAGrounding
from clavix.runtime import ComponentError, build_runtime, load_component, window_titles_via
from clavix.testing.contracts import AcousticContract, FeedbackContract, GroundingContract
from clavix.testing.fakes import FakeGrounding, FakeNativeOps, RecordingFeedback


class TestScriptedAcousticContract(AcousticContract):
    listen_calls = 2

    @pytest.fixture
    def impl(self) -> AcousticInterface:
        return ScriptedAcoustic(["open github", "  take a screenshot  ".strip()], t_asr_ms=12.5)


class TestStubGroundingContract(GroundingContract):
    @pytest.fixture
    def impl(self) -> GroundingInterface:
        return StubGrounding()


class TestFakeGroundingContract(GroundingContract):
    known_label = "OK"

    @pytest.fixture
    def impl(self) -> GroundingInterface:
        return FakeGrounding(
            {"OK": UIElement("OK", "Button", (10, 10, 50, 30))}, window_titles=["Untitled - Notepad"]
        )


class TestConsoleFeedbackContract(FeedbackContract):
    @pytest.fixture
    def impl(self) -> FeedbackInterface:
        return ConsoleFeedback()


class TestRecordingFeedbackContract(FeedbackContract):
    @pytest.fixture
    def impl(self) -> FeedbackInterface:
        return RecordingFeedback()


class TestLoadComponent:
    def test_loads_with_options(self) -> None:
        spec = ComponentConfig.model_validate(
            {"class": "clavix.modules.feedback:ConsoleFeedback", "options": {"show_states": True}}
        )
        assert isinstance(load_component(spec, FeedbackInterface), ConsoleFeedback)  # type: ignore[type-abstract]

    @pytest.mark.parametrize(
        ("class_path", "message"),
        [
            ("clavix.nope:Thing", "cannot import"),
            ("clavix.modules.feedback:Missing", "no attribute"),
            ("clavix.modules.grounding:StubGrounding", "does not implement"),
        ],
    )
    def test_errors(self, class_path: str, message: str) -> None:
        with pytest.raises(ComponentError, match=message):
            load_component(ComponentConfig(class_path=class_path), FeedbackInterface)  # type: ignore[type-abstract]

    def test_bad_constructor_options(self) -> None:
        spec = ComponentConfig(class_path="clavix.modules.feedback:ConsoleFeedback", options={"x": 1})
        with pytest.raises(ComponentError, match="cannot construct"):
            load_component(spec, FeedbackInterface)  # type: ignore[type-abstract]

    def test_class_path_must_be_module_colon_class(self) -> None:
        with pytest.raises(ValueError):
            ComponentConfig(class_path="clavix.modules.feedback.ConsoleFeedback")


class TestBuildRuntime:
    def test_default_config_builds(self) -> None:
        runtime = build_runtime(load_config())
        assert isinstance(runtime.grounding, UIAGrounding)  # integrated in Phase 3; constructs without UIA
        assert isinstance(runtime.feedback, TTSFeedback)  # integrated in Phase 4; constructs without pyttsx3

    def test_feedback_abort_reaches_dispatcher(self) -> None:
        runtime = build_runtime(ClavixConfig(), feedback=RecordingFeedback(), native=FakeNativeOps())
        assert not runtime.dispatcher.aborted
        runtime.feedback.request_abort()
        assert runtime.dispatcher.aborted

    def test_window_titles_prefer_grounding(self) -> None:
        assert window_titles_via(FakeGrounding(window_titles=["A"]))() == ["A"]

    def test_window_titles_fall_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import clavix.runtime as rt

        monkeypatch.setattr(rt, "default_window_titles", lambda: ["fallback"])
        assert window_titles_via(StubGrounding())() == ["fallback"]
