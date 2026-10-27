"""Composition root: builds every runtime component from configuration.

This is the only module that wires implementations together. The CLI and the
benchmark harness both go through :func:`build_runtime`, so swapping a teammate
module is a config change (``modules.<name>.class``), never a code change.
"""

from __future__ import annotations

import importlib
import io
import sys
from dataclasses import dataclass
from typing import TypeVar

from clavix.config import ClavixConfig, ComponentConfig
from clavix.core.planner import Planner
from clavix.core.state_machine import ClavixAgent
from clavix.core.verifier import StateVerifier, TitlesProvider, default_window_titles
from clavix.execution.dispatcher import CommandSafetyPolicy, Dispatcher
from clavix.execution.native_ops import NativeOps
from clavix.modules.acoustic import AcousticInterface
from clavix.modules.feedback import FeedbackInterface
from clavix.modules.grounding import GroundingInterface

T = TypeVar("T")


class ComponentError(RuntimeError):
    """Raised when a configured component cannot be imported or constructed."""


def load_component(spec: ComponentConfig, interface: type[T]) -> T:
    """Import ``spec.class_path`` and instantiate it with ``spec.options``.

    Args:
        spec: Component configuration (``"package.module:ClassName"`` + options).
        interface: Base class the instance must derive from.

    Raises:
        ComponentError: On import failure, a missing class, a constructor error,
            or an instance that does not implement ``interface``.
    """
    module_name, _, class_name = spec.class_path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ComponentError(f"cannot import {module_name!r}: {exc}") from exc
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ComponentError(f"{module_name!r} has no attribute {class_name!r}")
    try:
        instance = cls(**spec.options)
    except Exception as exc:
        raise ComponentError(f"cannot construct {spec.class_path}: {exc}") from exc
    if not isinstance(instance, interface):
        raise ComponentError(f"{spec.class_path} does not implement {interface.__name__}")
    return instance


def window_titles_via(grounding: GroundingInterface) -> TitlesProvider:
    """Prefer the grounding module's window titles; fall back to the built-in source."""

    def provider() -> list[str]:
        try:
            return grounding.list_window_titles()
        except NotImplementedError:
            return default_window_titles()

    return provider


def harden_stdio() -> None:
    """Replace unencodable characters on stdout/stderr instead of raising.

    Legacy Windows consoles use cp1252, so printing a window title or LLM text
    with e.g. ``◑`` would otherwise raise ``UnicodeEncodeError`` mid-task.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


@dataclass
class Runtime:
    """Fully wired components, shared across tasks."""

    config: ClavixConfig
    acoustic: AcousticInterface
    grounding: GroundingInterface
    feedback: FeedbackInterface
    dispatcher: Dispatcher
    verifier: StateVerifier

    def make_agent(self, planner: Planner) -> ClavixAgent:
        """Create an agent that uses these components and ``planner``."""
        return ClavixAgent.from_config(
            self.config,
            planner=planner,
            dispatcher=self.dispatcher,
            feedback=self.feedback,
            acoustic=self.acoustic,
            verifier=self.verifier,
        )


def build_runtime(
    config: ClavixConfig,
    *,
    acoustic: AcousticInterface | None = None,
    grounding: GroundingInterface | None = None,
    feedback: FeedbackInterface | None = None,
    native: NativeOps | None = None,
    verifier: StateVerifier | None = None,
) -> Runtime:
    """Build all components, loading unspecified modules from ``config.modules``.

    Keyword arguments override the configured implementation (used by tests and
    the harness). The feedback module is bound to the dispatcher's abort.
    """
    acoustic = acoustic or load_component(config.modules.acoustic, AcousticInterface)  # type: ignore[type-abstract]
    grounding = grounding or load_component(config.modules.grounding, GroundingInterface)  # type: ignore[type-abstract]
    feedback = feedback or load_component(config.modules.feedback, FeedbackInterface)  # type: ignore[type-abstract]
    dispatcher = Dispatcher(
        grounding,
        feedback,
        native,
        enable_visual_fallback=config.execution.enable_visual_fallback,
        safety=CommandSafetyPolicy(config.execution.blocked_command_patterns),
    )
    feedback.bind_abort(dispatcher.abort)
    verifier = verifier or StateVerifier(
        config.verifier.timeout_sec,
        config.verifier.poll_interval_sec,
        window_titles=window_titles_via(grounding),
    )
    return Runtime(config, acoustic, grounding, feedback, dispatcher, verifier)
