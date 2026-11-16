"""Generate piano MIDI test files that look like real stem exports.

Run:  python tests/make_test_midi.py  (writes into samples/)

Each file is a single piano track. Most are "performed": the MIDI file says
120 bpm while the music is at another tempo, timing is loose, velocities vary
and there are a few accidental ghost notes, which is what an audio-to-MIDI
export from a tool like RipX tends to look like.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import mido

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"


def write_midi(path: Path, notes, *, file_bpm=120.0, ts=None, tpb=480):
    """notes: list of (pitch, start_sec, end_sec, velocity)."""
    mid = mido.MidiFile(ticks_per_beat=tpb)
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tr.append(mido.MetaMessage("track_name", name="Piano", time=0))
    tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(file_bpm), time=0))
    if ts:
        tr.append(mido.MetaMessage("time_signature", numerator=ts[0], denominator=ts[1], time=0))
    tr.append(mido.Message("program_change", program=0, time=0))
    sec_to_tick = lambda s: int(round(s * file_bpm / 60 * tpb))
    events = []
    for p, s, e, v in notes:
        events.append((sec_to_tick(max(0, s)), 1, mido.Message("note_on", note=p, velocity=v)))
        events.append((sec_to_tick(max(0, e)), 0, mido.Message("note_off", note=p, velocity=0)))
    events.sort(key=lambda x: (x[0], x[1]))
    last = 0
    for t, _, msg in events:
        msg.time = t - last
        last = t
        tr.append(msg)
    tr.append(mido.MetaMessage("end_of_track", time=0))
    path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(path))
    return path


class Performer:
    """Converts (pitch, beat, length_in_beats, vel) to humanized seconds."""

    def __init__(self, bpm, jitter=0.0, legato=0.92, lead_in=0.37, seed=1, beat_ql=1.0):
        self.spb = 60.0 / bpm
        self.jitter = jitter
        self.legato = legato
        self.lead_in = lead_in
        self.rng = random.Random(seed)
        self.beat_ql = beat_ql
        self.notes = []

    def play(self, pitch, ql, length_ql, vel=70):
        b = ql / self.beat_ql
        L = length_ql / self.beat_ql
        j = self.rng.gauss(0, self.jitter) if self.jitter else 0.0
        s = self.lead_in + b * self.spb + j
        e = s + L * self.spb * self.legato + (self.rng.gauss(0, self.jitter) if self.jitter else 0)
        v = max(20, min(120, int(vel + (self.rng.gauss(0, 8) if self.jitter else 0))))
        self.notes.append((pitch, s, max(e, s + 0.06), v))

    def ghosts(self, count, total_ql):
        for _ in range(count):
            s = self.lead_in + self.rng.uniform(0, total_ql / self.beat_ql) * self.spb
            self.notes.append((self.rng.randint(50, 80), s, s + self.rng.uniform(0.008, 0.03),
                               self.rng.randint(8, 30)))


def clean_c_major():
    """Quantized 4/4, C major, file tempo is correct (like a DAW export)."""
    p = Performer(100, jitter=0, legato=1.0, lead_in=0.0)
    melody = [(72, 1), (74, 1), (76, 1), (77, 1), (79, 2), (79, 2),
              (81, 0.5), (79, 0.5), (77, 0.5), (76, 0.5), (74, 2), (72, 4),
              (76, 1), (74, 1), (72, 1), (71, 1), (72, 4)]
    t = 0.0
    for pitch, d in melody:
        p.play(pitch, t, d, 80)
        t += d
    # Alberti bass, two notes per beat.
    chords = [(48, 55, 52), (48, 55, 52), (53, 57, 60), (55, 59, 62), (48, 55, 52),
              (55, 59, 62), (48, 55, 52)]
    for bar, (a, b, c) in enumerate(chords):
        for i, n in enumerate([a, b, c, b, a, b, c, b]):
            p.play(n, bar * 4 + i * 0.5, 0.5, 60)
    p.play(36, 28, 4, 60)
    p.play(48, 28, 4, 60)
    return write_midi(OUT / "01_c_major_clean_4-4.mid", p.notes, file_bpm=100, ts=(4, 4))


def waltz_f_major():
    """3/4 waltz in F major at 92 bpm, played loosely; file claims 120 bpm."""
    p = Performer(92, jitter=0.012, seed=7)
    # One-beat pickup, then 16 bars.
    p.play(72, 0, 1, 70)  # C5 pickup
    prog = ["F", "F", "C7", "C7", "F", "Bb", "C7", "F"] * 2
    roots = {"F": (41, [57, 60, 65]), "C7": (36, [58, 60, 64]), "Bb": (34, [58, 62, 65])}
    tune = [77, 76, 77, 72, 70, 72, 74, 72, 70, 69, 70, 67, 65, 67, 69, 70, 72, 74, 76, 77, 79,
            77, 76, 74, 72, 74, 76, 77, 72, 69, 70, 72, 74, 72, 70, 67, 69, 67, 65, 64, 65, 67,
            69, 70, 72, 74, 72, 70]
    i = 0
    for bar, ch in enumerate(prog):
        start = 1 + bar * 3
        root, upper = roots[ch]
        p.play(root, start, 1, 72)
        for beat in (1, 2):
            for n in upper:
                p.play(n - 12 if n > 60 else n, start + beat, 1, 52)
        # Melody: dotted quarter + eighth + quarter, or three quarters.
        if bar % 2 == 0:
            p.play(tune[i % len(tune)], start, 1.5, 78)
            p.play(tune[(i + 1) % len(tune)], start + 1.5, 0.5, 68)
            p.play(tune[(i + 2) % len(tune)], start + 2, 1, 72)
        else:
            for k in range(3):
                p.play(tune[(i + k) % len(tune)], start + k, 1, 72)
        i += 3
    p.play(65, 1 + 16 * 3, 3, 70)
    p.play(41, 1 + 16 * 3, 3, 70)
    p.ghosts(6, 48)
    return write_midi(OUT / "02_waltz_f_major_humanized_3-4.mid", p.notes, file_bpm=120)


def nocturne_e_flat():
    """4/4 in E-flat, LH triplet arpeggios, held RH notes; file says 120."""
    p = Performer(66, jitter=0.010, seed=3, legato=0.97)
    chords = [(39, 46, 51, 55), (44, 48, 51, 56), (46, 50, 53, 58), (39, 46, 51, 55)] * 2
    for bar, (a, b, c, d) in enumerate(chords):
        for beat in range(4):
            for k, n in enumerate([a, b, c] if beat % 2 == 0 else [d, c, b]):
                p.play(n, bar * 4 + beat + k / 3, 1 / 3, 50)
    mel = [(70, 3), (67, 0.5), (68, 0.5), (72, 2), (70, 2), (70, 1.5), (68, 0.5), (67, 1),
           (65, 1), (63, 4), (75, 2), (74, 1), (72, 1), (70, 3), (68, 0.5), (67, 0.5),
           (65, 2), (67, 1), (65, 1), (63, 4)]
    t = 0.0
    for pitch, d in mel:
        p.play(pitch, t, d, 82)
        t += d
    # Inner held notes in the right hand (second voice).
    for bar in range(8):
        p.play(63 if bar % 4 in (0, 3) else 65, bar * 4, 4, 45)
    p.play(39, 32, 4, 60)
    p.play(63, 32, 4, 60)
    p.play(67, 32, 4, 60)
    p.ghosts(5, 32)
    return write_midi(OUT / "03_nocturne_e-flat_triplets_4-4.mid", p.notes, file_bpm=120)


def barcarolle_a_minor():
    """6/8 in A minor (dotted quarter = 56), with an accidental (G#)."""
    p = Performer(56, jitter=0.010, seed=11, beat_ql=1.5)
    harm = [(45, 52, 57), (40, 52, 56), (45, 52, 57), (38, 53, 57),
            (40, 52, 56), (45, 52, 57), (40, 50, 56), (45, 52, 57)]
    for bar, (a, b, c) in enumerate(harm):
        for k, n in enumerate([a, b, c, b, c, b]):
            p.play(n, bar * 3 + k * 0.5, 0.5, 52)
    mel = [(76, 1.5), (74, 0.5), (72, 0.5), (71, 0.5), (72, 1), (71, 0.5), (68, 1.5),
           (69, 1.5), (71, 1.5), (72, 1), (74, 0.5), (76, 1.5), (77, 1.5), (76, 0.5),
           (74, 0.5), (72, 0.5), (71, 1.5), (69, 3)]
    t = 0.0
    for pitch, d in mel:
        p.play(pitch, t, d, 78)
        t += d
    return write_midi(OUT / "04_barcarolle_a_minor_6-8.mid", p.notes, file_bpm=120)


ALL = [clean_c_major, waltz_f_major, nocturne_e_flat, barcarolle_a_minor]


def main():
    for fn in ALL:
        print(fn())


if __name__ == "__main__":
    sys.exit(main())
