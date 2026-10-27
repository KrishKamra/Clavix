"""Command-line entry point (``clavix``).

Examples::

    uv run clavix "open github"          # one command, planned by the configured LLM
    uv run clavix                        # interactive: type commands until Ctrl+C / EOF
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from clavix.config import load_config
from clavix.core.planner import build_planner
from clavix.runtime import build_runtime, harden_stdio


async def _run(command: str | None, config_path: str | None, show_states: bool) -> int:
    config = load_config(config_path)
    runtime = build_runtime(config)
    agent = runtime.make_agent(build_planner(config.planner))
    if show_states:
        agent.add_listener(lambda previous, current: print(f"[clavix] {previous} -> {current}"))
    if command is not None:
        outcome = await agent.run(command)
        return 0 if outcome.success else 1
    while True:
        outcome = await agent.run()
        if outcome.error_message and outcome.error_message.startswith("EOF"):
            return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the agent."""
    parser = argparse.ArgumentParser(description="Clavix voice-to-action agent.")
    parser.add_argument("command", nargs="?", help="command to execute (omit for interactive mode)")
    parser.add_argument("--config", help="config YAML (default: configs/default_config.yaml)")
    parser.add_argument("--states", action="store_true", help="print state transitions")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    harden_stdio()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        return asyncio.run(_run(args.command, args.config, args.states))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
