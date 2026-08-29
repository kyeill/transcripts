"""Match a whisper transcript (chronological, timestamped) to the worship
guide's ordered list of service items, so each stretch of audio gets the
right speaker label - and music gets dropped instead of showing up as
garbled sung lyrics.

Two passes:

1. Anchor pass - a handful of guide items carry real reference text (scripture
   quotes, the offertory's full lyrics). Fuzzy-match those against the
   transcript to pin down exact [start, end] times for those items.
2. Fill pass - every other item (no reference text: prayers, the sermon,
   announcements, hymns cited by hymnal number only, ...) gets an estimated
   time window, splitting the gap between two anchors proportionally by a
   rough per-label duration prior. Transcript segments are then bucketed into
   whichever item's window contains them.

Anchors make most of the guide exact; priors only fill the un-anchorable
gaps, and only ever affect where a boundary falls - never which text an
anchored item gets.
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# a body is only anchor-eligible if it's an actual scripture quotation
# (read verbatim) rather than a bulletin note, sermon outline, or logistics
# aside - which can be long too, but aren't what gets said word-for-word
_SCRIPTURE_REF_RE = re.compile(r"\b(?:[1-3]\s?)?[A-Z][a-z]+\.?\s+\d{1,3}\s?:\s?\d{1,3}")

# Items whose printed body is scripture but is NEVER read aloud - it's there
# for the congregation to read silently. Anchoring on these is guaranteed to
# be a false match: there is no audio for the text to match against, so the
# fuzzy search just pins whatever nearby speech scores least badly, and every
# later anchor inherits the error via the search cursor.
NEVER_SPOKEN_LABELS = {"Silent meditation"}

# rough seconds, used only to split time between two anchors when the
# items in between have no reference text to match against
DURATION_PRIORS = {
    "Prelude": 120,
    "Welcome": 60,
    "Silent meditation": 90,
    "Call to Worship": 60,
    "Song": 240,
    "Invocation": 60,
    "Call to Confession": 30,
    "Prayer of confession": 60,
    "Declaration of forgiveness": 30,
    "Scripture reading": 120,
    "Sermon": 2100,
    "Prayer": 90,
    "Living out our faith": 180,
    "Missionary Greeting": 120,
    "Prayers of the People": 180,
    "Offertory": 240,
    "Doxology": 60,
    "Benediction": 45,
    "Postlude": 90,
}
DEFAULT_PRIOR = 90

MIN_ANCHOR_TEXT_LEN = 30
# A genuinely read-aloud passage scores well above this against its printed
# text once digits are normalised away. 0.4 was far too permissive - on prose
# of similar length almost anything clears it, so false anchors were being
# accepted and then propagated by the search cursor.
MIN_ANCHOR_RATIO = 0.6
ANCHOR_LOOKAHEAD = 250  # segments to search ahead of the cursor
MAX_ANCHOR_SPAN = 80  # max segments an anchor can span


def _normalise(text):
    """Printed scripture carries verse numbers that are never read aloud;
    drop digits from both sides so they don't depress a true match's score."""
    return re.sub(r"\d+", " ", text.lower())


@dataclass
class AlignedBlock:
    kind: str
    label: str
    speaker: str = ""
    title: str = ""
    text: str = ""
    start: float = 0.0
    end: float = 0.0


def _find_anchor(item_text, segments, cursor):
    """Best contiguous segment range for item_text, searching from cursor."""
    best = None  # (ratio, i, j)
    limit = min(len(segments), cursor + ANCHOR_LOOKAHEAD)
    for i in range(cursor, limit):
        text = ""
        for j in range(i, min(i + MAX_ANCHOR_SPAN, len(segments))):
            text = (text + " " + segments[j]["text"]).strip()
            ratio = SequenceMatcher(None, _normalise(text), _normalise(item_text)).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, i, j)
            if len(text) > len(item_text) * 1.5:
                break  # already well past a plausible match length
    return best


def _is_anchor_eligible(item):
    """Song lyrics are always sung close to verbatim. For speech, only a
    printed scripture quotation is - a bulletin note or sermon outline can
    also be long, but isn't what actually gets said word-for-word."""
    if item.label in NEVER_SPOKEN_LABELS:
        return False
    if item.kind == "music":
        return len(item.text) >= MIN_ANCHOR_TEXT_LEN
    if len(item.text) < MIN_ANCHOR_TEXT_LEN:
        return False
    return bool(_SCRIPTURE_REF_RE.search(item.title) or _SCRIPTURE_REF_RE.search(item.text))


def _anchor_items(items, segments):
    """Return {item_index: (start_time, end_time)} for items with a good match."""
    anchors = {}
    cursor = 0
    for idx, item in enumerate(items):
        if not _is_anchor_eligible(item):
            continue
        result = _find_anchor(item.text, segments, cursor)
        if result is None:
            continue
        ratio, i, j = result
        if ratio < MIN_ANCHOR_RATIO:
            continue
        anchors[idx] = (segments[i]["start"], segments[j]["end"])
        cursor = j + 1
    return anchors


def _fill_windows(items, anchors, audio_duration):
    """Return {item_index: (start, end)} for every item, anchored or estimated."""
    windows = {}
    n = len(items)
    i = 0
    prev_end = 0.0
    anchor_positions = sorted(anchors)

    while i < n:
        if i in anchors:
            windows[i] = anchors[i]
            prev_end = anchors[i][1]
            i += 1
            continue

        # gap of consecutive unanchored items up to the next anchor (or the end)
        j = i
        while j < n and j not in anchors:
            j += 1
        next_start = anchors[j][0] if j < n else audio_duration

        gap_items = list(range(i, j))
        weights = [DURATION_PRIORS.get(items[k].label, DEFAULT_PRIOR) for k in gap_items]
        total_weight = sum(weights) or 1
        gap_duration = max(next_start - prev_end, 0)

        t = prev_end
        for k, w in zip(gap_items, weights):
            share = gap_duration * (w / total_weight)
            windows[k] = (t, t + share)
            t += share

        i = j
        prev_end = next_start

    return windows


def align(items, segments):
    if not segments:
        return [
            AlignedBlock(kind=item.kind, label=item.label, speaker=item.speaker, title=item.title)
            for item in items
        ]

    audio_duration = segments[-1]["end"]
    anchors = _anchor_items(items, segments)
    windows = _fill_windows(items, anchors, audio_duration)

    blocks = [
        AlignedBlock(
            kind=item.kind,
            label=item.label,
            speaker=item.speaker,
            title=item.title,
            start=windows[idx][0],
            end=windows[idx][1],
        )
        for idx, item in enumerate(items)
    ]

    # bucket segments into whichever item's window contains their midpoint
    seg_i = 0
    for block in blocks:
        texts = []
        while seg_i < len(segments) and (segments[seg_i]["start"] + segments[seg_i]["end"]) / 2 < block.end:
            if block.kind == "speech":
                texts.append(segments[seg_i]["text"])
            seg_i += 1
        block.text = " ".join(texts).strip()

    return blocks


if __name__ == "__main__":
    import sys

    from worship_guide import parse_worship_guide

    items = parse_worship_guide(sys.argv[1])
    # smoke test with no transcript - just prints the estimated skeleton
    for block in align(items, []):
        print(f"[{block.kind:6}] {block.label:22} speaker={block.speaker!r} title={block.title!r}")
