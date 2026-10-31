"""Natural-language command -> validated :class:`ExecutionPlan`.

The planner asks an LLM for a JSON object ``{"steps": [...]}`` constrained by the
:class:`PlanDraft` JSON schema, validates it with Pydantic, and on failure feeds
the validation error back to the model for a bounded number of repair rounds.
``plan_id`` and ``original_prompt`` are always set by Clavix, never by the model.

LLM backends implement the tiny :class:`LLMClient` protocol, so tests and
benchmarks can substitute :class:`MockLLMClient`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal, Protocol, TypedDict

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clavix.config import PlannerConfig
from clavix.schemas.actions import ActionStep, ExecutionPlan


class ChatMessage(TypedDict):
    """One chat turn in the OpenAI/Ollama message format."""

    role: Literal["system", "user", "assistant"]
    content: str


class LLMClient(Protocol):
    """Minimal synchronous chat-completion interface."""

    def complete(self, messages: Sequence[ChatMessage], json_schema: dict[str, Any]) -> str:
        """Return the raw text of the model's reply.

        Args:
            messages: Conversation so far, starting with the system prompt.
            json_schema: JSON schema the reply must conform to.
        """
        ...


class PlanDraft(BaseModel):
    """The part of an :class:`ExecutionPlan` the LLM is allowed to author."""

    model_config = ConfigDict(extra="forbid")

    steps: list[ActionStep] = Field(min_length=1)


class PlanningError(RuntimeError):
    """Raised when no valid plan could be obtained from the LLM."""


SYSTEM_PROMPT = """\
You are the planner of Clavix, a desktop automation agent. Convert the user's spoken \
command into a JSON object {{"steps": [...]}} and output ONLY that JSON.

Host OS: {os_name}. Shell commands run through {shell}.

Each step has: step_id (int, starting at 1, increasing), action_type, action_name, \
payload (object), expected_postcondition (string or null).

Actions (action_name -> allowed action_type -> payload):
- launch_url -> DETERMINISTIC -> {{"url": "https://..."}}
- run_command -> DETERMINISTIC -> {{"cmd": str, "timeout_sec": int = 10, "detach": bool = false}}
  Use detach=true to start GUI applications that keep running.
- take_screenshot -> DETERMINISTIC -> {{"filepath": str, "target_region": [left, top, width, height] | null}}
- click_element -> OS_GROUNDING or VISUAL_FALLBACK -> {{"target_label": str, "fallback_coord": [x, y] | null}}
- speak_feedback -> FEEDBACK -> {{"message": str, "blocking": bool = false}}

Postconditions ("<kind>:<argument>"): window_title_contains:<text>, process_running:<name>, \
file_exists:<path>, exit_code:<int>, stdout_contains:<text>.

