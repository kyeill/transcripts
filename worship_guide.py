"""Parse a Christ Church "Worship Guide" PDF into an ordered list of service items.

The guide is a real-text PDF (not scanned) laid out as one line per element:
a label (Prelude / Welcome / Call to Worship / Sermon / ...) optionally
followed on the same line by a scripture reference and/or a leader's name,
then zero or more lines of body text (scripture quotes, lyrics, sermon
outline points) before the next labelled line starts.

Two label vocabularies drive classification. Everything else prefixed with
"*" and not matching either is a bare hymn/song title (the guide's normal
way of listing a congregational song - no "Hymn:" prefix is printed).
"""
import re
from dataclasses import dataclass, field

import pypdf

MUSIC_LABELS = ["Prelude", "Offertory", "Doxology", "Postlude"]

SPEECH_LABELS = [
    "Welcome",
    "Silent meditation",
    "Call to Worship",
    "Invocation",
    "Call to Confession",
    "Prayer of confession",
    "Declaration of forgiveness",
    "Scripture reading",
    "Sermon",
    "Prayer",
    "Living out our faith",
    "Missionary Greeting",
    "Prayers of the People",
    "Benediction",
    # an occasional section: new members take their vows before the
    # congregation, sometimes with baptisms, and it can run ten minutes
    "Reception of New Members and Baptisms",
    "Reception of New Members",
    "Baptism",
]

# longest-label-first so "Prayer of confession" matches before "Prayer".
# Matched case-insensitively: the guide's capitalisation drifts week to week
# ("Call to confession" one Sunday, "Declaration of Forgiveness" another), and
# a heading that fails to match doesn't just lose its own section - it gets
# swallowed into the body of the section above it, taking that section's
# anchor text with it.
_ALL_LABELS = sorted(MUSIC_LABELS + SPEECH_LABELS, key=len, reverse=True)
_LABEL_RE = re.compile(
    r"^(" + "|".join(re.escape(l) for l in _ALL_LABELS) + r")\b\s*(.*)$", re.I
)
# whatever spelling the guide used maps back to the one the rest of the code knows
_CANONICAL_LABEL = {label.lower(): label for label in _ALL_LABELS}
# One section under one heading in the document, however the guide splits it.
_CANONICAL_LABEL["reception of new members and baptisms"] = "Reception of New Members"


def label_key(label):
    """Labels are compared case-insensitively throughout.

    The guide's capitalisation drifts week to week, so nothing downstream
    should key off it. Note this applies to matching *known words* only -
    where capitalisation is itself the evidence (an ALL-CAPS divider, a
    person's name, a book of the Bible) the patterns stay case-sensitive
    deliberately, because lowercasing them would match almost anything.
    """
    return (label or "").casefold()

_SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Z’' ,]{6,}$")

# recurring footer boilerplate that isn't part of the order of service
_BOILERPLATE_RE = re.compile(
    r"^(Please rise in body|Prayer Partners are available|on your heart:)", re.I
)

# A per-service logistics note ("8:30 service  On the final verse of the song
# below, 4- to 5-year-olds are invited...") printed under the declaration of
# forgiveness. It runs until the next item, and left in, it drowns the verse
# above it - which then never matches the audio of it being read.
_SERVICE_NOTE_RE = re.compile(r"^\d{1,2}:\d{2}\s+service\b", re.I)

_WORD = r"[A-Z][A-Za-z'.]*"
# tried first: a role title anchors the name unambiguously, so it can't
# swallow trailing words of a sermon/song title that also happen to be
# capitalized (e.g. "Putting God to the Test Pastor Andrew VanderMaas").
_NAME_WITH_ROLE_RE = re.compile(rf"((?:Pastor|Elder|Dr\.|Rev\.)\s+{_WORD}(?:\s+{_WORD}){{0,3}})\s*$")
# fallback for names with no role prefix, e.g. "Greg and Ingrid Orr"
_NAME_ANY_RE = re.compile(rf"({_WORD}(?:\s+(?:and\s+)?{_WORD}){{1,4}})\s*$")


@dataclass
class ServiceItem:
    kind: str  # "music" | "speech" | "unknown"
    label: str
    title: str = ""
    speaker: str = ""
    reference: str = ""
    body: list = field(default_factory=list)
    # a heading not in the known lists, picked up by how it is styled
    unfamiliar: bool = False

    @property
    def text(self):
        # rejoin words the PDF broke across lines ("unrigh- teousness")
        return re.sub(r"([a-z])- ([a-z])", r"\1\2", " ".join(self.body)).strip()


