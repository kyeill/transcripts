import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone

from fetch_episode import episode_assets, latest_episode_url
from worship_guide import label_key, parse_worship_guide
from align import NEVER_SPOKEN_LABELS, align

_NEVER_SPOKEN = {label_key(l) for l in NEVER_SPOKEN_LABELS}
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
_MERGED = {label_key(l) for l in MERGED_LABELS}

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
_ENDS = {label_key(k): v for k, v in SECTION_ENDS.items()}

# A section cannot close this soon after it opens. "Amen" is common enough in
# a service that the sermon would otherwise end on one said in the moments
# after it starts - a congregation echoing the prayer before it, say - which
# collapses a 40 minute sermon to a single word.
SECTION_MIN_SECONDS = {"Sermon": 600}
_MIN_SECONDS = {label_key(k): v for k, v in SECTION_MIN_SECONDS.items()}

# spoken right after the closing formula, and belongs with it. The
# congregation's "thanks be to God" response is never picked up by the
# recording, so it can't be used.
_TRAILING_CUE_RE = re.compile(r"^[\s.,:;\"']*(?:please\s+)?be seated\.?", re.I)


def drop_merged_sections(items):
    return [item for item in items if label_key(item.label) not in _MERGED]


def through_sermon(blocks):
    for i in range(len(blocks) - 1, -1, -1):
        if label_key(blocks[i].label) == label_key(LAST_LABEL):
            return blocks[: i + 1]
    return blocks  # no sermon found - keep everything rather than emit nothing


def apply_section_ends(blocks, segments):
    """End each scripted section where it is actually spoken to a close."""
    if not blocks or not segments:
        return blocks

    splits = {}  # segment index -> (owning block index, character offset)
    cursor_time, cursor_seg = 0.0, 0
    for bi, block in enumerate(blocks):
        pattern = _ENDS.get(label_key(block.label)) if block.kind == "speech" else None
        if pattern is None:
            cursor_time = max(cursor_time, block.end)
            continue

        earliest = cursor_time + _MIN_SECONDS.get(label_key(block.label), 0)
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

    _start_at_speaker_introduction(blocks, segments)
    _reflow(blocks, segments, splits)
    return blocks


READING_LABEL = "Scripture reading"
INTRO_LOOKBACK_SECONDS = 600
_INTRO_CONTIGUOUS_GAP = 2.0
_BOOK_CHAPTER_RE = re.compile(r"\b((?:[1-3]\s)?[A-Z][a-z]+)\.?\s+(\d{1,3})")


def pull_reading_introduction(blocks, segments):
    """Put the reader's announcement of the passage back with the reading.

    The passage is announced before the congregation sings and only read
    afterwards, so the announcement lands minutes earlier and gets swallowed by
    the song in between. There is no fixed phrasing to look for - "today's
    reading", "continuing in Acts" - but whatever the wording it names the
    book, and the guide already gives the book and chapter. Only the text
    moves: the block keeps its window, so the song still sits between them in
    the timeline and still gets its own line.
    """
    for block in blocks:
        if label_key(block.label) != label_key(READING_LABEL) or not block.text:
            continue
        reference = _BOOK_CHAPTER_RE.search(block.title or "")
        if not reference:
            continue

        book, chapter = reference.group(1), reference.group(2)
        # the book with its chapter is the safer match; the book on its own is
        # the fallback for "continuing in Acts"
        for cue in (
            re.compile(rf"\b{re.escape(book)}\b[^.]{{0,20}}\b{re.escape(chapter)}\b", re.I),
            re.compile(rf"\b{re.escape(book)}\b", re.I),
        ):
            found = [
                i
                for i, s in enumerate(segments)
                if block.start - INTRO_LOOKBACK_SECONDS <= s["start"] < block.start
                and cue.search(s["text"])
            ]
            if not found:
                continue
            start = found[-1]  # the announcement immediately before the reading
            intro = [segments[start]["text"].strip()]
            for nxt in range(start + 1, len(segments)):
                if segments[nxt]["start"] >= block.start:
                    break
                if segments[nxt]["start"] - segments[nxt - 1]["end"] > _INTRO_CONTIGUOUS_GAP:
                    break  # the singing starts here; stop before it
                intro.append(segments[nxt]["text"].strip())
            block.text = " ".join([*intro, block.text]).strip()
            break
    return blocks


# The guide names who leads these, and they introduce themselves before
# reading ("My name is Dan Churchwell", "I am Brian Burke"), which lands ahead
# of the section's printed text and would otherwise stay with the welcome.
SPEAKER_INTRO_LABELS = {"Call to Worship"}
SPEAKER_INTRO_LOOKBACK = 300
_SPEAKER_INTRO = {label_key(l) for l in SPEAKER_INTRO_LABELS}