Rules:
1. Prefer DETERMINISTIC actions (URLs, shell commands) whenever they can achieve the goal.
2. Use OS_GROUNDING only to interact with on-screen UI that has no deterministic path.
3. Use VISUAL_FALLBACK only if exact screen coordinates are known.
4. Add a postcondition whenever the effect can be checked.
5. Never emit destructive commands (deleting files, formatting disks, shutting down).
"""

FEW_SHOT_EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    (
        "open github",
        {
            "steps": [
                {
                    "step_id": 1,
                    "action_type": "DETERMINISTIC",
                    "action_name": "launch_url",
                    "payload": {"url": "https://github.com"},
                    "expected_postcondition": "window_title_contains:GitHub",
                }
            ]
        },
    ),
    (
        "open notepad and click the file menu",
        {
            "steps": [
                {
                    "step_id": 1,
                    "action_type": "DETERMINISTIC",
                    "action_name": "run_command",
                    "payload": {"cmd": "notepad", "detach": True},
                    "expected_postcondition": "window_title_contains:Notepad",
                },
                {
                    "step_id": 2,
                    "action_type": "OS_GROUNDING",
                    "action_name": "click_element",
                    "payload": {"target_label": "File", "fallback_coord": None},
                    "expected_postcondition": None,
                },
            ]
        },
    ),
    (
        "take a screenshot and save it as shot.png, then tell me when you're done",
        {
            "steps": [
                {
                    "step_id": 1,
                    "action_type": "DETERMINISTIC",
                    "action_name": "take_screenshot",
                    "payload": {"filepath": "shot.png", "target_region": None},
                    "expected_postcondition": "file_exists:shot.png",
                },
                {
                    "step_id": 2,
                    "action_type": "FEEDBACK",
                    "action_name": "speak_feedback",
                    "payload": {"message": "Screenshot saved.", "blocking": False},
                    "expected_postcondition": None,
                },
            ]
        },
    ),
    # Hardening (P1-1): llama3:8b dropped a requested confirmation, so show one after a URL too.
    (
        "open wikipedia and let me know once it's up",
        {
            "steps": [
                {
                    "step_id": 1,
                    "action_type": "DETERMINISTIC",
                    "action_name": "launch_url",
                    "payload": {"url": "https://www.wikipedia.org"},
                    "expected_postcondition": None,
                },
                {
                    "step_id": 2,
                    "action_type": "FEEDBACK",
                    "action_name": "speak_feedback",
                    "payload": {"message": "Wikipedia is open.", "blocking": False},
                    "expected_postcondition": None,
                },
            ]
        },
    ),
    # Hardening (P1-1): llama3:8b set detach=true on mkdir; one-shot shell commands never detach.
    (
        "create a folder named archive",
        {
            "steps": [
                {
                    "step_id": 1,
                    "action_type": "DETERMINISTIC",
                    "action_name": "run_command",
                    "payload": {"cmd": "mkdir archive", "timeout_sec": 10, "detach": False},
                    "expected_postcondition": "file_exists:archive",
                },
            ]
        },
    ),
]
"""(command, plan) pairs shown to the model as prior conversation turns."""

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def build_messages(command: str, few_shot: bool = True) -> list[ChatMessage]:
    """Assemble the system prompt, optional few-shot turns and the user command."""
    if sys.platform == "win32":
        os_name, shell = "Windows", "cmd.exe"
    elif sys.platform == "darwin":
        os_name, shell = "macOS", "/bin/sh"
    else:
        os_name, shell = "Linux", "/bin/sh"
    messages: list[ChatMessage] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(os_name=os_name, shell=shell)}
    ]
    if few_shot:
        for example_cmd, example_plan in FEW_SHOT_EXAMPLES:
            messages.append({"role": "user", "content": example_cmd})
            messages.append({"role": "assistant", "content": json.dumps(example_plan)})
    messages.append({"role": "user", "content": command})
    return messages


def extract_json(raw: str) -> Any:
    """Parse a model reply as JSON, tolerating a surrounding Markdown code fence."""
    match = _FENCE_RE.match(raw)
    return json.loads(match.group(1) if match else raw)


class Planner:
    """Turns commands into validated plans using an :class:`LLMClient`."""

    def __init__(self, client: LLMClient, max_retries: int = 2, few_shot: bool = True) -> None:
        """
        Args:
            client: LLM backend.
            max_retries: Repair rounds after the first invalid reply.
            few_shot: Include :data:`FEW_SHOT_EXAMPLES` in the prompt.
        """
        self._client = client
        self._max_retries = max_retries
        self._few_shot = few_shot
        self._schema = PlanDraft.model_json_schema()

    async def plan(self, command: str) -> ExecutionPlan:
        """Produce a validated plan for ``command``.

        Raises:
            PlanningError: If the backend fails or every attempt is invalid.
        """
        messages = build_messages(command, self._few_shot)
        last_error = ""
        for _attempt in range(self._max_retries + 1):
            try:
                raw = await asyncio.to_thread(self._client.complete, messages, self._schema)
            except Exception as exc:
                raise PlanningError(f"LLM backend error: {type(exc).__name__}: {exc}") from exc
            try:
                draft = PlanDraft.model_validate(extract_json(raw))
                return ExecutionPlan(original_prompt=command, steps=draft.steps)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply was not a valid plan:\n"
                            f"{last_error}\nReturn only the corrected JSON object."
                        ),
                    },
                ]
        raise PlanningError(f"no valid plan after {self._max_retries + 1} attempts; last error: {last_error}")


class OllamaClient:
    """Local Ollama backend using ``/api/chat`` with schema-constrained output."""

    def __init__(
        self,
        model: str = "llama3:8b",
        base_url: str = "http://localhost:11434",
        timeout_sec: float = 60.0,
        temperature: float = 0.0,
        session: requests.Session | None = None,
    ) -> None:
        self._model = model
        self._url = base_url.rstrip("/") + "/api/chat"
        self._timeout_sec = timeout_sec
        self._temperature = temperature
        self._session = session or requests.Session()

    def complete(self, messages: Sequence[ChatMessage], json_schema: dict[str, Any]) -> str:
        """POST the conversation; Ollama's ``format`` enforces the JSON schema."""
        response = self._session.post(
            self._url,
            json={
                "model": self._model,
                "messages": list(messages),
                "stream": False,
                "format": json_schema,
                "options": {"temperature": self._temperature},
            },
            timeout=self._timeout_sec,
        )
        response.raise_for_status()
        return str(response.json()["message"]["content"])


