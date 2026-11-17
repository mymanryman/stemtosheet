# StemToSheet

Turn a piano MIDI file (for example a piano stem exported from RipX DAW) into
a printable score. Drop the file in, check the preview, press **Convert**, and
you get a PDF, plus MusicXML and a cleaned-up MIDI file if you want them.

Phase 1 handles MIDI only. Turning audio into MIDI comes in a later phase.

## What it does to your MIDI

| Step | What happens | Can you override it? |
|---|---|---|
| Ghost notes | Drops notes shorter than 40 ms or with velocity below 10 (stem separation leaves lots of these). | Yes, both thresholds |
| Rolled chords | Notes that start within 70 ms of each other are lined up as one chord. | No |
| Tempo | If the file's own tempo map already fits the notes (a DAW export made against a click), it is used. Otherwise the beats are tracked from the notes themselves, following rubato and tempo drift. | Yes, set a tempo |
| Time signature | Guessed from where long, loud and bass notes fall; triplet-heavy music becomes 6/8 or 12/8. A pickup becomes leading rests. | Yes |
| Quantize | Auto picks 1/8, 1/16, 1/32 or a mix with triplets, choosing the coarsest grid that still fits. Triplets are decided per beat. | Yes |
| Fill short rests | Holds notes through short gaps, as the sustain pedal would, so you don't get a 16th rest after every note. | Yes (off, 1/16, 1/8, 1/4) |
| Key | Krumhansl-Kessler key finding, then notes are spelled for that key (E♭ not D♯ in E♭ major). | Yes |
| Staves | Two staves (treble and bass, braced), split by a "smart" hand tracker that follows each hand's position and reach, or at a fixed note. Or one staff. | Yes |
| Voices | Notes held while the same hand plays on go into a second voice instead of being cut short. | No |

Output: title and composer, A4 (default) or Letter, PDF via MuseScore, MusicXML,
and MIDI.

## Setup

You need Python 3.10 or newer and MuseScore 4. Total download is about 400 MB
(mostly MuseScore and Qt).

### 1. Install MuseScore 4 (makes the PDFs)

* **Windows and Mac:** download from <https://musescore.org/download>. The
  download page may offer Muse Hub, which is optional: the plain MuseScore
  Studio 4 installer is enough. Install it to the default place.
* **Linux:** use the AppImage from the same page, or `flatpak install flathub org.musescore.MuseScore`.

StemToSheet finds MuseScore by itself in the default locations:

* Windows: `C:\Program Files\MuseScore 4\bin\MuseScore4.exe`
* Mac: `/Applications/MuseScore 4.app/Contents/MacOS/mscore`

If it shows "Not found" in the app, click **…** next to *MuseScore* and pick the
program (on a Mac you can pick `MuseScore 4.app`). MuseScore 3 also works.

Without MuseScore you still get MusicXML, which you can open in MuseScore,
Dorico, Sibelius or Finale and print from there.

### 2. Install Python

* **Windows:** install Python 3.12 from <https://www.python.org/downloads/>
  and tick **"Add python.exe to PATH"** in the first installer screen.
* **Mac:** macOS comes with Python 3.9, which is too old. Install 3.12 from
  <https://www.python.org/downloads/macos/> (or `brew install python@3.12`)
  and use `python3.12` in step 3. The old one can stay installed.

### 3. Install StemToSheet

Open a terminal (PowerShell on Windows, Terminal on Mac) in this folder:

```bash
# Windows
py -m venv .venv
.venv\Scripts\activate
pip install -e .

# Mac / Linux
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 4. Run it

```bash
stemtosheet            # opens the app
```

or `python -m stemtosheet`. Next time, activate the environment again
(step 3, second line) and run `stemtosheet`.

## Using the app

1. Drag one or more `.mid` files onto the window, or click **Add files…**.
2. The preview shows page 1 of the result and the left column shows what was
   detected (tempo, time signature, key, grid). Every setting change updates
   the preview.
3. If something is off, change it: the most common fixes are the time
   signature, the tempo (try double or half the detected one), and the grid.
4. Fill in title and composer, choose A4 or Letter and the formats.
5. Press **Convert**. Files are saved next to the MIDI file unless you choose a
   folder under *Save to*. **Open PDF** opens the result.

With several files selected, Convert processes them all with the same
settings, each titled after its file name.

## Command line

The same pipeline is available without the window, handy for batches:

```bash
stemtosheet-cli song.mid --title "Song" --composer "Me"
stemtosheet-cli *.mid -o out --page Letter --midi
stemtosheet-cli song.mid --bpm 72 --time-signature 3/4 --key "E-" --grid "1/16 + triplets"
```

Keys use music21 names: `E-` is E♭ major, `f#` is F♯ minor (lower case = minor).
Run `stemtosheet-cli --help` for everything.

## Tests

```bash
pip install -e ".[dev]"
python tests/make_test_midi.py   # (re)creates the sample MIDI files in samples/
pytest
```

`samples/` contains four generated piano pieces that mimic stem exports: a
clean DAW-style file, a loosely played 3/4 waltz whose file claims the wrong
tempo, a 4/4 piece with triplet arpeggios, and a 6/8 barcarolle. To include a
real RipX export in the test run:

```bash
STEMTOSHEET_REAL_MIDI="path/to/export.mid" pytest
```

## Notes on RipX exports

RipX writes a tempo map that changes every bar, but in exports we've seen the
notes don't line up with that map's beats, so StemToSheet tracks the beats
from the notes instead. It also writes lots of aftertouch and pitch-bend
data, which is ignored. Piano stems often contain quiet bleed notes from
other instruments: raise *Drop notes quieter than* if you see stray notes.

## Project layout

```
stemtosheet/
  midi_io.py        read MIDI (times in seconds and file beats)
  analysis.py       beat tracking, tempo, time signature, key
  cleanup.py        ghost notes, quantizing, hand split, voices
  score_builder.py  music21 score: measures, ties, spelling, layout
  export.py         MusicXML, MIDI, MuseScore PDF
  preview.py        Verovio first-page preview
  pipeline.py       ties it together (Options -> Result)
  gui.py            the desktop app (PySide6)
  cli.py            command line
tests/              sample generator and tests
samples/            generated test MIDI
```
