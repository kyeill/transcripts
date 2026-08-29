"""Match a whisper transcript (chronological, timestamped) to the worship
guide's ordered list of service items, so each stretch of audio gets the
right speaker label - and music gets dropped instead of showing up as
garbled sung lyrics.

Three passes:

1. Anchor pass - a few guide items print text that is genuinely performed
   word-for-word (scripture read aloud). Each is fuzzy-matched against the
   whole transcript to pin exact times.
2. Consistency pass - the chosen anchors must run in the same order as the
   guide, so a single bad match can't drag everything after it out of place.
3. Fill pass - everything else has no text to match on, so its boundaries
   come from where the audio is talking versus where it is quiet.

The fill pass leans on a quirk of the transcriber: whisper's voice-activity
filter drops singing almost entirely, so a hymn appears as a long silence.
Music therefore belongs in the silences and speech in the talking, which is a
far stronger signal than guessing how long anything ought to run.

Segments are then bucketed into whichever item's window contains them.
"""
import bisect
import math
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
# fuzzy search just pins whatever nearby speech scores least badly.
NEVER_SPOKEN_LABELS = {"Silent meditation"}

# rough seconds; only used to keep results plausible where the audio itself
# doesn't settle the question
DURATION_PRIORS = {
    "Prelude": 300,
    "Welcome": 90,
    "Silent meditation": 30,
    "Call to Worship": 60,
    "Song": 240,
    "Invocation": 60,
    "Call to Confession": 30,
    "Prayer of confession": 90,
    "Declaration of forgiveness": 30,
    "Scripture reading": 120,
    # Measured ~44min on 2026-08-23 including the closing prayer. This one
    # matters more than the rest: the document ends at the sermon, so setting
    # it short silently drops the end of the sermon and the prayer, while
    # setting it long only spills a little of the following song - which
    # renders as a title, not text. When in doubt, err long.
    "Sermon": 2400,
    "Prayer": 60,
    "Living out our faith": 180,
    "Missionary Greeting": 240,
    "Prayers of the People": 240,
    "Offertory": 240,
    "Doxology": 60,
    "Benediction": 60,
    "Postlude": 120,
}
DEFAULT_PRIOR = 90

MIN_ANCHOR_TEXT_LEN = 30
# Printed *lyrics* run to hundreds of characters. A short music body is the
# guide listing titles and credits ("Soon and Very Soon / arr. ..."), which is
# never sung - and worse, a title often IS spoken aloud later when the leader
# introduces it, so anchoring on one pins the wrong moment entirely.
MIN_MUSIC_ANCHOR_TEXT_LEN = 200
# A genuinely performed passage scores well above this against its printed
# text once digits are normalised away.
MIN_ANCHOR_RATIO = 0.6
MAX_ANCHOR_SPAN = 80  # max segments one anchor may span

MIN_GAP_SECONDS = 0.6  # a pause has to be this long to be a boundary candidate
MAX_CANDIDATES = 70  # clearest pauses per region, to bound the search
# Grid spacing has to stay fine. A coarse grid leaves the final minutes with
# no boundary at all, and the closing hymn and postlude - which follow the
# last spoken word and so create no pause of their own - then have nowhere to
# sit, which drags every earlier item out of position.
GRID_STEP_SECONDS = 20.0
MAX_GRID_POINTS = 240
# How strongly to insist that music lands on silence and speech on talking.
# Set well above the duration priors: the priors are guesses, this is measured.
TYPE_WEIGHT = 6.0


@dataclass
class AlignedBlock:
    kind: str
    label: str
    speaker: str = ""
    title: str = ""
    text: str = ""
    start: float = 0.0
    end: float = 0.0


def _normalise(text):
    """Printed scripture carries verse numbers that are never read aloud;
    drop digits from both sides so they don't depress a true match's score."""
    return re.sub(r"\d+", " ", text.lower())


def _is_silent_item(item):
    """Items expected to produce no speech: music, and silent reading."""
    return item.kind == "music" or item.label in NEVER_SPOKEN_LABELS


def _is_anchor_eligible(item):
    """Only text that is actually performed word-for-word can be an anchor."""
    if item.label in NEVER_SPOKEN_LABELS:
        return False
    if item.kind == "music":
        return len(item.text) >= MIN_MUSIC_ANCHOR_TEXT_LEN
    if len(item.text) < MIN_ANCHOR_TEXT_LEN:
        return False
    return bool(_SCRIPTURE_REF_RE.search(item.title) or _SCRIPTURE_REF_RE.search(item.text))