class GeminiClient:
    """Google Gemini backend using ``generateContent`` in JSON response mode."""

    _ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        timeout_sec: float = 60.0,
        temperature: float = 0.0,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self._url = self._ENDPOINT.format(model=model)
        self._timeout_sec = timeout_sec
        self._temperature = temperature
        self._session = session or requests.Session()

    def complete(self, messages: Sequence[ChatMessage], json_schema: dict[str, Any]) -> str:
        """POST the conversation; the schema is appended to the system instruction.

        Gemini's ``responseSchema`` accepts only an OpenAPI subset (no ``$defs``),
        so JSON mode plus Pydantic validation is used instead.
        """
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        system += "\n\nJSON schema of the reply:\n" + json.dumps(json_schema)
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
            if m["role"] != "system"
        ]
        response = self._session.post(
            self._url,
            headers={"x-goog-api-key": self._api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": self._temperature,
                },
            },
            timeout=self._timeout_sec,
        )
        response.raise_for_status()
        return str(response.json()["candidates"][0]["content"]["parts"][0]["text"])


class MockLLMClient:
    """Deterministic backend for tests and offline benchmarks.

    Replies are taken in order from ``responses`` (strings, or dicts that are
    JSON-encoded), or computed by a ``responder`` callable from the messages.
    Every call is recorded in :attr:`calls`.
    """

    def __init__(
        self,
        responses: Iterable[str | dict[str, Any]] = (),
        responder: Callable[[Sequence[ChatMessage]], str | dict[str, Any]] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._responder = responder
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: Sequence[ChatMessage], json_schema: dict[str, Any]) -> str:
        """Return the next scripted reply."""
        self.calls.append(list(messages))
        if self._responder is not None:
            reply = self._responder(messages)
        elif self._responses:
            reply = self._responses.pop(0)
        else:
            raise RuntimeError("MockLLMClient has no responses left")
        return reply if isinstance(reply, str) else json.dumps(reply)


def build_planner(config: PlannerConfig, mock_client: LLMClient | None = None) -> Planner:
    """Create a :class:`Planner` for the configured provider.

    Args:
        config: Planner configuration.
        mock_client: Client to use when ``provider`` is ``"mock"``.

    Raises:
        ValueError: If Gemini is selected without an API key, or mock without a client.
    """
    client: LLMClient
    match config.provider:
        case "ollama":
            client = OllamaClient(config.model, config.base_url, config.timeout_sec, config.temperature)
        case "gemini":
            api_key = os.environ.get(config.gemini_api_key_env)
            if not api_key:
                raise ValueError(f"set {config.gemini_api_key_env} to use the Gemini planner")
            client = GeminiClient(api_key, config.gemini_model, config.timeout_sec, config.temperature)
        case "mock":
            if mock_client is None:
                raise ValueError("provider 'mock' requires a mock_client")
            client = mock_client
    return Planner(client, max_retries=config.max_retries)
