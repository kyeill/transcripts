"""Match a whisper transcript (chronological, timestamped) to the worship
guide's ordered list of service items, so each stretch of audio gets the
right speaker label - and music gets dropped instead of showing up as
garbled sung lyrics.

Three passes:

1. Anchor pass - a few guide items print text that is genuinely performed
   word-for-word (scripture read aloud, a song whose full lyrics are given).
   Each is fuzzy-matched against the whole transcript to pin exact times.
2. Consistency pass - the chosen anchors must run in the same order as the
   guide, so a single bad match can't drag everything after it out of place.
3. Fill pass - everything else (prayers, the sermon, announcements, hymns
   cited only by number) has no text to match on, so its boundaries are cut
   at real silences in the audio, guided by rough per-label duration priors.

Segments are then bucketed into whichever item's window contains them.
"""
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

# rough seconds, used to keep the silence-cut boundaries plausible when
# several items share one gap between anchors
DURATION_PRIORS = {
    "Prelude": 120,
    "Welcome": 90,
    "Silent meditation": 60,
    "Call to Worship": 60,
    "Song": 240,
    "Invocation": 60,
    "Call to Confession": 30,
    "Prayer of confession": 90,
    "Declaration of forgiveness": 30,
    "Scripture reading": 120,
    "Sermon": 2100,
    "Prayer": 60,
    "Living out our faith": 180,
    "Missionary Greeting": 180,
    "Prayers of the People": 240,
    "Offertory": 240,
    "Doxology": 60,
    "Benediction": 60,
    "Postlude": 90,
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
GAP_REWARD = 2.0  # how much a big silence outweighs an off-prior duration
GAP_SATURATION = 8.0  # seconds beyond which a longer pause is no better


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


def _gap_candidates(segments, start_time, end_time):
    """Silences inside (start_time, end_time), as (time, size) pairs.

    A new section of a service almost always begins after a pause - a new
    speaker reaching the lectern, the end of a song - so real silences are
    far better boundary evidence than guessed durations.
    """
    gaps = []
    for a, b in zip(segments, segments[1:]):
        size = b["start"] - a["end"]
        if size < MIN_GAP_SECONDS:
            continue
        midpoint = (a["end"] + b["start"]) / 2
        if start_time < midpoint < end_time:
            gaps.append((midpoint, size))
    return gaps


def _duration_cost(duration, prior):
    """Penalise an item running far longer or shorter than its prior.

    Log-ratio so being half as long costs the same as being twice as long,
    rather than short items being effectively free.
    """
    return math.log(max(duration, 1.0) / prior) ** 2


def _cut_at_silences(labels, start_time, end_time, gaps):
    """Split [start_time, end_time] among len(labels) items, cutting at pauses.

    Chooses the boundaries minimising (duration implausibility - silence
    reward) over all valid combinations, so a cut lands on a real pause
    unless doing so would make some item an absurd length.
    """
    count = len(labels)
    priors = [DURATION_PRIORS.get(l, DEFAULT_PRIOR) for l in labels]
    if count == 1:
        return [(start_time, end_time)]

    needed = count - 1
    if len(gaps) < needed:  # not enough real pauses - fall back to priors
        total = sum(priors) or 1
        span = max(end_time - start_time, 0)
        bounds, t = [], start_time
        for p in priors:
            bounds.append((t, t + span * p / total))
            t += span * p / total
        return bounds

    # keep the search cheap on long gaps by considering only the clearest pauses
    if len(gaps) > 60:
        gaps = sorted(sorted(gaps, key=lambda g: -g[1])[:60])
    times = [start_time] + [g[0] for g in gaps] + [end_time]
    rewards = [0.0] + [GAP_REWARD * min(g[1], GAP_SATURATION) / GAP_SATURATION for g in gaps] + [0.0]
    last = len(times) - 1

    # dp[k][j]: best cost with items 0..k placed and item k ending at times[j]
    INF = float("inf")
    dp = [[INF] * len(times) for _ in range(count)]
    back = [[-1] * len(times) for _ in range(count)]
    for j in range(1, len(times)):
        dp[0][j] = _duration_cost(times[j] - times[0], priors[0]) - rewards[j]
    for k in range(1, count):
        for j in range(k + 1, len(times)):
            for i in range(k, j):
                if dp[k - 1][i] == INF:
                    continue
                cost = dp[k - 1][i] + _duration_cost(times[j] - times[i], priors[k]) - rewards[j]
                if cost < dp[k][j]:
                    dp[k][j] = cost
                    back[k][j] = i

    bounds, j = [], last
    for k in range(count - 1, -1, -1):
        i = back[k][j] if k > 0 else 0
        bounds.append((times[i], times[j]))
        j = i
    return list(reversed(bounds))


def _fill_windows(items, anchors, segments, audio_duration):
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

        labels = [items[k].label for k in range(i, j)]
        gaps = _gap_candidates(segments, prev_end, next_start)
        for k, bounds in zip(range(i, j), _cut_at_silences(labels, prev_end, next_start, gaps)):
            windows[k] = bounds

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
    windows = _fill_windows(items, anchors, segments, audio_duration)

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
    segments = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 else []
    for block in align(items, segments):
        mark = "MUSIC " if block.kind == "music" else "SPEECH"
        preview = block.text[:90] if block.kind == "speech" else block.title
        print(f"[{mark} {block.start:6.0f}-{block.end:6.0f}] {block.label:24} {block.speaker:24} {preview}")
