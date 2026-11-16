"""Turn cleaned-up chords into a music21 Score (piano grand staff or one staff)."""
from __future__ import annotations

import math
from fractions import Fraction

from music21 import (bar, clef, duration, instrument, key, layout, meter,
                     metadata, note, pitch, stream, tempo)
from music21 import chord as m21chord

from .cleanup import EPS, Chord

# Page sizes in millimetres.
PAGE_SIZES = {"A4": (210.0, 297.0), "Letter": (215.9, 279.4)}

# Durations (in quarters) we are happy to print as a single note or rest.
_NOTATABLE = {Fraction(x).limit_denominator(48) for x in (
    6, 4, 3, 2, 1.5, 1, 0.75, 0.5, 0.375, 0.25, 0.125, 1 / 3, 2 / 3, 1 / 6)}


def _f(x: float) -> Fraction:
    return Fraction(x).limit_denominator(48)


# --------------------------------------------------------------------------
# Spelling
# --------------------------------------------------------------------------

def spelling_table(key_name: str) -> dict[int, str]:
    """Map pitch class -> note name that reads best in the given key."""
    k = key.Key(key_name)
    names: dict[int, str] = {}
    for p in k.getScale().getPitches()[:7]:
        names[p.pitchClass] = p.name
    if k.mode == "minor":  # raised 6th and 7th of melodic/harmonic minor
        for interval in ("M6", "M7"):
            p = k.tonic.transpose(interval)
            names.setdefault(p.pitchClass, p.name)
    sharps = k.sharps
    if sharps > 0:
        chromatic = {1: "C#", 3: "D#", 6: "F#", 8: "G#", 10: "A#"}
    elif sharps < 0:
        chromatic = {1: "D-", 3: "E-", 6: "G-", 8: "A-", 10: "B-"}
    else:
        chromatic = {1: "C#", 3: "E-", 6: "F#", 8: "G#", 10: "B-"}
    naturals = {0: "C", 2: "D", 4: "E", 5: "F", 7: "G", 9: "A", 11: "B"}
    for pc in range(12):
        names.setdefault(pc, chromatic.get(pc) or naturals[pc])
    return names


def make_pitch(midi: int, table: dict[int, str]) -> pitch.Pitch:
    p = pitch.Pitch(table[midi % 12])
    # Set the octave so the MIDI number comes out right (handles B#, C-).
    p.octave = 4
    p.octave += (midi - p.midi) // 12
    return p


# --------------------------------------------------------------------------
# Rhythm splitting
# --------------------------------------------------------------------------

def _boundaries(ts: tuple[int, int]) -> tuple[Fraction, list[Fraction]]:
    """(beat length, strong internal bar positions) in quarters."""
    num, den = ts
    if (num, den) in {(6, 8), (9, 8), (12, 8)}:
        beat = Fraction(3, 2)
    else:
        beat = Fraction(4, den)
    bar_len = Fraction(num * 4, den)
    strong = []
    if (num, den) in {(4, 4), (12, 8)}:
        strong = [bar_len / 2]
    return beat, strong


def split_span(s: Fraction, e: Fraction, m0: Fraction, ts) -> list[tuple[Fraction, Fraction]]:
    """Split [s, e) (inside one bar starting at m0) into readable pieces."""
    beat, strong = _boundaries(ts)
    d = e - s
    rel = s - m0
    if d <= 0:
        return []
    crosses_strong = any(rel < b < rel + d for b in strong)
    on_beat = (rel % beat) == 0
    ends_on_beat = ((rel + d) % beat) == 0
    if d < beat:
        crosses_beat = math.floor(rel / beat) != math.ceil((rel + d) / beat) - 1
        ok = not crosses_beat
    else:
        ok = (on_beat or ends_on_beat) and not crosses_strong
    if ok and d in _NOTATABLE:
        return [(s, e)]
    # Find the split point: strong bar position, then beats, then finer.
    for level in ([m0 + b for b in strong], None, Fraction(1, 2), Fraction(1, 4),
                  Fraction(1, 3), Fraction(1, 8), Fraction(1, 6), Fraction(1, 12),
                  Fraction(1, 16), Fraction(1, 24), Fraction(1, 48)):
        if level is None:
            cands = [m0 + beat * k for k in range(1, 64)]
        elif isinstance(level, list):
            cands = level
        else:
            cands = [m0 + level * k for k in range(1, 400)]
        inside = [c for c in cands if s < c < e]
        if inside:
            c = inside[0]
            return split_span(s, c, m0, ts) + split_span(c, e, m0, ts)
    return [(s, e)]  # should not happen with a 1/48 grid


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def _fill_voice(container, chords: list[Chord], m0: Fraction, m1: Fraction,
                table, ts, hide_rests: bool):
    """Append notes, ties and rests for one voice in one bar."""
    pos = m0
    segs = []
    for c in chords:
        cs, ce = _f(c.start), _f(c.end)
        if ce <= m0 or cs >= m1:
            continue
        segs.append((max(cs, m0), min(ce, m1), c, cs < m0, ce > m1))
    segs.sort(key=lambda x: x[0])

    def add_rest(a, b):
        for s, e in split_span(a, b, m0, ts):
            r = note.Rest(quarterLength=float(e - s))
            if hide_rests:
                r.style.hideObjectOnPrint = True
            container.insert(float(s - m0), r)

    for s, e, c, tied_in, tied_out in segs:
        if s < pos:  # overlap safety net
            s = pos
            if s >= e:
                continue
        if s > pos:
            add_rest(pos, s)
        pieces = split_span(s, e, m0, ts)
        for i, (a, b) in enumerate(pieces):
            ps = [make_pitch(p, table) for p in c.pitches]
            el = note.Note(ps[0]) if len(ps) == 1 else m21chord.Chord(ps)
            el.duration = duration.Duration(float(b - a))
            start_tie = tied_in or i > 0
            end_tie = tied_out or i < len(pieces) - 1
            if start_tie and end_tie:
                el.tie = _tie("continue")
            elif start_tie:
                el.tie = _tie("stop")
            elif end_tie:
                el.tie = _tie("start")
            container.insert(float(a - m0), el)
        pos = e
    if pos < m1:
        if pos == m0 and not segs:
            r = note.Rest(quarterLength=float(m1 - m0))
            r.fullMeasure = True
            if hide_rests:
                r.style.hideObjectOnPrint = True
            container.insert(0, r)
        else:
            add_rest(pos, m1)


