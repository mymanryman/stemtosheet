"""StemToSheet desktop app (PySide6 / Qt)."""
from __future__ import annotations

import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import (QByteArray, QLoggingCategory, QObject, QRunnable, QSettings,
                            QSize, Qt, QThreadPool, QTimer, QUrl, Signal)
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QSplitter, QVBoxLayout,
                               QWidget)

from . import __version__
from .analysis import all_keys, key_display_name
from .cleanup import GRID_LABELS
from .export import find_musescore
from .pipeline import Options, convert
from .preview import qt_friendly_svg, render_first_page

MIDI_EXT = {".mid", ".midi", ".smf", ".kar"}
TIME_SIGNATURES = ["2/4", "3/4", "4/4", "2/2", "6/8", "9/8", "12/8"]
FILL_RESTS = [("Off", 0.0), ("1/16", 0.25), ("1/8", 0.5), ("1/4", 1.0)]
NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


# --------------------------------------------------------------------------
# Background work
# --------------------------------------------------------------------------

class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Job(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.done.emit(self.fn(*self.args))
        except Exception as e:  # report every failure to the UI
            traceback.print_exc()
            self.signals.failed.emit(f"{type(e).__name__}: {e}")


def _preview_job(path: str, opts: Options, req: int):
    res = convert(path, opts, write_files=False)
    svg, pages = render_first_page(res.musicxml, opts.page_size,
                                   title=opts.title or Path(path).stem, composer=opts.composer)
    return req, res.analysis, qt_friendly_svg(svg), pages


def _convert_job(paths: list[str], opts: Options, out_dir: str | None):
    results = []
    for p in paths:
        o = replace(opts, title=opts.title or Path(p).stem) if len(paths) == 1 else opts
        try:
            results.append((p, convert(p, o, out_dir), None))
        except Exception as e:
            traceback.print_exc()
            results.append((p, None, f"{type(e).__name__}: {e}"))
    return results


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

class FileList(QListWidget):
    """List of MIDI files that accepts drag and drop."""
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setMinimumWidth(220)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        self.files_dropped.emit(paths)
        e.acceptProposedAction()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            p = QPainter(self.viewport())
            p.setPen(QColor(128, 128, 128))
            p.drawText(self.viewport().rect(), Qt.AlignCenter | Qt.TextWordWrap,
                       "Drop MIDI files here\nor use Add files…")


class NoteSpin(QSpinBox):
    def textFromValue(self, v):
        return note_name(v)

    def valueFromText(self, text):
        for midi in range(self.minimum(), self.maximum() + 1):
            if note_name(midi).lower() == text.strip().lower():
                return midi
        return self.value()

    def validate(self, text, pos):
        from PySide6.QtGui import QValidator
        return QValidator.Acceptable, text, pos


class PagePreview(QScrollArea):
    """Shows an SVG page scaled to the available width."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.label = QLabel("Select a file to see the first page.")
        self.label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.label.setStyleSheet("background: #e9e9e9; color: #666; padding: 12px;")
        self.setWidget(self.label)
        self.renderer: QSvgRenderer | None = None

    def set_svg(self, svg: str):
        self.renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        self._draw()

    def set_message(self, text: str):
        self.renderer = None
        self.label.setPixmap(QPixmap())
        self.label.setText(text)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._draw()

    def _draw(self):
        if not self.renderer or not self.renderer.isValid():
            return
        size = self.renderer.defaultSize()
        w = max(200, self.viewport().width() - 24)
        h = int(w * size.height() / max(1, size.width()))
        ratio = self.devicePixelRatioF()
        img = QImage(int(w * ratio), int(h * ratio), QImage.Format_ARGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        self.renderer.render(p)
        p.end()
        pix = QPixmap.fromImage(img)
        pix.setDevicePixelRatio(ratio)
        self.label.setText("")
        self.label.setPixmap(pix)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"StemToSheet {__version__}")
        self.settings = QSettings("StemToSheet", "StemToSheet")
        self.pool = QThreadPool.globalInstance()
        self.preview_req = 0
        self.analysis = None
        self.last_outputs: dict[str, Path] = {}
        self.setAcceptDrops(True)

        self.preview_timer = QTimer(self, singleShot=True, interval=500)
        self.preview_timer.timeout.connect(self.refresh_preview)

        splitter = QSplitter()
        splitter.addWidget(self._build_files())
        splitter.addWidget(self._build_settings())
        splitter.addWidget(self._build_preview())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 3)
        splitter.setSizes([240, 360, 620])
        self.setCentralWidget(splitter)
        self.resize(QSize(1320, 860))
        self._load_settings()
        self._update_musescore_label()
        self.statusBar().showMessage("Add a MIDI file to begin.")

    # ---- layout --------------------------------------------------------

    def _build_files(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("<b>MIDI files</b>"))
        self.files = FileList()
        self.files.files_dropped.connect(self.add_files)
        self.files.itemSelectionChanged.connect(self._on_file_selected)
        v.addWidget(self.files, 1)
        row = QHBoxLayout()
        add = QPushButton("Add files…")
        add.clicked.connect(self._pick_files)
        rm = QPushButton("Remove")
        rm.clicked.connect(self._remove_selected)
        row.addWidget(add)
        row.addWidget(rm)
        v.addLayout(row)
        self.detected = QLabel("")
        self.detected.setWordWrap(True)
        self.detected.setStyleSheet("color: #555;")
        v.addWidget(self.detected)
        return w

    def _build_settings(self) -> QWidget:
        inner = QWidget()
        v = QVBoxLayout(inner)

        # Timing
        g = QGroupBox("Timing")
        f = QFormLayout(g)
        trow = QHBoxLayout()
        self.tempo_auto = QCheckBox("Auto")
        self.tempo_auto.setChecked(True)
        self.tempo = QDoubleSpinBox(minimum=20, maximum=300, decimals=1, value=100, suffix=" bpm")
        self.tempo.setEnabled(False)
        self.tempo_auto.toggled.connect(lambda on: self.tempo.setEnabled(not on))
        trow.addWidget(self.tempo_auto)
        trow.addWidget(self.tempo, 1)
        f.addRow("Tempo", trow)
        self.ts = QComboBox()
        self.ts.addItem("Auto", None)
        for t in TIME_SIGNATURES:
            self.ts.addItem(t, tuple(int(x) for x in t.split("/")))
        f.addRow("Time signature", self.ts)
        self.grid = QComboBox()
        for label in GRID_LABELS:
            self.grid.addItem(label, label)
        f.addRow("Quantize grid", self.grid)
        self.fill = QComboBox()
        for label, val in FILL_RESTS:
            self.fill.addItem(label, val)
        self.fill.setCurrentIndex(2)
        self.fill.setToolTip("Hold notes through short rests, the way the sustain pedal would.")
        f.addRow("Fill short rests", self.fill)
        v.addWidget(g)

        # Key and cleanup
        g = QGroupBox("Key and cleanup")
        f = QFormLayout(g)
        self.key = QComboBox()
        self.key.addItem("Auto", None)
        for k in all_keys():
            self.key.addItem(key_display_name(k), k)
        f.addRow("Key", self.key)
        self.ghost_ms = QSpinBox(minimum=0, maximum=500, value=40, suffix=" ms")
        self.ghost_ms.setToolTip("Notes shorter than this are dropped.")
        f.addRow("Drop notes shorter than", self.ghost_ms)
        self.ghost_vel = QSpinBox(minimum=0, maximum=127, value=10)
        self.ghost_vel.setToolTip("Notes with a velocity below this are dropped (0 keeps all).")
        f.addRow("Drop notes quieter than", self.ghost_vel)
        v.addWidget(g)

        # Layout
        g = QGroupBox("Layout")
        f = QFormLayout(g)
        self.staves = QComboBox()
        self.staves.addItem("Two staves (piano)", 2)
        self.staves.addItem("One staff", 1)
        f.addRow("Staves", self.staves)
        srow = QHBoxLayout()
        self.split_mode = QComboBox()
        self.split_mode.addItem("Smart (follows the hands)", "smart")
        self.split_mode.addItem("Fixed split", "fixed")
        self.split_pitch = NoteSpin(minimum=36, maximum=84, value=60)
        srow.addWidget(self.split_mode, 1)
        srow.addWidget(self.split_pitch)
        f.addRow("Hand split", srow)
        self.staves.currentIndexChanged.connect(
            lambda: (self.split_mode.setEnabled(self.staves.currentData() == 2),
                     self.split_pitch.setEnabled(self.staves.currentData() == 2)))
        self.title = QLineEdit(placeholderText="File name")
        f.addRow("Title", self.title)
        self.composer = QLineEdit()
        f.addRow("Composer", self.composer)
        self.page = QComboBox()
        self.page.addItems(["A4", "Letter"])
        f.addRow("Page size", self.page)
        v.addWidget(g)

        # Export
        g = QGroupBox("Export")
        f = QFormLayout(g)
        erow = QHBoxLayout()
        self.ex_pdf = QCheckBox("PDF")
        self.ex_xml = QCheckBox("MusicXML")
        self.ex_midi = QCheckBox("MIDI")
        self.ex_pdf.setChecked(True)
        self.ex_xml.setChecked(True)
        for c in (self.ex_pdf, self.ex_xml, self.ex_midi):
            erow.addWidget(c)
        f.addRow("Formats", erow)
        orow = QHBoxLayout()
        self.out_dir = QLineEdit(placeholderText="Next to the MIDI file")
        ob = QPushButton("…")
        ob.setFixedWidth(32)
        ob.clicked.connect(self._pick_out_dir)
        orow.addWidget(self.out_dir, 1)
        orow.addWidget(ob)
        f.addRow("Save to", orow)
        mrow = QHBoxLayout()
        self.ms_label = QLabel()
        self.ms_label.setWordWrap(True)
        self.ms_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        mb = QPushButton("…")
        mb.setFixedWidth(32)
        mb.clicked.connect(self._pick_musescore)
        mrow.addWidget(self.ms_label, 1)
        mrow.addWidget(mb)
        f.addRow("MuseScore", mrow)
        v.addWidget(g)

        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setMinimumHeight(40)
        self.convert_btn.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.convert_btn.clicked.connect(self.run_convert)
        v.addWidget(self.convert_btn)
        orow2 = QHBoxLayout()
        self.open_pdf = QPushButton("Open PDF")
        self.open_pdf.setEnabled(False)
        self.open_pdf.clicked.connect(lambda: self._open(self.last_outputs.get("pdf")))
        self.open_folder = QPushButton("Open folder")
        self.open_folder.setEnabled(False)
        self.open_folder.clicked.connect(
            lambda: self._open(next(iter(self.last_outputs.values())).parent
                               if self.last_outputs else None))
        orow2.addWidget(self.open_pdf)
        orow2.addWidget(self.open_folder)
        v.addLayout(orow2)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(90)
        v.addWidget(self.log, 1)

        # Any setting change refreshes the preview.
        for wdg in (self.ts, self.grid, self.fill, self.key, self.staves, self.split_mode,
                    self.page):
            wdg.currentIndexChanged.connect(self._schedule_preview)
        for wdg in (self.tempo, self.ghost_ms, self.ghost_vel, self.split_pitch):
            wdg.valueChanged.connect(self._schedule_preview)
        self.tempo_auto.toggled.connect(self._schedule_preview)
        for wdg in (self.title, self.composer):
            wdg.editingFinished.connect(self._schedule_preview)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setMinimumWidth(340)
        return scroll

    def _build_preview(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Preview</b> (first page)"))
        row.addStretch(1)
        self.page_info = QLabel("")
        row.addWidget(self.page_info)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_preview)
        row.addWidget(refresh)
        v.addLayout(row)
        self.preview = PagePreview()
        v.addWidget(self.preview, 1)
        return w

    # ---- settings ------------------------------------------------------

    def _load_settings(self):
        s = self.settings
        self.page.setCurrentText(s.value("page", "A4"))
        self.out_dir.setText(s.value("out_dir", ""))
        self.composer.setText(s.value("composer", ""))
        self.ghost_ms.setValue(int(s.value("ghost_ms", 40)))
        self.ghost_vel.setValue(int(s.value("ghost_vel", 10)))
        self.ex_midi.setChecked(s.value("ex_midi", "false") == "true")

    def closeEvent(self, e):
        s = self.settings
        s.setValue("page", self.page.currentText())
        s.setValue("out_dir", self.out_dir.text())
        s.setValue("composer", self.composer.text())
        s.setValue("ghost_ms", self.ghost_ms.value())
        s.setValue("ghost_vel", self.ghost_vel.value())
        s.setValue("ex_midi", "true" if self.ex_midi.isChecked() else "false")
        super().closeEvent(e)

    def options(self) -> Options:
        return Options(
            bpm=None if self.tempo_auto.isChecked() else self.tempo.value(),
            time_signature=self.ts.currentData(),
            key=self.key.currentData(),
            grid=self.grid.currentData(),
            fill_rests=self.fill.currentData(),
            ghost_ms=self.ghost_ms.value(),
            ghost_velocity=self.ghost_vel.value(),
            staves=self.staves.currentData(),
            split_mode=self.split_mode.currentData(),
            split_pitch=self.split_pitch.value(),
            title=self.title.text().strip(),
            composer=self.composer.text().strip(),
            page_size=self.page.currentText(),
            export_pdf=self.ex_pdf.isChecked(),
            export_musicxml=self.ex_xml.isChecked(),
            export_midi=self.ex_midi.isChecked(),
            musescore_path=self.settings.value("musescore", "") or None,
        )

    def _update_musescore_label(self):
        exe = find_musescore(self.settings.value("musescore", "") or None)
        if exe:
            self.ms_label.setText(Path(exe).name)
            self.ms_label.setToolTip(exe)
            self.ms_label.setStyleSheet("")
        else:
            self.ms_label.setText("Not found: install MuseScore 4 to make PDFs")
            self.ms_label.setStyleSheet("color: #b00020;")

    # ---- files ---------------------------------------------------------

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def add_files(self, paths: list[str]):
        existing = {self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())}
        added = None
        for p in paths:
            path = Path(p)
            if path.is_dir():
                self.add_files([str(x) for x in sorted(path.iterdir())])
                continue
            if path.suffix.lower() not in MIDI_EXT or str(path) in existing:
                continue
            item = QListWidgetItem(path.name)
            item.setData(Qt.UserRole, str(path))
            item.setToolTip(str(path))
            self.files.addItem(item)
            added = item
        if added:
            self.files.clearSelection()
            added.setSelected(True)
            self.files.setCurrentItem(added)

    def _pick_files(self):
        start = self.settings.value("last_dir", str(Path.home()))
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose MIDI files", start,
                                                "MIDI files (*.mid *.midi);;All files (*)")
        if paths:
            self.settings.setValue("last_dir", str(Path(paths[0]).parent))
            self.add_files(paths)

    def _remove_selected(self):
        for item in self.files.selectedItems():
            self.files.takeItem(self.files.row(item))
        if self.files.count() == 0:
            self.preview.set_message("Select a file to see the first page.")
            self.detected.setText("")

    def _pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Save files to", self.out_dir.text() or str(Path.home()))
        if d:
            self.out_dir.setText(d)

    def _pick_musescore(self):
        path, _ = QFileDialog.getOpenFileName(self, "Find the MuseScore program")
        if path:
            # On macOS people pick the .app bundle; use the binary inside it.
            if path.endswith(".app"):
                path = str(Path(path) / "Contents" / "MacOS" / "mscore")
            self.settings.setValue("musescore", path)
            self._update_musescore_label()

    def _current_path(self) -> str | None:
        item = self.files.currentItem()
        if item is None and self.files.count():
            item = self.files.item(0)
        return item.data(Qt.UserRole) if item else None

    def _on_file_selected(self):
        self.refresh_preview()

    # ---- preview -------------------------------------------------------

    def _schedule_preview(self, *_):
        if self._current_path():
            self.preview_timer.start()

    def refresh_preview(self):
        path = self._current_path()
        if not path:
            return
        self.preview_req += 1
        self.page_info.setText("Rendering…")
        job = Job(_preview_job, path, self.options(), self.preview_req)
        job.signals.done.connect(self._preview_done)
        job.signals.failed.connect(self._preview_failed)
        self.pool.start(job)

    def _preview_done(self, payload):
        req, analysis, svg, pages = payload
        if req != self.preview_req:
            return  # a newer preview is on its way
        self.analysis = analysis
        self.preview.set_svg(svg)
        self.page_info.setText(f"Page 1 of {pages}")
        self._show_detected(analysis)

    def _preview_failed(self, msg):
        self.page_info.setText("")
        self.preview.set_message(f"Could not render this file.\n\n{msg}")

    def _show_detected(self, a):
        t, m, k = a.timing, a.meter, a.key
        src = {"file": "from the file's tempo map", "detected": "tracked from the notes",
               "manual": "manual"}[t.source]
        ts = m.time_signature
        alts = ", ".join(key_display_name(x) for x, _ in k.alternatives[:2])
        self.detected.setText(
            f"<b>Detected</b><br>Tempo: {t.bpm:g} bpm ({src})<br>"
            f"Beat fit: {t.confidence:.0%}<br>Time signature: {ts[0]}/{ts[1]}<br>"
            f"Key: {key_display_name(k.tonic)}<br><small>also possible: {alts}</small><br>"
            f"Grid: {a.grid}<br>Notes: {a.notes} ({a.ghost_removed} ghost notes dropped)")
        self.ts.setItemText(0, f"Auto ({ts[0]}/{ts[1]})")
        self.key.setItemText(0, f"Auto ({key_display_name(k.tonic)})")
        self.grid.setItemText(0, f"Auto ({a.grid})")
        if self.tempo_auto.isChecked():
            self.tempo.blockSignals(True)
            self.tempo.setValue(t.bpm)
            self.tempo.blockSignals(False)

    # ---- convert -------------------------------------------------------

    def run_convert(self):
        items = self.files.selectedItems() or [self.files.item(i) for i in range(self.files.count())]
        paths = [it.data(Qt.UserRole) for it in items]
        if not paths:
            QMessageBox.information(self, "StemToSheet", "Add a MIDI file first.")
            return
        opts = self.options()
        if not (opts.export_pdf or opts.export_musicxml or opts.export_midi):
            QMessageBox.information(self, "StemToSheet", "Tick at least one export format.")
            return
        if len(paths) > 1 and opts.title:
            opts = replace(opts, title="")  # each file keeps its own name as title
        self.convert_btn.setEnabled(False)
        self.convert_btn.setText("Converting…")
        self._t0 = time.time()
        job = Job(_convert_job, paths, opts, self.out_dir.text().strip() or None)
        job.signals.done.connect(self._convert_done)
        job.signals.failed.connect(self._convert_failed)
        self.pool.start(job)

    def _convert_done(self, results):
        self.convert_btn.setEnabled(True)
        self.convert_btn.setText("Convert")
        for path, res, err in results:
            name = Path(path).name
            if err:
                self.log.appendPlainText(f"✗ {name}: {err}")
                continue
            self.last_outputs = dict(res.outputs)
            made = ", ".join(f"{k.upper() if k != 'musicxml' else 'MusicXML'}" for k in res.outputs)
            self.log.appendPlainText(f"✓ {name} → {made or 'nothing'}")
            for w in res.warnings:
                self.log.appendPlainText(f"  ! {w}")
        self.open_pdf.setEnabled("pdf" in self.last_outputs)
        self.open_folder.setEnabled(bool(self.last_outputs))
        self.statusBar().showMessage(f"Done in {time.time() - self._t0:.1f}s", 8000)

    def _convert_failed(self, msg):
        self.convert_btn.setEnabled(True)
        self.convert_btn.setText("Convert")
        self.log.appendPlainText(f"✗ {msg}")

    def _open(self, path):
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def main(argv=None) -> int:
    QLoggingCategory.setFilterRules("qt.svg.warning=false\nqt.svg.debug=false")
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("StemToSheet")
    win = MainWindow()
    files = [a for a in app.arguments()[1:] if Path(a).suffix.lower() in MIDI_EXT]
    if files:
        win.add_files(files)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
