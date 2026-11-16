import os
import sys
from pathlib import Path

import pytest
from music21 import converter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import make_test_midi  # noqa: E402
from stemtosheet import cleanup, export  # noqa: E402
from stemtosheet.cleanup import QNote  # noqa: E402
from stemtosheet.pipeline import Options, analyze, convert  # noqa: E402
from stemtosheet.score_builder import spelling_table  # noqa: E402

SAMPLES = ROOT / "samples"


@pytest.fixture(scope="session", autouse=True)
def samples():
    for fn in make_test_midi.ALL:
        fn()
    return SAMPLES


def sample(prefix):
    return next(SAMPLES.glob(prefix + "*.mid"))


# ---- analysis -------------------------------------------------------------

def test_clean_file_uses_its_own_grid():
    a = analyze(sample("01"))
    assert a.timing.source == "file"
    assert a.timing.bpm == pytest.approx(100, abs=0.5)
    assert a.meter.time_signature == (4, 4)
    assert a.key.tonic == "C"


@pytest.mark.parametrize("prefix,bpm", [("02", 92), ("03", 66), ("04", 56)])
def test_tempo_tracked_when_file_tempo_is_wrong(prefix, bpm):
    a = analyze(sample(prefix))  # these files all claim 120 bpm
    assert a.timing.source == "detected"
    assert a.timing.bpm == pytest.approx(bpm, rel=0.03)
    assert a.timing.confidence > 0.8


def test_waltz_is_three_four_in_f():
    a = analyze(sample("02"))
    assert a.meter.time_signature == (3, 4)
    assert a.key.tonic == "F"


def test_triplet_feel_gives_compound_meter():
    a = analyze(sample("04"))
    assert a.meter.time_signature in {(6, 8), (12, 8)}
    assert a.key.tonic == "a"


def test_ghost_notes_removed():
    a = analyze(sample("02"))
    assert a.ghost_removed == 6


# ---- cleanup --------------------------------------------------------------

def test_quantize_snaps_to_grid():
    notes = [QNote(60, 0.03, 0.97), QNote(62, 1.02, 1.49), QNote(64, 1.51, 2.1)]
    q = cleanup.quantize(notes, "1/8")
    assert [(n.start, n.end) for n in q] == [(0, 1), (1, 1.5), (1.5, 2)]


def test_quantize_triplets_per_beat():
    notes = [QNote(60, 0.0, 0.3), QNote(62, 0.34, 0.64), QNote(64, 0.65, 0.98),
             QNote(65, 1.0, 1.5), QNote(67, 1.52, 2.0)]
    q = cleanup.quantize(notes, "1/16 + triplets")
    starts = [round(n.start, 3) for n in q]
    assert starts == [0, 0.333, 0.667, 1, 1.5]


def test_fixed_split():
    notes = [QNote(p, 0, 1) for p in (40, 59, 60, 72)]
    rh, lh = cleanup.split_hands(notes, "fixed", 60)
    assert [n.pitch for n in rh] == [60, 72]
    assert [n.pitch for n in lh] == [40, 59]


def test_smart_split_keeps_hands_together():
    # A wide two-handed chord splits between the hands, not mid-hand.
    notes = [QNote(p, 0, 1) for p in (40, 47, 52, 64, 67, 72)]
    rh, lh = cleanup.split_hands(notes, "smart", 60)
    assert sorted(n.pitch for n in rh) == [64, 67, 72]
    # A melody that dips just below middle C stays in the right hand.
    notes = [QNote(72, 0, 1), QNote(67, 1, 2), QNote(59, 2, 3), QNote(64, 3, 4),
             QNote(36, 0, 4)]
    rh, lh = cleanup.split_hands(notes, "smart", 60)
    assert [n.pitch for n in lh] == [36]


def test_spelling_follows_key():
    assert spelling_table("E-")[3] == "E-"
    assert spelling_table("E")[8] == "G#"
    assert spelling_table("a")[8] == "G#"     # leading tone in A minor
    assert spelling_table("F")[10] == "B-"


# ---- conversion -----------------------------------------------------------

def test_convert_writes_files(tmp_path):
    opts = Options(export_pdf=False, export_midi=True, title="Waltz", composer="Test")
    res = convert(sample("02"), opts, tmp_path)
    assert res.outputs["musicxml"].exists()
    assert res.outputs["midi"].exists()
    score = converter.parse(str(res.outputs["musicxml"]))
    assert len(score.parts) == 2
    xml = res.outputs["musicxml"].read_text(encoding="utf-8")
    assert "<work-title>Waltz</work-title>" in xml
    assert ">Test</creator>" in xml
    m1 = score.parts[0].getElementsByClass("Measure")[0]
    assert m1.timeSignature.ratioString == "3/4"
    # Every measure in every part is exactly full.
    for part in score.parts:
        for m in part.getElementsByClass("Measure"):
            assert m.duration.quarterLength == pytest.approx(3.0)


def test_single_staff_and_letter(tmp_path):
    opts = Options(staves=1, page_size="Letter", export_pdf=False)
    res = convert(sample("01"), opts, tmp_path)
    xml = res.musicxml
    assert "<page-width>1234</page-width>" in xml   # 215.9 mm at 40 tenths / 7 mm
    assert len(converter.parse(xml, format="musicxml").parts) == 1


def test_manual_overrides():
    opts = Options(bpm=90, time_signature=(4, 4), key="d", grid="1/16",
                   export_pdf=False, export_musicxml=False)
    res = convert(sample("02"), opts, write_files=False)
    assert res.analysis.timing.source == "manual"
    assert "<fifths>-1</fifths>" in res.musicxml
    assert "<beats>4</beats>" in res.musicxml


def test_preview_renders():
    from stemtosheet.preview import qt_friendly_svg, render_first_page
    res = convert(sample("03"), Options(), write_files=False)
    svg, pages = render_first_page(res.musicxml, "A4", title="Nocturne")
    assert pages >= 1
    flat = qt_friendly_svg(svg)
    assert 'class="definition-scale"' not in flat
    assert "Nocturne" in flat


@pytest.mark.skipif(not export.find_musescore(), reason="MuseScore not installed")
def test_pdf_via_musescore(tmp_path):
    res = convert(sample("01"), Options(export_musicxml=False), tmp_path)
    assert res.outputs["pdf"].stat().st_size > 1000
    assert not list(tmp_path.glob(".*.musicxml"))  # temp file cleaned up


REAL = os.environ.get("STEMTOSHEET_REAL_MIDI")


@pytest.mark.skipif(not REAL, reason="set STEMTOSHEET_REAL_MIDI to a RipX export")
def test_real_ripx_export():
    a = analyze(REAL)
    assert a.timing.source == "detected"
    assert 50 < a.timing.bpm < 150
    assert a.timing.confidence > 0.5
