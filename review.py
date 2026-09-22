"""Plain-language notes on where a transcript's section splits are guesses.

Most splits rest on firm evidence: the guide's printed text matched against
what was read aloud, or a closing formula ("Amen", "the word of the Lord").
Where neither was there to go on, the split came from pauses and typical
lengths alone. Those are the spots worth a human look, so they are listed at
the top of the document rather than left for the reader to find.
"""
import re

from align import NEVER_SPOKEN_LABELS
from worship_guide import label_key

_NEVER_SPOKEN = {label_key(l) for l in NEVER_SPOKEN_LABELS}

# fewer words than this in a spoken section usually means it went missing,
# or its words ended up in the section next to it
FEW_WORDS = 15
# spoken words inside a song's window that no section kept; below this it is
# just a song being introduced or a stray line
DROPPED_WORDS = 40
# Only a real speaking pace counts. Sung lines come out slower, and the
# transcriber's glitches - a lyric repeated in a third of a second - are too
# short to trust either way.
SPEAKING_RATE = 1.8  # words per second
MIN_LINE_SECONDS = 1.5

def _dropped_words(block, segments, kept_text):
    """Spoken (not sung) words inside a song's window that no section kept."""
    count, previous = 0, None
    for s in segments:
        if not block.start <= s["start"] < block.end:
            continue
        text = s["text"].strip()
        duration = s["end"] - s["start"]
        # the transcriber sometimes repeats one line dozens of times in a
        # fraction of a second ("Amen. Amen. Amen."); that isn't real speech
        if not text or text == previous:
            continue
        previous = text
        words = len(text.split())
        if duration < MIN_LINE_SECONDS or words / duration < SPEAKING_RATE:
            continue
        if text not in kept_text:
            count += words
    return count


def review_notes(items, blocks, segments, closing_labels):
    """Notes for the blocks kept in the document (the service through the sermon).

    items are the guide items the blocks were aligned from, in the same order.
    """
    notes = []
    kept_text = " ".join(b.text for b in blocks)

    for item, block in zip(items, blocks):
        label = block.label
        if block.kind == "music":
            dropped = _dropped_words(block, segments, kept_text)
            if dropped >= DROPPED_WORDS:
                name = f"“{block.title}”" if block.title else "a song"
                notes.append(
                    f"About {dropped} spoken words during {name} were left out as part "
                    f"of the song. If something was said there, it is missing."
                )
            continue
        if label_key(label) in _NEVER_SPOKEN:
            continue

        words = len(block.text.split())
        if words < FEW_WORDS:
            notes.append(
                f"{label}: only {words} words. It may be missing, or its words may "
                f"be in the section before or after it."
            )
        if block.anchor_eligible and not block.anchored:
            notes.append(
                f"{label}: the text printed in the guide could not be found in the "
                f"recording, so where this section starts and ends is a best guess."
            )
        if label_key(label) in closing_labels and not block.closed:
            ending = "the word of the Lord" if "reading" in label_key(label) else "Amen"
            notes.append(
                f"{label}: the usual closing (“{ending}”) was not heard, so where "
                f"it ends is a best guess."
            )

        if item.unfamiliar:
            notes.append(
                f"{label}: a heading this program has not seen before, so it was "
                f"given its own section and where it starts and ends is a best guess."
            )
    return notes