_SMALL_WORDS = {"of", "the", "and", "to", "for", "in", "a", "an", "on", "at", "with"}
_ROLE_WORDS = re.compile(r"\b(?:Pastor|Elder|Director|Minister|Ministries|Assistant)\b")


def looks_like_heading(line):
    """A guide line styled like a section heading ("Reception of New Members").

    Title Case with at least one small joining word, short, and none of the
    punctuation or digits that body text, names and scripture carry. Song
    lyrics are sentence case, so they don't qualify.
    """
    words = line.split()
    if not 2 <= len(words) <= 7 or re.search(r"[\d.,;:!?()@]", line):
        return False
    if _ROLE_WORDS.search(line):
        return False  # a staff title under a name, not a section
    return (
        words[0][0].isupper()
        and all(w in _SMALL_WORDS or w[0].isupper() for w in words)
        and any(w in _SMALL_WORDS for w in words)
    )


def _extract_pages(pdf_path):
    reader = pypdf.PdfReader(pdf_path)
    return [p.extract_text() or "" for p in reader.pages]


def _order_of_service_lines(pages):
    """Lines from the 'Sunday Worship' page through the musician credits."""
    started = False
    lines = []
    for page_text in pages:
        if not started:
            if "Sunday Worship" in page_text:
                started = True
            else:
                continue
        for raw in page_text.splitlines():
            line = raw.strip()
            if not line:
                continue
            lines.append(line)
        if "Worship musician" in page_text:
            break
    return lines


def _split_name_and_reference(remainder):
    """Pull a trailing leader name off a label's remainder, if one is printed."""
    speaker = ""
    m = _NAME_WITH_ROLE_RE.search(remainder) or _NAME_ANY_RE.search(remainder)
    if m:
        speaker = m.group(1).strip()
        remainder = remainder[: m.start()].strip()
    return remainder, speaker


def parse_worship_guide(pdf_path):
    pages = _extract_pages(pdf_path)
    lines = _order_of_service_lines(pages)

    items = []
    current = None
    in_note = False

    for line in lines:
        if line.startswith("Sunday Worship"):
            continue
        if line == "Worship musician":
            break
        stripped = line.lstrip("*").strip()

        if _SECTION_HEADER_RE.match(stripped):
            continue  # e.g. "GOD CALLS US TO WORSHIP" - a divider, not an item
        if _BOILERPLATE_RE.match(stripped):
            continue

        if _SERVICE_NOTE_RE.match(stripped):
            in_note = True  # everything up to the next item is the note
            continue

        m = _LABEL_RE.match(stripped)

        if m:
            in_note = False
            label = _CANONICAL_LABEL[m.group(1).lower()]
            remainder = m.group(2).strip()
            kind = "music" if label in MUSIC_LABELS else "speech"
            speaker = ""
            if kind == "speech":
                remainder, speaker = _split_name_and_reference(remainder)
            current = ServiceItem(kind=kind, label=label, title=remainder, speaker=speaker)
            items.append(current)
        elif line.startswith("*"):
            # bare congregational song title, e.g. "* All Creatures of Our God and King  Trinity Hymnal 115"
            in_note = False
            current = ServiceItem(kind="music", label="Song", title=stripped)
            items.append(current)
        elif (
            current is not None
            and current.kind == "speech"
            and label_key(current.label) != "sermon"
            and looks_like_heading(stripped)
        ):
            # An occasional section with a heading not listed above ("Reception
            # of New Members" before it was added). Left as body text it would
            # silently merge into the section before; as its own item it gets
            # its own place in the timeline and a flag for checking.
            current = ServiceItem(kind="speech", label=stripped, unfamiliar=True)
            items.append(current)
        elif current is not None and not in_note:
            current.body.append(line)
        # else: stray line before the first recognized item - drop it

    return items


if __name__ == "__main__":
    import sys

    for item in parse_worship_guide(sys.argv[1]):
        print(f"[{item.kind:6}] {item.label:22} title={item.title!r} speaker={item.speaker!r}")
        if item.body:
            print(f"          body: {item.text[:80]!r}")
