# Authors

Clavix was designed and built by four students of the **Department of Artificial Intelligence,
Mukesh Patel School of Technology Management & Engineering (MPSTME), NMIMS University, Mumbai, India**.

Each member owned one module and built it against a frozen interface contract, so the four modules
could be developed in parallel and integrated through reviewed pull requests.

| | Member | Role | Owns |
|:-:|--------|------|------|
| 🧭 | **Krish Kamra** | Architecture, core and integration lead | Agent state machine, LLM planner, dispatcher, verifier, benchmark and evaluation, CI, documentation |
| 🎙️ | **Shreyash Pathak** | Acoustic ingestion | Energy endpointing, Faster-Whisper transcription, audio fixtures (`clavix/modules/acoustic_whisper.py`) |
| 🧩 | **Varun Agrawal** | Structural grounding | Windows UI Automation search and invocation (`clavix/modules/grounding_uia.py`) |
| 🔊 | **Aditya Angane** | Feedback and safety HUD | Speech worker, glass HUD, abort hotkey (`clavix/modules/feedback_tts.py`, `hud.py`, `hud_web/`) |

The roadmap in [docs/ROADMAP.md](docs/ROADMAP.md) records who built what and when.

## Acknowledgements

Clavix stands on excellent open-source work: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
and OpenAI's Whisper models, [Ollama](https://ollama.com) and Meta's Llama 3,
[uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows),
[pywebview](https://pywebview.flowrl.com), [pyttsx3](https://github.com/nateshmbhat/pyttsx3),
[Pydantic](https://docs.pydantic.dev) and the [uv](https://docs.astral.sh/uv/) /
[Ruff](https://docs.astral.sh/ruff/) toolchain.
