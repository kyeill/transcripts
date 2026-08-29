"""Download the service audio and transcribe it locally with faster-whisper.

Free and self-hosted: no API key, no per-run cost. CPU-only inference of a
~75 minute service takes roughly 30-90+ minutes depending on WHISPER_MODEL,
which is fine for a weekly job with hours of retry headroom (see
.github/workflows/weekly.yml). Runs on Linux (GitHub Actions or WSL) - the
`av` dependency has no Windows-ARM64 wheel, so this can't run on Kyle's local
machine; it's tested by triggering the real workflow.
"""
import os
import tempfile

import requests
from faster_whisper import WhisperModel

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "medium")


def _download(audio_url, dest):
    with requests.get(audio_url, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def transcribe(audio_url, model_size=MODEL_SIZE):
    """Return ({start, end, text} segments in order, total audio seconds).

    The duration matters as much as the segments: a service ends with a sung
    amen and an instrumental postlude, which produce no transcript at all, so
    taking the last spoken word as the end of the audio leaves those with
    nowhere to go and pushes everything before them out of place.
    """
    with tempfile.TemporaryDirectory() as tmp:
        audio_path = os.path.join(tmp, "service.mp3")
        _download(audio_url, audio_path)

        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, info = model.transcribe(audio_path, vad_filter=True)

        found = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
        return found, info.duration


if __name__ == "__main__":
    import sys
    import json

    found, duration = transcribe(sys.argv[1])
    print(f"{duration:.1f}s of audio, {len(found)} segments")
    print(json.dumps(found, indent=2))
