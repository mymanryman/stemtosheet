"""Read a MIDI file into a flat list of note events with times in seconds.

We use mido rather than music21's MIDI reader because we want the raw,
unquantized timing: all cleanup (tempo detection, quantizing, splitting)
happens in our own pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import mido


@dataclass
class NoteEvent:
    pitch: int        # MIDI note number
    start: float      # seconds
    end: float        # seconds
    velocity: int = 64
    # Position on the file's own beat grid, in quarter notes.
    start_q: float = 0.0
    end_q: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class MidiData:
    path: Path
    notes: list[NoteEvent]
    # Tempo and meter as written in the file (first occurrence), if any.
    file_bpm: float | None = None
    file_time_signature: tuple[int, int] | None = None
    tempo_changes: int = 0
    ticks_per_beat: int = 480
    track_names: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max((n.end for n in self.notes), default=0.0)


def load_midi(path: str | Path) -> MidiData:
    path = Path(path)
    mid = mido.MidiFile(str(path))

    file_bpm = None
    file_ts = None
    tempo_changes = 0
    track_names: list[str] = []
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                tempo_changes += 1
                if file_bpm is None:
                    file_bpm = mido.tempo2bpm(msg.tempo)
            elif msg.type == "time_signature" and file_ts is None:
                file_ts = (msg.numerator, msg.denominator)
            elif msg.type == "track_name" and msg.name.strip():
                track_names.append(msg.name.strip())

    # Walk all tracks merged, in ticks, and convert ticks to seconds with the
    # tempo map so each note has both a time and a position on the file grid.
    tpb = mid.ticks_per_beat or 480
    notes: list[NoteEvent] = []
    active: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
    tick = 0
    seconds = 0.0
    tempo = 500000  # microseconds per quarter (120 bpm)
    for msg in mido.merge_tracks(mid.tracks):
        if msg.time:
            seconds += mido.tick2second(msg.time, tpb, tempo)
            tick += msg.time
        q = tick / tpb
        if msg.type == "set_tempo":
            tempo = msg.tempo
        elif msg.type == "note_on" and msg.velocity > 0:
            if msg.channel == 9:  # ignore General MIDI drums
                continue
            active.setdefault((msg.channel, msg.note), []).append((seconds, q, msg.velocity))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            stack = active.get((msg.channel, msg.note))
            if stack:
                start, start_q, vel = stack.pop(0)
                notes.append(NoteEvent(msg.note, start, seconds, vel, start_q, q))
    # Close notes that never received a note-off.
    for (_, pitch), stack in active.items():
        for start, start_q, vel in stack:
            notes.append(NoteEvent(pitch, start, max(seconds, start + 0.1), vel,
                                   start_q, max(tick / tpb, start_q + 0.25)))

    notes.sort(key=lambda n: (n.start, n.pitch))
    return MidiData(
        path=path,
        notes=notes,
        file_bpm=file_bpm,
        file_time_signature=file_ts,
        tempo_changes=tempo_changes,
        ticks_per_beat=mid.ticks_per_beat,
        track_names=track_names,
    )
