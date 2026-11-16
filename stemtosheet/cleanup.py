"""Note cleanup: ghost notes, quantizing, staff splitting and voice layout.

Positions after `to_quarters` are in quarter-note lengths (music21's unit),
with 0 at the start of bar 1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from .midi_io import NoteEvent

EPS = 1e-6


@dataclass
class QNote:
    pitch: int
    start: float   # quarter lengths
    end: float
    velocity: int = 64

    @property
    def dur(self) -> float:
        return self.end - self.start


# --------------------------------------------------------------------------
# Ghost notes
# --------------------------------------------------------------------------

def remove_ghost_notes(notes: list[NoteEvent], min_ms: float = 40.0,
                       min_velocity: int = 0) -> tuple[list[NoteEvent], int]:
    """Drop very short or very quiet notes. Returns (kept, removed_count)."""
    kept = [n for n in notes
            if n.duration * 1000.0 >= min_ms and n.velocity >= min_velocity]
    return kept, len(notes) - len(kept)


# --------------------------------------------------------------------------
# Quantizing
# --------------------------------------------------------------------------

GRIDS = {
    # label: (straight step in quarters or None, allow 8th-note triplets)
    "1/8": (0.5, False),
    "1/16": (0.25, False),
    "1/32": (0.125, False),
    "1/8 + triplets": (0.5, True),
    "1/16 + triplets": (0.25, True),
}
GRID_LABELS = ["Auto"] + list(GRIDS)


def _snap(x: float, step: float) -> float:
    return round(x / step) * step


def _beat_uses_triplets(onsets_in_beat: list[float], step: float) -> bool:
    """Decide per beat whether the onsets read better as triplets."""
    straight = trip = 0.0
    informative = False
    for x in onsets_in_beat:
        f = x - math.floor(x)
        if min(f, 1 - f) < 0.07:
            continue
        informative = True
        straight += abs(x - _snap(x, step))
        trip += abs(x - _snap(x, 1 / 3))
    # Bias toward straight rhythms: triplets must be clearly better.
    return informative and trip + 0.03 * len(onsets_in_beat) < straight


def quantize(notes: list[QNote], grid: str) -> list[QNote]:
    if grid == "Auto":
        grid = choose_grid(notes)
    step, allow_trip = GRIDS[grid]

    triplet_beats: set[int] = set()
    if allow_trip:
        by_beat: dict[int, list[float]] = {}
        for n in notes:
            by_beat.setdefault(math.floor(n.start + 1 / 6), []).append(n.start)
        triplet_beats = {b for b, xs in by_beat.items() if _beat_uses_triplets(xs, step)}

    def q(x: float) -> float:
        b = math.floor(x + 1 / 6)
        s = 1 / 3 if b in triplet_beats else step
        v = _snap(x, s)
        # Store exact fractions so later arithmetic stays clean.
        return float(Fraction(v).limit_denominator(48))

    out: list[QNote] = []
    for n in notes:
        b = math.floor(n.start + 1 / 6)
        unit = 1 / 3 if b in triplet_beats else step
        s = q(n.start)
        e = q(n.end)
        if e < s + unit - EPS:
            e = float(Fraction(s + unit).limit_denominator(48))
        out.append(QNote(n.pitch, s, e, n.velocity))
    return fix_overlaps(out)


def _grid_error(xs, step: float, trip: bool) -> float:
    total = 0.0
    for x in xs:
        d = abs(x - _snap(x, step))
        if trip:
            d = min(d, abs(x - _snap(x, 1 / 3)) + 0.01)
        total += d
    return total / max(1, len(xs))


# Mean error a grid gives for random onsets: what "no fit at all" looks like.
_RANDOM = [i / 960 for i in range(960)]
_GRID_CANDIDATES = [("1/8", 0.5, False), ("1/8 + triplets", 0.5, True),
                    ("1/16", 0.25, False), ("1/16 + triplets", 0.25, True),
                    ("1/32", 0.125, False)]
_RANDOM_ERR = {name: _grid_error(_RANDOM, st, tr) for name, st, tr in _GRID_CANDIDATES}


def choose_grid(notes: list[QNote]) -> str:
    """Pick the grid that explains the onsets best relative to chance.

    A finer grid always lands closer to the notes, so compare each grid's
    error with the error it would give for random timing, and give the
    coarser (easier to read) grids a small head start.
    """
    if not notes:
        return "1/16"
    starts = [n.start for n in notes]
    best, best_score = "1/8", math.inf
    for rank, (name, step, trip) in enumerate(_GRID_CANDIDATES):
        score = _grid_error(starts, step, trip) / _RANDOM_ERR[name] + 0.04 * rank
        if score < best_score:
            best, best_score = name, score
    return best


def fix_overlaps(notes: list[QNote]) -> list[QNote]:
    """Remove duplicates and stop a note when the same pitch is struck again."""
    notes = sorted(notes, key=lambda n: (n.pitch, n.start, -n.end))
    out: list[QNote] = []
    for n in notes:
        if out and out[-1].pitch == n.pitch:
            prev = out[-1]
            if abs(prev.start - n.start) < EPS:
                prev.end = max(prev.end, n.end)
                continue
            if prev.end > n.start + EPS:
                prev.end = n.start
        out.append(n)
    out = [n for n in out if n.dur > EPS]
    out.sort(key=lambda n: (n.start, n.pitch))
    return out


# --------------------------------------------------------------------------
# Staff splitting
# --------------------------------------------------------------------------

def _groups(notes: list[QNote]) -> list[list[QNote]]:
    groups: list[list[QNote]] = []
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        if groups and abs(groups[-1][0].start - n.start) < EPS:
            groups[-1].append(n)
        else:
            groups.append([n])
    return groups


def split_hands(notes: list[QNote], mode: str = "smart",
                split_pitch: int = 60) -> tuple[list[QNote], list[QNote]]:
    """Return (right_hand, left_hand)."""
    if mode == "fixed":
        rh = [n for n in notes if n.pitch >= split_pitch]
        lh = [n for n in notes if n.pitch < split_pitch]
        return rh, lh

    rh: list[QNote] = []
    lh: list[QNote] = []
    rh_c, lh_c = float(split_pitch + 7), float(split_pitch - 12)
    for g in _groups(notes):
        ps = [n.pitch for n in g]  # already sorted low to high
        t = g[0].start
        # Notes each hand is still holding limit what else it can reach.
        held_l = [n.pitch for n in lh[-12:] if n.end > t + EPS and n.dur >= 0.5]
        held_r = [n.pitch for n in rh[-12:] if n.end > t + EPS and n.dur >= 0.5]
        best_s, best_cost = 0, math.inf
        for s in range(len(ps) + 1):
            left, right = ps[:s], ps[s:]
            cost = 0.0
            for p in left:
                cost += abs(p - lh_c) / 12
                if p >= split_pitch:
                    cost += 0.4 + 0.2 * (p - split_pitch)
            for p in right:
                cost += abs(p - rh_c) / 12
                if p < split_pitch:
                    cost += 0.4 + 0.2 * (split_pitch - p)
            for hand, held in ((left, held_l), (right, held_r)):
                if hand:
                    span = hand[-1] - hand[0]
                    if span > 14:
                        cost += 1.0 + (span - 14) * 0.15
                    if held:
                        reach = max(hand + held) - min(hand + held)
                        if reach > 14:
                            cost += 0.8 + (reach - 14) * 0.1
                    if len(hand) > 5:
                        cost += 1.5
            if cost < best_cost:
                best_cost, best_s = cost, s
        left_notes, right_notes = g[:best_s], g[best_s:]
        lh.extend(left_notes)
        rh.extend(right_notes)
        if left_notes:
            lh_c = 0.7 * lh_c + 0.3 * sum(n.pitch for n in left_notes) / len(left_notes)
        else:
            lh_c = 0.9 * lh_c + 0.1 * (split_pitch - 12)
        if right_notes:
            rh_c = 0.7 * rh_c + 0.3 * sum(n.pitch for n in right_notes) / len(right_notes)
        else:
            rh_c = 0.9 * rh_c + 0.1 * (split_pitch + 7)
        # Keep the two hand centres apart.
        if rh_c - lh_c < 7:
            mid = (rh_c + lh_c) / 2
            rh_c, lh_c = mid + 3.5, mid - 3.5
    return rh, lh


# --------------------------------------------------------------------------
# Voices
# --------------------------------------------------------------------------

@dataclass
class Chord:
    start: float
    end: float
    pitches: list[int]


def assign_voices(notes: list[QNote], max_voices: int = 2,
                  fill_gap: float = 0.25) -> list[list[Chord]]:
    """Lay out one staff's notes as non-overlapping chords in 1-2 voices.

    Voice 1 carries the main rhythm. Notes that are held across later onsets
    go to voice 2 (if allowed), otherwise they are shortened.
    """
    groups = _groups(notes)
    v1: list[Chord] = []
    v2: list[Chord] = []
    v2_end = -math.inf
    for i, g in enumerate(groups):
        start = g[0].start
        nxt = groups[i + 1][0].start if i + 1 < len(groups) else math.inf
        room = nxt - start
        long = [n for n in g if n.dur > room + EPS]
        short = [n for n in g if n.dur <= room + EPS]
        if max_voices >= 2 and long and short and v2_end <= start + EPS:
            end2 = max(n.end for n in long)
            v2.append(Chord(start, end2, sorted(n.pitch for n in long)))
            v2_end = end2
            main = short
        else:
            main = g
        end1 = min(max(n.end for n in main), nxt)
        # Close tiny gaps before the next onset so we don't print 16th rests
        # after every slightly detached note.
        if nxt != math.inf and 0 < nxt - end1 <= fill_gap + EPS and nxt - end1 < (end1 - start):
            end1 = nxt
        v1.append(Chord(start, end1, sorted(n.pitch for n in main)))
    # Voice 2 chords must not overlap each other either.
    for a, b in zip(v2, v2[1:]):
        if a.end > b.start:
            a.end = b.start
    return [v1, v2] if v2 else [v1]
