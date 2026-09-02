import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone

from fetch_episode import episode_assets, latest_episode_url
from worship_guide import parse_worship_guide
from align import NEVER_SPOKEN_LABELS, align
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


# Guide lines that aren't a section of their own in practice. The prayer of
# confession is spoken as part of the confession, so it belongs under that
# heading rather than getting one of its own.
MERGED_LABELS = {"Prayer of confession"}

# This liturgy is scripted, and these sections each finish on a fixed spoken
# formula. That is far better evidence than anything in the audio: the leader
# runs straight on into the next section at a normal speaking pace, so no
# silence marks the change and only the words give it away. Whatever follows
# the formula in the same breath belongs to the section after.
_AMEN = re.compile(r"\bamen\b", re.I)
SECTION_ENDS = {
    "Invocation": _AMEN,
    "Call to Confession": _AMEN,  # the merged prayer of confession ends it
    # Only as its own sentence. The reader's closing "The word of the Lord."
    # is a separate utterance, but the same words can appear inside the passage
    # being read - Acts 15:35 ends "teaching and preaching the word of the
    # Lord" - and matching that cuts the reading off in the middle of itself.
    "Scripture reading": re.compile(r"(?:^|[.!?]\s+)the word of the Lord\b", re.I),
    "Prayer": _AMEN,
    "Sermon": _AMEN,  # the closing prayer, which the guide gives no line of its own
}

# A section cannot close this soon after it opens. "Amen" is common enough in
# a service that the sermon would otherwise end on one said in the moments
# after it starts - a congregation echoing the prayer before it, say - which
# collapses a 40 minute sermon to a single word.
SECTION_MIN_SECONDS = {"Sermon": 600}

# spoken right after the closing formula, and belongs with it. The
# congregation's "thanks be to God" response is never picked up by the
# recording, so it can't be used.
_TRAILING_CUE_RE = re.compile(r"^[\s.,:;\"']*(?:please\s+)?be seated\.?", re.I)


def drop_merged_sections(items):
    return [item for item in items if item.label not in MERGED_LABELS]


def through_sermon(blocks):
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].label == LAST_LABEL:
            return blocks[: i + 1]
    return blocks  # no sermon found - keep everything rather than emit nothing


def apply_section_ends(blocks, segments):
    """End each scripted section where it is actually spoken to a close."""
    if not blocks or not segments:
        return blocks

    splits = {}  # segment index -> (owning block index, character offset)
    cursor_time, cursor_seg = 0.0, 0
    for bi, block in enumerate(blocks):
        pattern = SECTION_ENDS.get(block.label) if block.kind == "speech" else None
        if pattern is None:
            cursor_time = max(cursor_time, block.end)
            continue

        earliest = cursor_time + SECTION_MIN_SECONDS.get(block.label, 0)
        hit = None
        for si in range(cursor_seg, len(segments)):
            if segments[si]["end"] <= earliest:
                continue
            found = pattern.search(segments[si]["text"])
            if found:
                hit = (si, found.end())
                break
        if hit is None:  # never said, or not transcribed - keep the inferred end
            cursor_time = max(cursor_time, block.end)
            continue

        si, offset = hit
        cue = _TRAILING_CUE_RE.match(segments[si]["text"][offset:])
        if cue:
            offset += cue.end()
        block.end = segments[si]["end"]
        splits[si] = (bi, offset)
        cursor_time, cursor_seg = block.end, si

    _reflow(blocks, segments, splits)
    return blocks


def _speaking_owner(blocks, bi):
    """The block that text at this position belongs to.

    A silent section can't own any: the silent meditation is read without
    speaking, so a greeting caught inside its window is really the start of
    whatever comes next - the elder introducing himself before the call to
    worship, for instance.
    """
    while bi < len(blocks) - 1 and blocks[bi].label in NEVER_SPOKEN_LABELS:
        bi += 1
    return bi


def _reflow(blocks, segments, splits):
    """Close the blocks back up and re-collect their text around the cuts."""
    previous_end = 0.0
    for block in blocks:
        block.start = previous_end
        block.end = max(block.end, previous_end)
        previous_end = block.end

    texts = [[] for _ in blocks]
    bi = 0
    for si, segment in enumerate(segments):
        while bi < len(blocks) - 1 and segment["start"] >= blocks[bi].end:
            bi += 1
        if si in splits:
            owner, offset = splits[si]
            head = segment["text"][:offset].strip()
            # the closing formula takes its punctuation with it, so the
            # remainder would otherwise open on a stray full stop
            tail = segment["text"][offset:].lstrip(" .,:;!?\"'").strip()
            if head:
                texts[_speaking_owner(blocks, owner)].append(head)
            if tail and owner + 1 < len(blocks):
                texts[_speaking_owner(blocks, owner + 1)].append(tail)
            bi = min(owner + 1, len(blocks) - 1)
            continue
        texts[_speaking_owner(blocks, bi)].append(segment["text"])

    for block, parts in zip(blocks, texts):
        keeps_text = block.kind == "speech" and block.label not in NEVER_SPOKEN_LABELS
        block.text = " ".join(parts).strip() if keeps_text else ""


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
        items = drop_merged_sections(parse_worship_guide(pdf_path))

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

    # section ends first, on the whole service: the cuts hand trailing words to
    # the following section, and the sermon's own end comes from the closing
    # prayer that follows it
    blocks = apply_section_ends(align(items, segments, audio_duration=audio_duration), segments)
    blocks = through_sermon(blocks)
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
