"""Record one benchmark command into a mono 16 kHz PCM WAV fixture."""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

from clavix.modules.acoustic_whisper import MicrophoneSource


def main() -> int:
    """Record and save the requested benchmark task's spoken command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", help="task ID from configs/benchmark_suite.json")
    args = parser.parse_args()
    suite = json.loads(Path("configs/benchmark_suite.json").read_text(encoding="utf-8"))
    task = next((item for item in suite["tasks"] if item["task_id"] == args.task_id), None)
    if task is None:
        parser.error(f"unknown task ID: {args.task_id}")
    print(f"Speak naturally: {task['command']}", flush=True)
    audio, _ = MicrophoneSource().read_utterance()
    padding = np.zeros(4_800, dtype=np.float32)
    complete = np.concatenate((padding, audio, padding))
    pcm = (np.clip(complete, -1.0, 1.0) * 32767).astype("<i2")
    output = Path("tests/fixtures/audio") / f"{args.task_id}.wav"
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(pcm.tobytes())
    if output.stat().st_size >= 300_000:
        output.unlink()
        print("recording exceeds the 300 KB fixture limit", file=sys.stderr)
        return 1
    print(f"Wrote {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
