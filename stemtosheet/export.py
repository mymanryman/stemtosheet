"""MusicXML / MIDI writing and PDF rendering through MuseScore's command line."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from music21 import musicxml

from .score_builder import PAGE_SIZES


class MuseScoreError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# MusicXML and MIDI
# --------------------------------------------------------------------------

def to_musicxml_string(score) -> str:
    exporter = musicxml.m21ToXml.GeneralObjectExporter(score)
    data = exporter.parse()
    return data.decode("utf-8") if isinstance(data, bytes) else data


def write_midi(score, path: Path) -> Path:
    score.write("midi", fp=str(path))
    return path


# --------------------------------------------------------------------------
# Finding MuseScore
# --------------------------------------------------------------------------

def _candidates() -> list[str]:
    system = platform.system()
    paths: list[str] = []
    env = os.environ.get("MUSESCORE_PATH")
    if env:
        paths.append(env)
    if system == "Windows":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        for root in filter(None, roots):
            paths += [
                os.path.join(root, "MuseScore 4", "bin", "MuseScore4.exe"),
                os.path.join(root, "MuseScore 3", "bin", "MuseScore3.exe"),
                os.path.join(root, "Programs", "MuseScore 4", "bin", "MuseScore4.exe"),
            ]
    elif system == "Darwin":
        for app in ("MuseScore 4.app", "MuseScore 3.app"):
            paths += [f"/Applications/{app}/Contents/MacOS/mscore",
                      os.path.expanduser(f"~/Applications/{app}/Contents/MacOS/mscore")]
    for name in ("MuseScore4", "mscore4portable", "musescore4", "mscore", "musescore",
                 "MuseScore3", "mscore3", "musescore3"):
        found = shutil.which(name)
        if found:
            paths.append(found)
    return paths


def find_musescore(preferred: str | None = None) -> str | None:
    if preferred and Path(preferred).exists():
        return preferred
    for p in _candidates():
        if p and Path(p).exists():
            return p
    return None


def _env() -> dict:
    env = dict(os.environ)
    # On Linux servers without a display, let Qt render off-screen.
    if platform.system() == "Linux" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def musescore_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=60, env=_env())
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out.stdout + out.stderr)
        return m.group(1) if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def style_xml(page_size: str, major_version: int = 4) -> str:
    """A minimal MuseScore style file that sets page size and margins (inches)."""
    w_mm, h_mm = PAGE_SIZES.get(page_size, PAGE_SIZES["A4"])
    inch = lambda mm: f"{mm / 25.4:.5f}"
    margin = 15.0
    version = "4.40" if major_version >= 4 else "3.02"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<museScore version="{version}">
  <Style>
    <pageWidth>{inch(w_mm)}</pageWidth>
    <pageHeight>{inch(h_mm)}</pageHeight>
    <pagePrintableWidth>{inch(w_mm - 2 * margin)}</pagePrintableWidth>
    <pageEvenLeftMargin>{inch(margin)}</pageEvenLeftMargin>
    <pageOddLeftMargin>{inch(margin)}</pageOddLeftMargin>
    <pageEvenTopMargin>{inch(margin)}</pageEvenTopMargin>
    <pageEvenBottomMargin>{inch(margin)}</pageEvenBottomMargin>
    <pageOddTopMargin>{inch(margin)}</pageOddTopMargin>
    <pageOddBottomMargin>{inch(margin)}</pageOddBottomMargin>
    <pageTwosided>0</pageTwosided>
  </Style>
</museScore>
"""


def musicxml_to_pdf(xml_path: Path, pdf_path: Path, page_size: str = "A4",
                    musescore_path: str | None = None, timeout: int = 180) -> Path:
    exe = find_musescore(musescore_path)
    if not exe:
        raise MuseScoreError(
            "MuseScore was not found, so no PDF was made. Install MuseScore 4 "
            "(musescore.org) or set its path in Settings. The MusicXML file can "
            "still be opened in MuseScore and printed from there.")
    ver = musescore_version(exe)
    major = int(ver.split(".")[0]) if ver else 4
    with tempfile.TemporaryDirectory() as tmp:
        style = Path(tmp) / "page.mss"
        style.write_text(style_xml(page_size, major), encoding="utf-8")
        cmd = [exe, "-S", str(style), "-o", str(pdf_path), str(xml_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, env=_env())
        except subprocess.TimeoutExpired:
            raise MuseScoreError("MuseScore took too long to make the PDF.")
        except OSError as e:
            raise MuseScoreError(f"Could not start MuseScore at {exe}: {e}")
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        raise MuseScoreError("MuseScore did not produce a PDF.\n" + "\n".join(tail))
    return pdf_path
