"""Command-line conversion, useful for batch jobs and testing.

    python -m stemtosheet.cli song.mid --title "My Song" --page Letter
"""
from __future__ import annotations

import argparse
import sys

from .analysis import key_display_name
from .cleanup import GRID_LABELS
from .pipeline import Options, convert


def parse_ts(s: str):
    a, b = s.split("/")
    return int(a), int(b)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stemtosheet", description="Piano MIDI to sheet music.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--out-dir")
    ap.add_argument("--bpm", type=float)
    ap.add_argument("--time-signature", type=parse_ts, metavar="N/D")
    ap.add_argument("--key", help='music21 key, e.g. "E-" (E-flat major) or "f#" (F# minor)')
    ap.add_argument("--grid", choices=GRID_LABELS, default="Auto")
    ap.add_argument("--ghost-ms", type=float, default=40.0)
    ap.add_argument("--ghost-velocity", type=int, default=10)
    ap.add_argument("--fill-rests", type=float, default=0.5,
                    help="close rests up to this many quarter notes (0 = off)")
    ap.add_argument("--single-staff", action="store_true")
    ap.add_argument("--split", choices=["smart", "fixed"], default="smart")
    ap.add_argument("--split-pitch", type=int, default=60)
    ap.add_argument("--title", default="")
    ap.add_argument("--composer", default="")
    ap.add_argument("--page", choices=["A4", "Letter"], default="A4")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-musicxml", action="store_true")
    ap.add_argument("--midi", action="store_true", help="also write a cleaned MIDI file")
    ap.add_argument("--musescore")
    a = ap.parse_args(argv)

    status = 0
    for f in a.files:
        opts = Options(bpm=a.bpm, time_signature=a.time_signature, key=a.key, grid=a.grid,
                       ghost_ms=a.ghost_ms, ghost_velocity=a.ghost_velocity, fill_rests=a.fill_rests,
                       staves=1 if a.single_staff else 2, split_mode=a.split,
                       split_pitch=a.split_pitch, title=a.title, composer=a.composer,
                       page_size=a.page, export_pdf=not a.no_pdf,
                       export_musicxml=not a.no_musicxml, export_midi=a.midi,
                       musescore_path=a.musescore)
        res = convert(f, opts, a.out_dir)
        print(f"{f}\n  {res.analysis.summary()}")
        if a.key is None:
            alts = ", ".join(key_display_name(k) for k, _ in res.analysis.key.alternatives)
            print(f"  other likely keys: {alts}")
        for kind, path in res.outputs.items():
            print(f"  {kind}: {path}")
        for w in res.warnings:
            print(f"  warning: {w}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
