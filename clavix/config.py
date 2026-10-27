"""Typed runtime configuration loaded from ``configs/default_config.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "default_config.yaml"


class PlannerConfig(BaseModel):
    """LLM planner settings."""

    provider: Literal["ollama", "gemini", "mock"] = "ollama"
    model: str = "llama3:8b"
    base_url: str = "http://localhost:11434"
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_key_env: str = "GEMINI_API_KEY"
    timeout_sec: float = 60.0
    temperature: float = 0.0
    max_retries: int = Field(default=2, ge=0)


class ExecutionConfig(BaseModel):
    """Dispatcher and native-operation settings."""

    enable_visual_fallback: bool = False
    blocked_command_patterns: list[str] = Field(default_factory=list)


class VerifierConfig(BaseModel):
    """State verifier polling settings."""

    timeout_sec: float = 5.0
    poll_interval_sec: float = 0.25


class AgentConfig(BaseModel):
    """State machine behaviour."""

    max_step_retries: int = Field(default=1, ge=0)
    speak_summary: bool = True


class BenchmarkConfig(BaseModel):
    """Benchmark harness locations."""

    suite_path: str = "configs/benchmark_suite.json"
    output_dir: str = "benchmarks"


class ComponentConfig(BaseModel):
    """A pluggable module: ``class`` is ``"package.module:ClassName"``.

    ``options`` are passed to the class constructor as keyword arguments.
    """

    model_config = ConfigDict(populate_by_name=True)

    class_path: str = Field(alias="class", pattern=r"^[\w.]+:\w+$")
    options: dict[str, Any] = Field(default_factory=dict)


class ModulesConfig(BaseModel):
    """Which implementation of each teammate module to load."""

    acoustic: ComponentConfig = Field(
        default_factory=lambda: ComponentConfig(class_path="clavix.modules.acoustic:ConsoleAcoustic")
    )
    grounding: ComponentConfig = Field(
        default_factory=lambda: ComponentConfig(class_path="clavix.modules.grounding:StubGrounding")
    )
    feedback: ComponentConfig = Field(
        default_factory=lambda: ComponentConfig(class_path="clavix.modules.feedback:ConsoleFeedback")
    )


class ClavixConfig(BaseModel):
    """Top-level configuration."""

    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    benchmark: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    modules: ModulesConfig = Field(default_factory=ModulesConfig)


def load_config(path: Optional[str | Path] = None) -> ClavixConfig:
    """Load configuration from YAML, falling back to built-in defaults.

    Args:
        path: YAML file to load. Defaults to ``configs/default_config.yaml`` in
            the repository; if that file is absent, defaults are returned.

    Raises:
        FileNotFoundError: If an explicit ``path`` does not exist.
    """
    if path is None:
        if not DEFAULT_CONFIG_PATH.exists():
            return ClavixConfig()
        config_path = DEFAULT_CONFIG_PATH
    else:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(config_path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return ClavixConfig.model_validate(data)


def resolve_repo_path(path: str | Path) -> Path:
    """Resolve a config-relative path against the repository root."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p