def _start_at_speaker_introduction(blocks, segments):
    """Move a section's start back to where its reader gives their name."""
    for i, block in enumerate(blocks):
        if i == 0 or label_key(block.label) not in _SPEAKER_INTRO or not block.speaker:
            continue
        surname = block.speaker.split()[-1]
        if len(surname) < 4:  # too short to be distinctive
            continue

        cue = re.compile(rf"\b{re.escape(surname)}\b", re.I)
        spoken = [
            s["start"]
            for s in segments
            if block.start - SPEAKER_INTRO_LOOKBACK <= s["start"] < block.start
            and cue.search(s["text"])
        ]
        if not spoken:
            continue
        moment = spoken[-1]

        # walk back to whichever block currently holds that moment
        holder = next(
            (j for j in range(i - 1, -1, -1) if blocks[j].start <= moment < blocks[j].end),
            None,
        )
        if holder is None or any(b.kind == "music" for b in blocks[holder:i]):
            continue  # never collapse a song to make room
        for between in blocks[holder:i]:
            between.end = min(between.end, moment)
        block.start = moment


# A sung stretch the transcriber ran together with the words on either side:
# few words spread over minutes. Those words were spoken, not sung.
MUSIC_SPILLOVER_RATE = 1.0  # words per second, well under a speaking pace


def _music_spillover(blocks, segments):
    """Words trapped inside a music block that open the section after it.

    Music sections carry no text, so anything spoken during one is dropped.
    Usually that is right. But when the transcriber runs the sung minutes
    together with the announcement before them and the first words of the next
    section, the last of those words are that section's opening - a prayer
    beginning while the closing chord is still ringing - and dropping them
    loses the start of it.
    """
    owners = {}
    for bi, block in enumerate(blocks[:-1]):
        if block.kind != "music":
            continue
        inside = [
            si
            for si, s in enumerate(segments)
            if block.start <= s["start"] < block.end and s["text"].strip()
        ]
        if not inside:
            continue

        # the sung stretch itself: minutes long, almost no words in it
        sung = [
            si
            for si in inside
            if segments[si]["end"] > segments[si]["start"]
            and len(segments[si]["text"].split())
            / (segments[si]["end"] - segments[si]["start"])
            < MUSIC_SPILLOVER_RATE
        ]
        if not sung:
            continue  # the singing was transcribed properly; nothing is trapped

        # from the singing onwards the words are the next section starting up
        for si in inside:
            if si >= sung[-1]:
                owners[si] = bi + 1
    return owners


def _segment_owner(blocks, bi, segment):
    """Whose words these are, when a segment straddles a boundary.

    Singing is dropped by the transcriber, so words inside a music block were
    spoken rather than sung - and a segment running past the end of that block
    is the opening of the next section, caught in the same breath as the last
    announcement. Leaving it with the music throws it away, since music
    sections carry no text.
    """
    if bi < len(blocks) - 1 and blocks[bi].kind == "music" and segment["end"] > blocks[bi].end:
        bi += 1
    return _speaking_owner(blocks, bi)


def _speaking_owner(blocks, bi):
    """The block that text at this position belongs to.

    A silent section can't own any: the silent meditation is read without
    speaking, so a greeting caught inside its window is really the start of
    whatever comes next - the elder introducing himself before the call to
    worship, for instance.
    """
    while bi < len(blocks) - 1 and label_key(blocks[bi].label) in _NEVER_SPOKEN:
        bi += 1
    return bi


def _reflow(blocks, segments, splits):
    """Close the blocks back up and re-collect their text around the cuts."""
    previous_end = 0.0
    for block in blocks:
        block.start = previous_end
        block.end = max(block.end, previous_end)
        previous_end = block.end

    spillover = _music_spillover(blocks, segments)
    texts = [[] for _ in blocks]
    bi = 0
    for si, segment in enumerate(segments):
        while bi < len(blocks) - 1 and segment["start"] >= blocks[bi].end:
            bi += 1
        if si in spillover:
            texts[_speaking_owner(blocks, spillover[si])].append(segment["text"])
            continue
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
        texts[_segment_owner(blocks, bi, segment)].append(segment["text"])

    for block, parts in zip(blocks, texts):
        keeps_text = block.kind == "speech" and label_key(block.label) not in _NEVER_SPOKEN
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
    blocks = pull_reading_introduction(blocks, segments)
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
