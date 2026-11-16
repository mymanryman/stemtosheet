"""End-to-end conversion: MIDI file -> cleaned music21 score -> exports."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from . import analysis, cleanup, export
from .cleanup import QNote
from .midi_io import MidiData, load_midi
from .score_builder import build_score


@dataclass
class Options:
    # None means "detect automatically".
    bpm: float | None = None
    time_signature: tuple[int, int] | None = None
    key: str | None = None              # music21 key name, e.g. "E-" or "f#"
    grid: str = "Auto"                  # see cleanup.GRID_LABELS
    ghost_ms: float = 40.0              # drop notes shorter than this
    ghost_velocity: int = 10            # ... or quieter than this
    fill_rests: float = 0.5             # close rests up to this many quarters (pedal)
    staves: int = 2                     # 2 = piano grand staff, 1 = single staff
    split_mode: str = "smart"           # "smart" or "fixed"
    split_pitch: int = 60               # middle C
    voices: int = 2                     # max voices per staff
    title: str = ""
    composer: str = ""
    page_size: str = "A4"               # "A4" or "Letter"
    export_pdf: bool = True
    export_musicxml: bool = True
    export_midi: bool = False
    musescore_path: str | None = None


@dataclass
class Analysis:
    data: MidiData
    timing: analysis.Timing
    meter: analysis.MeterGuess
    key: analysis.KeyGuess
    grid: str
    ghost_removed: int
    notes: int

    def summary(self) -> str:
        t = self.timing
        src = {"file": "from file", "detected": "detected", "manual": "manual"}[t.source]
        ts = self.meter.time_signature
        return (f"{self.notes} notes, {self.data.duration:.0f}s · "
                f"tempo {t.bpm:g} bpm ({src}, fit {t.confidence:.0%}) · "
                f"{ts[0]}/{ts[1]} · {analysis.key_display_name(self.key.tonic)} · "
                f"grid {self.grid} · {self.ghost_removed} ghost notes removed")


@dataclass
class Result:
    analysis: Analysis
    score: object
    musicxml: str                       # MusicXML text (for preview)
    outputs: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _beat_notes(notes, timing):
    out = []
    for n in notes:
        s, e = timing.beats(n)
        out.append((n.pitch, s, e, n.velocity))
    return out


def prepare(data: MidiData, opts: Options):
    """Run analysis and cleanup. Returns (Analysis, staves, ts, key, bpm)."""
    notes, removed = cleanup.remove_ghost_notes(data.notes, opts.ghost_ms, opts.ghost_velocity)
    notes = analysis.cluster_chords(notes)
    timing = analysis.detect_timing(data, opts.bpm, notes)
    bnotes = _beat_notes(notes, timing)

    trust_file_meter = timing.source == "file"
    meter_guess = analysis.detect_meter(bnotes, data.file_time_signature, trust_file_meter)
    if opts.time_signature:
        meter_guess = analysis.MeterGuess(tuple(opts.time_signature), meter_guess.pickup_beats
                                          if tuple(opts.time_signature) == meter_guess.time_signature
                                          else 0, 1.0)
    ts = meter_guess.time_signature
    compound = analysis.is_compound(ts)

    # Beats -> quarter notes. A detected beat in 6/8 is a dotted quarter.
    beat_ql = 1.5 if (compound and timing.source != "file") else 1.0
    beats_per_bar = (ts[0] * 4 / ts[1]) / beat_ql

    # Shift so the detected downbeat lands on a barline (pickup = leading rests),
    # then drop any completely empty leading bars.
    pickup = meter_guess.pickup_beats % max(1, round(beats_per_bar))
    shift = (beats_per_bar - pickup) % beats_per_bar if pickup else 0.0
    qnotes = [QNote(p, (s + shift) * beat_ql, (e + shift) * beat_ql, v) for p, s, e, v in bnotes]
    bar_ql = ts[0] * 4 / ts[1]
    if qnotes:
        first = min(n.start for n in qnotes)
        drop = math.floor((first + 0.1) / bar_ql) * bar_ql
        if drop > 0:
            for n in qnotes:
                n.start -= drop
                n.end -= drop
        # Tiny negative starts (a note slightly early on beat 1) snap to 0.
        for n in qnotes:
            if n.start < 0:
                n.start = 0.0
            n.end = max(n.end, n.start + 0.05)

    grid = opts.grid if opts.grid != "Auto" else cleanup.choose_grid(qnotes)
    qnotes = cleanup.quantize(qnotes, grid)

    key_guess = analysis.estimate_key(notes)
    key_name = opts.key or key_guess.tonic

    if opts.staves >= 2:
        rh, lh = cleanup.split_hands(qnotes, opts.split_mode, opts.split_pitch)
        staves = [
            {"voices": cleanup.assign_voices(rh, opts.voices, opts.fill_rests), "clef": "treble"},
            {"voices": cleanup.assign_voices(lh, opts.voices, opts.fill_rests), "clef": "bass"},
        ]
    else:
        med = median([n.pitch for n in qnotes]) if qnotes else 60
        staves = [{"voices": cleanup.assign_voices(qnotes, opts.voices, opts.fill_rests),
                   "clef": "treble" if med >= 57 else "bass"}]

    an = Analysis(data, timing, meter_guess, key_guess, grid, removed, len(qnotes))
    shown_bpm = timing.bpm
    return an, staves, ts, key_name, shown_bpm


def analyze(path: str | Path, opts: Options | None = None) -> Analysis:
    return prepare(load_midi(path), opts or Options())[0]


def convert(path: str | Path, opts: Options, out_dir: str | Path | None = None,
            write_files: bool = True) -> Result:
    path = Path(path)
    data = load_midi(path)
    an, staves, ts, key_name, bpm = prepare(data, opts)
    title = opts.title or path.stem
    score = build_score(staves, ts=ts, key_name=key_name, bpm=bpm, title=title,
                        composer=opts.composer, page_size=opts.page_size)
    xml_text = export.to_musicxml_string(score)
    result = Result(an, score, xml_text)
    if not write_files:
        return result

    out_dir = Path(out_dir) if out_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / path.stem
    xml_path = base.with_suffix(".musicxml")
    # MuseScore needs a MusicXML file on disk even if the user didn't ask for one.
    xml_path_written = False
    if opts.export_musicxml or opts.export_pdf:
        target = xml_path if opts.export_musicxml else out_dir / f".{path.stem}.tmp.musicxml"
        target.write_text(xml_text, encoding="utf-8")
        xml_path_written = True
        if opts.export_musicxml:
            result.outputs["musicxml"] = target
    if opts.export_midi:
        midi_path = out_dir / f"{path.stem} (clean).mid"
        export.write_midi(score, midi_path)
        result.outputs["midi"] = midi_path
    if opts.export_pdf and xml_path_written:
        pdf_path = base.with_suffix(".pdf")
        try:
            export.musicxml_to_pdf(target, pdf_path, opts.page_size, opts.musescore_path)
            result.outputs["pdf"] = pdf_path
        except export.MuseScoreError as e:
            result.warnings.append(str(e))
        finally:
            if not opts.export_musicxml:
                target.unlink(missing_ok=True)
    return result
