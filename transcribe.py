"""Submit a publicly-hosted audio file to AssemblyAI and wait for the transcript.

The audio is never downloaded here - AssemblyAI fetches the URL itself, so
this never touches the ~60-90MB service recording locally or in CI.
"""
import os
import time

import requests

BASE = "https://api.assemblyai.com/v2"
POLL_SECONDS = 15


def transcribe(audio_url, api_key=None):
    api_key = api_key or os.environ["ASSEMBLYAI_API_KEY"]
    headers = {"authorization": api_key}

    resp = requests.post(
        f"{BASE}/transcript",
        json={"audio_url": audio_url, "speaker_labels": True},
        headers=headers,
    )
    resp.raise_for_status()
    transcript_id = resp.json()["id"]

    while True:
        poll = requests.get(f"{BASE}/transcript/{transcript_id}", headers=headers)
        poll.raise_for_status()
        result = poll.json()
        status = result["status"]
        if status == "completed":
            return result
        if status == "error":
            raise RuntimeError(f"AssemblyAI transcription failed: {result['error']}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    import sys
    import json

    result = transcribe(sys.argv[1])
    print(json.dumps(result, indent=2)[:2000])