def _tie(kind: str):
    from music21 import tie
    return tie.Tie(kind)


def build_score(staves: list[dict], *, ts: tuple[int, int], key_name: str,
                bpm: float, title: str = "", composer: str = "",
                page_size: str = "A4") -> stream.Score:
    """staves: list of {"voices": [[Chord...], ...], "clef": "treble"|"bass"}."""
    table = spelling_table(key_name)
    bar_len = Fraction(ts[0] * 4, ts[1])
    last_end = max((_f(c.end) for st in staves for v in st["voices"] for c in v),
                   default=bar_len)
    n_bars = max(1, math.ceil(last_end / bar_len))

    score = stream.Score()
    md = metadata.Metadata()
    md.title = title or ""
    md.composer = composer or ""
    score.insert(0, md)

    w, h = PAGE_SIZES.get(page_size, PAGE_SIZES["A4"])
    # MusicXML layout: 40 tenths per 7 mm staff space.
    tenths = lambda mm: round(mm * 40 / 7)
    score.insert(0, layout.ScoreLayout(
        scalingMillimeters=7, scalingTenths=40,
        pageLayout=layout.PageLayout(pageWidth=tenths(w), pageHeight=tenths(h),
                                     leftMargin=tenths(15), rightMargin=tenths(15),
                                     topMargin=tenths(15), bottomMargin=tenths(15))))

    grand = len(staves) > 1
    parts = []
    for idx, st in enumerate(staves):
        part = stream.PartStaff() if grand else stream.Part()
        part.id = f"P{idx + 1}"
        piano = instrument.Piano()
        part.insert(0, piano)
        voices = [v for v in st["voices"] if v] or [[]]
        for m in range(n_bars):
            m0, m1 = bar_len * m, bar_len * (m + 1)
            meas = stream.Measure(number=m + 1)
            if m == 0:
                meas.insert(0, clef.TrebleClef() if st["clef"] == "treble" else clef.BassClef())
                meas.insert(0, key.KeySignature(key.Key(key_name).sharps))
                meas.insert(0, meter.TimeSignature(f"{ts[0]}/{ts[1]}"))
                if idx == 0:
                    compound = tuple(ts) in {(6, 8), (9, 8), (12, 8)}
                    mm = tempo.MetronomeMark(
                        number=round(bpm),
                        referent=duration.Duration(1.5 if compound else 4 / ts[1] if ts[1] in (2, 4) else 1.0))
                    mm.placement = "above"
                    meas.insert(0, mm)
            used = [v for v in voices if any(_f(c.start) < m1 and _f(c.end) > m0 for c in v)]
            if len(used) <= 1:
                _fill_voice(meas, used[0] if used else [], m0, m1, table, ts, False)
            else:
                for vi, v in enumerate(voices):
                    vs = stream.Voice(id=str(vi + 1))
                    _fill_voice(vs, v, m0, m1, table, ts, hide_rests=vi > 0)
                    meas.insert(0, vs)
            if m == n_bars - 1:
                meas.rightBarline = bar.Barline("final")
            part.append(meas)
        parts.append(part)
        score.insert(0, part)

    if grand:
        sg = layout.StaffGroup(parts, name="Piano", abbreviation="Pno.",
                               symbol="brace", barTogether=True)
        score.insert(0, sg)

    for p in parts:
        p.makeBeams(inPlace=True)
        p.makeAccidentals(inPlace=True, cautionaryNotImmediateRepeat=False)
    return score