def _best_match(item_text, segments):
    """Best contiguous segment range for item_text over the whole transcript.

    Searched globally rather than forward-only from the last anchor: a
    forward cursor means one bad match hides every later one behind it.
    Ordering is imposed afterwards by _monotonic_anchors instead.
    """
    target = _normalise(item_text)
    best = None  # (ratio, i, j)
    for i in range(len(segments)):
        text = ""
        for j in range(i, min(i + MAX_ANCHOR_SPAN, len(segments))):
            text = (text + " " + segments[j]["text"]).strip()
            ratio = SequenceMatcher(None, _normalise(text), target).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, i, j)
            if len(text) > len(item_text) * 1.5:
                break  # already well past a plausible match length
    return best


def _monotonic_anchors(candidates):
    """Keep the best-scoring subset of anchors that runs in guide order.

    candidates: {item_index: (ratio, first_segment, last_segment)}

    The service happens in the order the guide prints, so anchors must too.
    Rather than trusting each match on its own, keep the highest-scoring
    chain that is consistent - which lets a strong anchor overrule a weak
    one that would have put it out of sequence.
    """
    keys = sorted(candidates)
    if not keys:
        return {}

    best_total = [0.0] * len(keys)
    prev = [-1] * len(keys)
    for a in range(len(keys)):
        ratio_a, first_a, _ = candidates[keys[a]]
        best_total[a] = ratio_a
        for b in range(a):
            _, _, last_b = candidates[keys[b]]
            if last_b < first_a and best_total[b] + ratio_a > best_total[a]:
                best_total[a] = best_total[b] + ratio_a
                prev[a] = b

    end = max(range(len(keys)), key=lambda k: best_total[k])
    chain = []
    while end != -1:
        chain.append(keys[end])
        end = prev[end]
    return {k: candidates[k] for k in chain}


def _anchor_items(items, segments):
    """Return {item_index: (start_time, end_time)} for confidently matched items."""
    candidates = {}
    for idx, item in enumerate(items):
        if not _is_anchor_eligible(item):
            continue
        match = _best_match(item.text, segments)
        if match is None or match[0] < MIN_ANCHOR_RATIO:
            continue
        candidates[idx] = match

    kept = _monotonic_anchors(candidates)
    return {
        idx: (segments[first]["start"], segments[last]["end"])
        for idx, (_, first, last) in kept.items()
    }


class SpeechClock:
    """Answers 'how many seconds of actual talking fall between a and b?'

    Used to test whether a proposed window looks like music or like speech.
    """

    def __init__(self, segments):
        self.starts = [s["start"] for s in segments]
        self.ends = [s["end"] for s in segments]
        self.cumulative = [0.0]
        for s in segments:
            self.cumulative.append(self.cumulative[-1] + max(s["end"] - s["start"], 0))

    def _talk_before(self, t):
        i = bisect.bisect_right(self.starts, t) - 1
        if i < 0:
            return 0.0
        total = self.cumulative[i]
        return total + max(min(t, self.ends[i]) - self.starts[i], 0)

    def between(self, a, b):
        return max(self._talk_before(b) - self._talk_before(a), 0.0)


def _boundary_candidates(segments, start_time, end_time, grid_step=0.0):
    """Times where a section plausibly changes, i.e. either edge of a pause.

    Both edges matter, not just the middle: a hymn's window should be able to
    start where the talking stops and end where it resumes, hugging the
    silence rather than straddling it.

    A plain grid is mixed in as well. Pauses alone leave stretches with no
    boundary at all - a closing hymn after the last spoken word produces no
    audio and so no pause - and with nowhere to put those items the search is
    forced to drag everything before them out of position.
    """
    found = []
    if segments and start_time < segments[0]["start"] < end_time:
        found.append((segments[0]["start"], segments[0]["start"] - start_time))
    for a, b in zip(segments, segments[1:]):
        size = b["start"] - a["end"]
        if size < MIN_GAP_SECONDS:
            continue
        for edge in (a["end"], b["start"]):
            if start_time < edge < end_time:
                found.append((edge, size))
    if segments and start_time < segments[-1]["end"] < end_time:
        found.append((segments[-1]["end"], end_time - segments[-1]["end"]))

    if len(found) > MAX_CANDIDATES:  # keep only the clearest pauses
        found = sorted(found, key=lambda g: -g[1])[:MAX_CANDIDATES]

    times = {t for t, _ in found}
    if grid_step > 0:
        count = min(int((end_time - start_time) / grid_step), MAX_GRID_POINTS)
        step = (end_time - start_time) / (count + 1) if count > 0 else 0
        if step > 0:
            times.update(start_time + step * n for n in range(1, count + 1))
    return sorted(times)


