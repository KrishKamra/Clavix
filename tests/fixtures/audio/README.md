# Acoustic benchmark recordings

The benchmark requires one natural recording for each task ID in
`configs/benchmark_suite.json`. Record them from the repository root with
`python scripts/record_fixture.py <task_id>`. The script uses the default input
device and writes 16 kHz mono 16-bit PCM with 300 ms of padding on both sides.

Recording device, speaker identity, and room conditions must describe the actual
recording session. This checkout did not contain audio fixtures or recording
metadata, so those details and the ten recordings are pending; do not attribute
synthetic audio or guessed metadata to a speaker/device.
