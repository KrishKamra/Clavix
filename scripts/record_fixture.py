"""Record benchmark commands into mono 16 kHz 16-bit PCM WAV fixtures.

Examples::

    uv run python scripts/record_fixture.py T1-01          # speak one command
    uv run python scripts/record_fixture.py --all          # speak all ten in turn
    uv run python scripts/record_fixture.py --all --synthesize   # Windows SAPI voice (placeholder)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from clavix.modules.acoustic_whisper import MicrophoneSource

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "configs" / "benchmark_suite.json"
OUT_DIR = ROOT / "tests" / "fixtures" / "audio"
SAMPLE_RATE = 16_000
PAD = np.zeros(int(0.3 * SAMPLE_RATE), dtype=np.float32)
MAX_BYTES = 300_000
DEFAULT_VOICE = "Microsoft Zira Desktop"
# Spoken forms for words SAPI mispronounces ("github" comes out as "guess up").
SAPI_PRONUNCIATION = {"github": "git hub"}

# Reads the text, voice and output path from the environment so no quoting is needed.
_SAPI_SCRIPT = """
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice($env:CLAVIX_VOICE)
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($env:CLAVIX_OUT, $fmt)
$s.Speak($env:CLAVIX_TEXT)
$s.Dispose()
"""


def record(source: MicrophoneSource) -> np.ndarray:
    """Capture one utterance; its 300 ms pre-roll is the leading pad."""
    audio, _ = source.read_utterance()
    return np.concatenate((audio, PAD[: int(0.1 * SAMPLE_RATE)]))  # 200 ms tail + 100 ms


def synthesize(text: str, voice: str) -> np.ndarray:
    """Speak ``text`` with a Windows SAPI voice; trim its silence and pad 300 ms each side."""
    if sys.platform != "win32":
        raise SystemExit("--synthesize needs Windows (System.Speech)")
    text = " ".join(SAPI_PRONUNCIATION.get(word, word) for word in text.split())
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tts.wav"
        env = {**os.environ, "CLAVIX_TEXT": text, "CLAVIX_VOICE": voice, "CLAVIX_OUT": str(out)}
        subprocess.run(  # noqa: S603 - fixed script; user text goes through the environment
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI_SCRIPT], env=env, check=True
        )
        with wave.open(str(out), "rb") as wav_file:
            pcm = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype="<i2")
    audio = pcm.astype(np.float32) / 32768.0
    voiced = np.flatnonzero(np.abs(audio) > 1e-3)
    if voiced.size:
        audio = audio[voiced[0] : voiced[-1] + 1]
    return np.concatenate((PAD, audio, PAD))


def write_wav(path: Path, audio: np.ndarray) -> int:
    """Write 16-bit PCM; return the file size, or -1 (and delete it) if over the limit."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm.tobytes())
    size = path.stat().st_size
    if size > MAX_BYTES:
        path.unlink()
        return -1
    return size


def main() -> int:
    """Record (or synthesize) the requested tasks' commands."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("task_id", nargs="?", help="task ID from configs/benchmark_suite.json")
    parser.add_argument("--all", action="store_true", help="every task in the suite")
    parser.add_argument("--device", help="sounddevice input device (default: system default)")
    parser.add_argument("--synthesize", action="store_true", help="use a Windows SAPI voice, not the mic")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"SAPI voice (default: {DEFAULT_VOICE})")
    args = parser.parse_args()
    if args.all == (args.task_id is not None):
        parser.error("give exactly one of TASK_ID or --all")

    tasks = json.loads(SUITE.read_text(encoding="utf-8"))["tasks"]
    if not args.all:
        tasks = [task for task in tasks if task["task_id"] == args.task_id]
        if not tasks:
            parser.error(f"unknown task ID: {args.task_id}")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    source = MicrophoneSource(input_device=device)
    for task in tasks:
        output = OUT_DIR / f"{task['task_id']}.wav"
        if args.synthesize:
            audio = synthesize(task["command"], args.voice)
        else:
            print(f"\n[{task['task_id']}] Wait for 'listening', then say: {task['command']}", flush=True)
            audio = record(source)
        size = write_wav(output, audio)
        if size < 0:
            print(f"{output.name}: over the {MAX_BYTES // 1000} KB fixture limit, not saved", file=sys.stderr)
            return 1
        print(f"Wrote {output.relative_to(ROOT)} ({size} bytes, {len(audio) / SAMPLE_RATE:.2f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
