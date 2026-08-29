import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone

from fetch_episode import episode_assets, latest_episode_url
from worship_guide import parse_worship_guide
from align import align
from render import render

# transcribe is imported inside run(): it pulls in faster-whisper, which has no
# Windows-ARM64 wheel, and importing it at module level would make this file
# unimportable on Kyle's machine even for the parts that need no transcription.

OUTPUT_DIR = "output"

# The document stops once the sermon is over. What follows is the closing
# prayer and a song, then announcements and the missionary greeting, none of
# which Kyle wants transcribed. The whole service is still aligned first: the
# sermon's end is only known from where the next item starts, and the closing
# prayer is not a separate line in the guide, so it stays inside the sermon.
LAST_LABEL = "Sermon"


_AMEN_RE = re.compile(r"\bamen\b", re.I)
CLOSING_PRAYER_LOOKAHEAD = 600  # seconds past the sermon to look for its "amen"


def through_sermon(blocks):
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].label == LAST_LABEL:
            return blocks[: i + 1]
    return blocks  # no sermon found - keep everything rather than emit nothing


def extend_to_closing_amen(blocks, segments):
    """Run the final block on to the end of the sermon's closing prayer.

    The guide gives that prayer no line of its own, so it belongs to the
    sermon - but the song after it is sung over accompaniment and so leaves no
    silence, and with nothing in the audio marking the change the search tends
    to end the sermon a few minutes early. A prayer ends on "amen", which is a
    dependable close, so the block is extended to the first one after it and
    cut there - which also leaves the song's opening line out.
    """
    if not blocks or not segments or blocks[-1].kind != "speech":
        return blocks

    last = blocks[-1]
    limit = last.end + CLOSING_PRAYER_LOOKAHEAD
    trailing = []
    for segment in segments:
        if segment["end"] <= last.end or segment["start"] > limit:
            continue
        found = _AMEN_RE.search(segment["text"])
        if found:
            trailing.append(segment["text"][: found.end()])
            last.text = " ".join([last.text, *trailing]).strip()
            last.end = segment["end"]
            break
        trailing.append(segment["text"])
    return blocks  # no "amen" in range - leave the boundary where the search put it


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

    from transcribe import transcribe

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

    blocks = through_sermon(align(items, segments, audio_duration=audio_duration))
    blocks = extend_to_closing_amen(blocks, segments)
    print(f"keeping {len(blocks)} sections, through the end of the {LAST_LABEL.lower()}")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    render(title, date_str, blocks, out_path)
    print(f"wrote {out_path}")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="specific episode URL; defaults to the latest")
    args = parser.parse_args()
    run(args.url)
