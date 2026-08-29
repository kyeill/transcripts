import argparse
import json
import os
import tempfile
from datetime import datetime, timezone

from fetch_episode import episode_assets, latest_episode_url
from worship_guide import parse_worship_guide
from transcribe import transcribe
from align import align
from render import render

OUTPUT_DIR = "output"


def run(episode_url=None):
    if episode_url is None:
        episode_url = latest_episode_url()

    slug = episode_url.rstrip("/").rsplit("/", 1)[-1]
    out_path = os.path.join(OUTPUT_DIR, f"{slug}.docx")
    if os.path.exists(out_path):
        print(f"{out_path} already exists - this week's episode is already done, skipping")
        return None

    title, audio_url, pdf_url = episode_assets(episode_url)
    print(f"episode: {title}\naudio: {audio_url}\nworship guide: {pdf_url}")

    if not pdf_url:
        raise RuntimeError(f"no worship guide found for {episode_url}; can't identify speakers")

    with tempfile.TemporaryDirectory() as tmp:
        import requests

        pdf_path = os.path.join(tmp, "guide.pdf")
        r = requests.get(pdf_url)
        r.raise_for_status()
        with open(pdf_path, "wb") as f:
            f.write(r.content)
        items = parse_worship_guide(pdf_path)

    print(f"parsed {len(items)} order-of-service items")

    segments, audio_duration = transcribe(audio_url)
    print(f"transcribed {len(segments)} segments across {audio_duration / 60:.1f} min")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # Diagnostic dump of the raw timestamped transcript. Transcription is the
    # only slow step (~15min) while alignment is instant, so keeping this lets
    # alignment be re-tuned offline against real timings in seconds rather than
    # re-transcribing the service for every attempt. Deliberately gitignored
    # and published as a build artifact instead: it is working data, not a
    # deliverable, and unlike the document it still contains the sung sections.
    with open(os.path.join(OUTPUT_DIR, f"{slug}.segments.json"), "w", encoding="utf-8") as f:
        json.dump({"duration": audio_duration, "segments": segments}, f, indent=1)

    blocks = align(items, segments, audio_duration=audio_duration)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    render(title, date_str, blocks, out_path)
    print(f"wrote {out_path}")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="specific episode URL; defaults to the latest")
    args = parser.parse_args()
    run(args.url)