def _window_cost(item, start, end, clock):
    """How badly a window suits an item, by content type and then by length."""
    span = max(end - start, 1e-6)
    talking = clock.between(start, end)
    wrong = (talking / span) if _is_silent_item(item) else (1.0 - talking / span)

    prior = DURATION_PRIORS.get(item.label, DEFAULT_PRIOR)
    # log-ratio so half as long costs the same as twice as long, rather than
    # short items being effectively free
    length = math.log(max(span, 1.0) / prior) ** 2
    return TYPE_WEIGHT * wrong + length


def _place_items(region_items, start_time, end_time, segments, clock):
    """Choose windows for consecutive unanchored items across [start, end].

    Minimises total window cost over the candidate boundaries, so music is
    pushed onto the silences and speech onto the talking, with the duration
    priors only breaking ties.
    """
    count = len(region_items)
    if count == 0:
        return []
    if count == 1:
        return [(start_time, end_time)]

    candidates = _boundary_candidates(segments, start_time, end_time, GRID_STEP_SECONDS)
    times = [start_time] + candidates + [end_time]
    if len(times) < count + 1:  # not enough distinct pauses - fall back to priors
        priors = [DURATION_PRIORS.get(i.label, DEFAULT_PRIOR) for i in region_items]
        total = sum(priors) or 1
        span = max(end_time - start_time, 0)
        bounds, t = [], start_time
        for p in priors:
            bounds.append((t, t + span * p / total))
            t += span * p / total
        return bounds

    last = len(times) - 1
    INF = float("inf")
    # dp[k][j]: best cost with items 0..k placed and item k ending at times[j]
    dp = [[INF] * len(times) for _ in range(count)]
    back = [[-1] * len(times) for _ in range(count)]
    for j in range(1, len(times)):
        dp[0][j] = _window_cost(region_items[0], times[0], times[j], clock)
    for k in range(1, count):
        for j in range(k + 1, len(times)):
            best, best_i = INF, -1
            for i in range(k, j):
                if dp[k - 1][i] == INF:
                    continue
                cost = dp[k - 1][i] + _window_cost(region_items[k], times[i], times[j], clock)
                if cost < best:
                    best, best_i = cost, i
            dp[k][j], back[k][j] = best, best_i

    bounds, j = [], last
    for k in range(count - 1, -1, -1):
        i = back[k][j] if k > 0 else 0
        bounds.append((times[i], times[j]))
        j = i
    return list(reversed(bounds))


def _fill_windows(items, anchors, segments, clock, audio_duration):
    """Return {item_index: (start, end)} for every item, anchored or inferred."""
    windows = {}
    n = len(items)
    i = 0
    prev_end = 0.0

    while i < n:
        if i in anchors:
            windows[i] = anchors[i]
            prev_end = anchors[i][1]
            i += 1
            continue

        j = i
        while j < n and j not in anchors:
            j += 1
        next_start = anchors[j][0] if j < n else audio_duration

        region = [items[k] for k in range(i, j)]
        for k, bounds in zip(range(i, j), _place_items(region, prev_end, next_start, segments, clock)):
            windows[k] = bounds

        i = j
        prev_end = next_start

    return windows


def align(items, segments, audio_duration=None):
    if not segments:
        return [
            AlignedBlock(kind=item.kind, label=item.label, speaker=item.speaker, title=item.title)
            for item in items
        ]

    if audio_duration is None:
        audio_duration = segments[-1]["end"]
    clock = SpeechClock(segments)
    anchors = _anchor_items(items, segments)
    windows = _fill_windows(items, anchors, segments, clock, audio_duration)

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
    import json
    import sys

    from worship_guide import parse_worship_guide

    items = parse_worship_guide(sys.argv[1])
    segments, duration = [], None
    if len(sys.argv) > 2:
        raw = json.load(open(sys.argv[2], encoding="utf-8"))
        segments = raw["segments"] if isinstance(raw, dict) else raw
        duration = raw.get("duration") if isinstance(raw, dict) else None
    for block in align(items, segments, audio_duration=duration):
        mark = "MUSIC " if block.kind == "music" else "SPEECH"
        preview = block.text[:70] if block.kind == "speech" else block.title
        print(
            f"[{mark} {block.start/60:5.1f}-{block.end/60:5.1f}m] "
            f"{block.label:24} {block.speaker:22} {preview}"
        )
