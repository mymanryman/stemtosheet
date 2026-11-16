"""First-page preview with Verovio.

Verovio produces SVG that browsers show perfectly, but Qt's SVG renderer only
supports SVG Tiny, which has no nested <svg> and no nested <tspan>. The
helpers below flatten those so the preview renders in a plain Qt widget
without needing a web engine.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import verovio

from .score_builder import PAGE_SIZES

# Verovio's font resources are global; render one score at a time.
_LOCK = threading.Lock()
_RESOURCES = str(Path(verovio.__file__).resolve().parent / "data")

_TEXT_RE = re.compile(r"<text([^>]*)>(.*?)</text>", re.S)
_FONT_SIZE_RE = re.compile(r'font-size="([\d.]+)px"')
_SMUFL_RE = re.compile(r'<tspan[^>]*font-family="Leipzig"[^>]*>.*?</tspan>', re.S)
_TAG_RE = re.compile(r"</?tspan[^>]*>")


def _add_credits(musicxml: str, title: str, composer: str) -> str:
    """Verovio draws the page header from <credit> elements; add them."""
    from xml.sax.saxutils import escape
    credits = ""
    if title:
        credits += ('<credit page="1"><credit-type>title</credit-type><credit-words '
                    'default-x="600" default-y="1650" justify="center" valign="top" '
                    f'font-size="22">{escape(title)}</credit-words></credit>')
    if composer:
        credits += ('<credit page="1"><credit-type>composer</credit-type><credit-words '
                    'default-x="1100" default-y="1560" justify="right" valign="bottom" '
                    f'font-size="11">{escape(composer)}</credit-words></credit>')
    if not credits:
        return musicxml
    i = musicxml.find("<part-list")
    return musicxml[:i] + credits + musicxml[i:] if i > 0 else musicxml


def render_first_page(musicxml: str, page_size: str = "A4", zoom: int = 40,
                      title: str = "", composer: str = "") -> tuple[str, int]:
    """Return (svg_text, page_count) for the first page."""
    musicxml = _add_credits(musicxml, title, composer)
    w_mm, h_mm = PAGE_SIZES.get(page_size, PAGE_SIZES["A4"])
    with _LOCK:
        return _render(musicxml, w_mm, h_mm, zoom)


def _render(musicxml, w_mm, h_mm, zoom):
    tk = verovio.toolkit(False)
    tk.setResourcePath(_RESOURCES)
    tk.setOptions({
        # Verovio page units are tenths of a millimetre.
        "pageWidth": int(w_mm * 10),
        "pageHeight": int(h_mm * 10),
        "pageMarginLeft": 150, "pageMarginRight": 150,
        "pageMarginTop": 150, "pageMarginBottom": 150,
        "scale": zoom,
        "adjustPageHeight": False,
        "breaks": "auto",
        "footer": "none",
    })
    if not tk.loadData(musicxml):
        raise RuntimeError("Verovio could not read the MusicXML.")
    return tk.renderToSVG(1), tk.getPageCount()


def qt_friendly_svg(svg: str) -> str:
    """Flatten Verovio SVG so QtSvg (SVG Tiny 1.2) can draw it."""
    m = re.search(r'<svg class="definition-scale"[^>]*viewBox="([^"]+)"[^>]*>', svg)
    if m:
        viewbox = m.group(1)
        svg = svg[:m.start()] + "<g>" + svg[m.end():]
        head, inner_close, tail = svg.rsplit("</svg>", 2)
        svg = head + "</g>" + inner_close + "</svg>" + tail
        svg = re.sub(r"<svg ", f'<svg viewBox="{viewbox}" ', svg, count=1)

    def fix_text(mt: re.Match) -> str:
        attrs, body = mt.group(1), mt.group(2)
        plain = _SMUFL_RE.sub("", body)
        sizes = [float(x) for x in _FONT_SIZE_RE.findall(plain)] or \
            [float(x) for x in _FONT_SIZE_RE.findall(body)]
        # Position and alignment may sit on an inner tspan; lift them up.
        for name in ("x", "y", "text-anchor"):
            if f' {name}="' not in attrs:
                m2 = re.search(rf'<tspan[^>]* {name}="([^"]+)"', body)
                if m2:
                    attrs += f' {name}="{m2.group(1)}"'
        body = _SMUFL_RE.sub("\u2669", body)   # music glyph (e.g. tempo note) -> ♩
        body = " ".join(_TAG_RE.sub("", body).split())
        if sizes:
            attrs = _FONT_SIZE_RE.sub("", attrs) + f' font-size="{max(sizes):g}px"'
        return f"<text{attrs}>{body}</text>"

    return _TEXT_RE.sub(fix_text, svg)
