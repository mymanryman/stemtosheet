"""Tempo, beat grid, meter and key detection.

Everything here works on plain note lists so it is easy to test and does not
depend on the GUI.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .midi_io import MidiData, NoteEvent

# --------------------------------------------------------------------------
# Beat grid
# --------------------------------------------------------------------------


@dataclass
class Timing:
    """Maps note times (seconds) to musical positions measured in *beats*.

    source is "file" when the file's own tempo map is trusted (positions come
    straight from the MIDI ticks: typical for MIDI made in a DAW against a
    click), or "detected" / "manual" when we track the beats ourselves.
    Tracked beats follow tempo drift and rubato, so a performance that speeds
    up and slows down still lands on a steady grid.
    """

    bpm: float                                # typical tempo, for the score
    beat_times: np.ndarray | None = None      # seconds of beat 0, 1, 2, ...
    source: str = "detected"                  # "file", "detected" or "manual"
    confidence: float = 0.0                   # 0..1, how well onsets sit on the grid

    def to_beats(self, t: float) -> float:
        bt = self.beat_times
        if bt is None or len(bt) < 2:
            return t * self.bpm / 60.0
        if t <= bt[0]:
            return (t - bt[0]) / (bt[1] - bt[0])
        if t >= bt[-1]:
            return len(bt) - 1 + (t - bt[-1]) / (bt[-1] - bt[-2])
        return float(np.interp(t, bt, np.arange(len(bt))))

    def beats(self, note: NoteEvent) -> tuple[float, float]:
        if self.source == "file":
            return note.start_q, note.end_q
        return self.to_beats(note.start), self.to_beats(note.end)


def cluster_chords(notes: list[NoteEvent], window: float = 0.07) -> list[NoteEvent]:
    """Line up rolled / slightly spread chords.

    Notes that start within `window` seconds of the first note of a group get
    the group's (velocity weighted) start time, so they quantize together.
    """
    out = [NoteEvent(n.pitch, n.start, n.end, n.velocity, n.start_q, n.end_q)
           for n in sorted(notes, key=lambda n: n.start)]
    i = 0
    while i < len(out):
        j = i + 1
        while j < len(out) and out[j].start - out[i].start < window:
            j += 1
        if j - i > 1:
            grp = out[i:j]
            wsum = sum(n.velocity + 1 for n in grp)
            t = sum(n.start * (n.velocity + 1) for n in grp) / wsum
            q = sum(n.start_q * (n.velocity + 1) for n in grp) / wsum
            for n in grp:
                n.end = max(n.end, t + 0.03)
                n.start = t
                n.start_q = q
                n.end_q = max(n.end_q, q + 0.05)
        i = j
    return out


def _accent(n: NoteEvent) -> float:
    """Long, loud and low notes tend to fall on beats."""
    return (min(n.duration, 2.0) + 0.15) * (0.3 + n.velocity / 127.0) * (1.5 if n.pitch < 55 else 1.0)


def _grid_deviation(positions: np.ndarray, weights: np.ndarray | None = None) -> float:
    """Mean distance (in beats) from the nearest 1/16 or 1/8-triplet."""
    if len(positions) == 0:
        return 0.0
    d16 = np.abs(positions * 4 - np.round(positions * 4)) / 4
    d12 = np.abs(positions * 3 - np.round(positions * 3)) / 3
    d = np.minimum(d16, d12)
    return float(np.average(d, weights=weights))


FS = 100.0  # onset envelope sample rate (Hz)


def _envelope(notes: list[NoteEvent]) -> np.ndarray:
    end = max(n.start for n in notes) + 3.0
    env = np.zeros(int(end * FS) + 1)
    for n in notes:
        env[int(round(n.start * FS))] += _accent(n)
    # Smooth a little (gaussian, sigma 30 ms) so slightly loose onsets still add up.
    k = np.exp(-0.5 * (np.arange(-9, 10) / 3.0) ** 2)
    env = np.convolve(env, k / k.sum(), mode="same")
    return env / (env.std() + 1e-9)


def _tempo_prior(bpm):
    """Gentle preference for moderate tempos (resolves half/double ambiguity)."""
    return np.exp(-0.5 * (np.log2(np.asarray(bpm, dtype=float) / 100.0) / 0.9) ** 2)


def estimate_tempo(env: np.ndarray) -> tuple[float, list[tuple[float, float]]]:
    """Global tempo from the envelope's autocorrelation (robust to drift)."""
    n = len(env)
    f = np.fft.rfft(env - env.mean(), 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac /= ac[0] + 1e-9
    lags = np.arange(int(FS * 60 / 240), int(FS * 60 / 38))
    lags = lags[lags < n]
    bpms = 60.0 * FS / lags
    # A real beat also shows up at twice its period; add that in.
    two = np.clip(2 * lags, 0, n - 1)
    score = (ac[lags] + 0.5 * ac[two]) * _tempo_prior(bpms)
    order = np.argsort(score)[::-1]
    # Parabolic refinement of the best lag.
    i = order[0]
    lag = float(lags[i])
    if 0 < i < len(lags) - 1:
        y0, y1, y2 = score[i - 1], score[i], score[i + 1]
        denom = y0 - 2 * y1 + y2
        if denom != 0:
            lag += 0.5 * (y0 - y2) / denom
    alts = []
    for j in order[1:]:
        b = float(bpms[j])
        if all(abs(b - a) / a > 0.08 for a, _ in alts) and abs(b - 60 * FS / lag) / b > 0.08:
            alts.append((round(b, 1), float(score[j])))
        if len(alts) >= 3:
            break
    return 60.0 * FS / lag, alts


def track_beats(env: np.ndarray, bpm: float, tightness: float = 120.0) -> np.ndarray:
    """Dynamic-programming beat tracker (after Ellis, 2007).

    Finds beat times that sit on strong onsets while keeping beat-to-beat
    spacing close to the given tempo. Returns beat times in seconds.
    """
    period = FS * 60.0 / bpm
    n = len(env)
    w = np.exp(-0.5 * (np.arange(-int(period), int(period) + 1) * 32.0 / period) ** 2)
    local = np.convolve(env, w, mode="same")
    cum = np.zeros(n)
    back = np.full(n, -1, dtype=int)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    prev_rel = -np.arange(hi, lo - 1, -1)          # offsets -2p .. -p/2
    tx = -tightness * np.log(-prev_rel / period) ** 2
    for t in range(n):
        idx = t + prev_rel
        ok = idx >= 0
        if ok.any():
            cand = cum[idx[ok]] + tx[ok]
            k = int(np.argmax(cand))
            if cand[k] > 0:
                cum[t] = local[t] + cand[k]
                back[t] = idx[ok][k]
                continue
        cum[t] = local[t]
    # Best end point within the last beat period, then backtrack.
    tail = cum[max(0, n - int(period) - int(3 * FS)):]
    t = int(np.argmax(tail)) + max(0, n - int(period) - int(3 * FS))
    beats = [t]
    while back[t] >= 0:
        t = back[t]
        beats.append(t)
    beats = np.asarray(beats[::-1], dtype=float) / FS
    return beats


def _extend(beats: np.ndarray, start: float, end: float) -> np.ndarray:
    """Pad the beat list so it covers [start, end]."""
    if len(beats) < 2:
        return beats
    head = beats[1] - beats[0]
    pre = []
    t = beats[0] - head
    while t > start - head:
        pre.append(t)
        t -= head
    tail = beats[-1] - beats[-2]
    post = []
    t = beats[-1] + tail
    while t < end + tail:
        post.append(t)
        t += tail
    return np.concatenate([np.asarray(pre[::-1]), beats, np.asarray(post)])


def detect_timing(data: MidiData, manual_bpm: float | None = None,
                  notes: list[NoteEvent] | None = None) -> Timing:
    notes = notes if notes is not None else data.notes
    if not notes:
        return Timing(bpm=manual_bpm or data.file_bpm or 120.0, source="file")
    weights = np.array([_accent(n) for n in notes])

    # 1) Does the file's own grid already fit? (Typical when the MIDI was made
    #    against a click or quantized in a DAW.)
    if not manual_bpm and data.file_bpm:
        file_pos = np.array([n.start_q for n in notes])
        file_dev = _grid_deviation(file_pos, weights)
        if file_dev < 0.03:
            return Timing(round(data.file_bpm, 2), None, "file",
                          max(0.0, 1 - file_dev / 0.08))

    # 2) Otherwise estimate the tempo and track the beats.
    env = _envelope(notes)
    if manual_bpm:
        bpm, source = float(manual_bpm), "manual"
    else:
        bpm, _ = estimate_tempo(env)
        source = "detected"
    beats = track_beats(env, bpm)
    beats = _extend(beats, min(n.start for n in notes), max(n.end for n in notes))
    t = Timing(bpm, beats, source)
    if len(beats) > 2:
        t.bpm = round(60.0 / float(np.median(np.diff(beats))), 1)
    if manual_bpm:
        t.bpm = float(manual_bpm)
    pos = np.array([t.to_beats(n.start) for n in notes])
    t.confidence = max(0.0, 1 - _grid_deviation(pos, weights) / 0.08)
    return t


# --------------------------------------------------------------------------
# Meter
# --------------------------------------------------------------------------

COMPOUND = {(6, 8), (9, 8), (12, 8)}


def is_compound(ts: tuple[int, int]) -> bool:
    return tuple(ts) in COMPOUND


@dataclass
class MeterGuess:
    time_signature: tuple[int, int]
    pickup_beats: int = 0     # beats before the first downbeat
    confidence: float = 0.0


def triplet_fraction(beat_positions: list[float]) -> float:
    """Share of off-beat onsets that sit on a triplet rather than a 1/16 grid."""
    trip = straight = 0
    for b in beat_positions:
        f = b - math.floor(b)
        if min(f, 1 - f) < 0.06:
            continue  # on the beat: tells us nothing
        d16 = abs(f * 4 - round(f * 4)) / 4
        d12 = abs(f * 3 - round(f * 3)) / 3
        if d12 + 0.02 < d16:
            trip += 1
        elif d16 + 0.02 < d12:
            straight += 1
    total = trip + straight
    return trip / total if total else 0.0


def detect_meter(beat_notes: list[tuple[int, float, float, int]],
                 file_ts: tuple[int, int] | None = None,
                 trust_file: bool = False) -> MeterGuess:
    """beat_notes: (pitch, start_beat, end_beat, velocity)."""
    if trust_file and file_ts and file_ts != (4, 4):
        return MeterGuess(file_ts, 0, 0.8)
    if not beat_notes:
        return MeterGuess(file_ts or (4, 4), 0, 0.0)

    n_beats = int(math.ceil(max(n[2] for n in beat_notes))) + 1
    # Per beat: how long/loud the notes starting there are, and the lowest
    # pitch struck there. Bars tend to start with long notes and a bass note.
    accent = np.zeros(n_beats)
    lowest = np.full(n_beats, 200.0)
    for pitch, s, e, vel in beat_notes:
        b = round(s)
        if abs(s - b) > 0.15 or b < 0 or b >= n_beats:
            continue
        w = (0.3 + vel / 127.0) * min(e - s, 4.0)
        accent[b] = max(accent[b], w) + 0.1 * min(accent[b], w)
        lowest[b] = min(lowest[b], pitch)
    bass = np.zeros(n_beats)
    for b in range(n_beats):
        if lowest[b] > 127:
            continue
        window = lowest[max(0, b - 2): b + 3]
        neighbours = window[window <= 127]
        # A bass note that is the lowest around it and below the average.
        if lowest[b] <= neighbours.min() and lowest[b] < neighbours.mean() - 2:
            bass[b] = 1.0
        elif lowest[b] <= neighbours.min():
            bass[b] = 0.3
    strength = accent / (accent.mean() + 1e-9) + 1.5 * bass

    best = None
    scores = {}
    for L in (2, 3, 4):
        if n_beats < 2 * L:
            continue
        for ph in range(L):
            on = strength[ph::L]
            mask = np.ones(n_beats, bool)
            mask[ph::L] = False
            off = strength[mask]
            contrast = (on.mean() - off.mean()) / (strength.mean() + 1e-9)
            if L not in scores or contrast > scores[L][0]:
                scores[L] = (contrast, ph)
    if not scores:
        return MeterGuess(file_ts or (4, 4), 0, 0.0)

    # 2- and 4-beat bars look alike; use 4 unless 3 clearly wins.
    c4 = max(scores.get(4, (-9, 0))[0], scores.get(2, (-9, 0))[0])
    c3 = scores.get(3, (-9, 0))[0]
    if c3 > c4 + 0.08:
        L, (contrast, phase) = 3, scores[3]
    elif 4 in scores:
        L, (contrast, phase) = 4, scores[4]
    else:
        L, (contrast, phase) = 2, scores[2]

    trip = triplet_fraction([n[1] for n in beat_notes])
    if trip > 0.6:
        ts = {2: (6, 8), 3: (9, 8), 4: (12, 8)}[L]
    else:
        ts = {2: (2, 4), 3: (3, 4), 4: (4, 4)}[L]
    best = MeterGuess(ts, phase, float(max(0.0, min(1.0, contrast))))
    return best


# --------------------------------------------------------------------------
# Key
# --------------------------------------------------------------------------

# Krumhansl-Kessler key profiles.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# Tonic spelling as used in key signatures (max 6 accidentals).
MAJOR_TONICS = ["C", "D-", "D", "E-", "E", "F", "F#", "G", "A-", "A", "B-", "B"]
MINOR_TONICS = ["c", "c#", "d", "e-", "e", "f", "f#", "g", "g#", "a", "b-", "b"]


@dataclass
class KeyGuess:
    tonic: str        # music21 key string, lower case = minor ("E-", "c#")
    correlation: float
    alternatives: list[tuple[str, float]]


def estimate_key(notes: list[NoteEvent]) -> KeyGuess:
    hist = np.zeros(12)
    for n in notes:
        hist[n.pitch % 12] += max(n.duration, 0.05) * (0.5 + n.velocity / 127)
    if hist.sum() == 0:
        return KeyGuess("C", 0.0, [])
    results = []
    for i in range(12):
        results.append((MAJOR_TONICS[i], float(np.corrcoef(hist, np.roll(_MAJOR, i))[0, 1])))
        results.append((MINOR_TONICS[i], float(np.corrcoef(hist, np.roll(_MINOR, i))[0, 1])))
    results.sort(key=lambda r: -r[1])
    return KeyGuess(results[0][0], results[0][1], results[1:4])


def key_display_name(k: str) -> str:
    name = k[0].upper() + k[1:].replace("-", "b")
    return f"{name} {'minor' if k[0].islower() else 'major'}"


def all_keys() -> list[str]:
    """Keys offered in the override menu, ordered round the circle of fifths."""
    majors = ["C", "G", "D", "A", "E", "B", "F#", "D-", "A-", "E-", "B-", "F"]
    minors = ["a", "e", "b", "f#", "c#", "g#", "e-", "b-", "f", "c", "g", "d"]
    return majors + minors
