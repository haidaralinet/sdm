"""
Subzeo Download Manager - GUI
Run:  python subzeo.py
"""
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QGuiApplication, QIcon,
                           QKeySequence)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QTableWidgetSelectionRange, QTimeEdit, QToolBar, QVBoxLayout,
    QWidget,
)

from subzeo_engine import (ALL_EXT, VIDEO_FORMATS, Manager, Scheduler, ext_of, fmt_eta,
                           fmt_size, fmt_speed, power_action)

API_PORT = 16800


def resource(rel):
    """Path to a bundled file (works from source and from a PyInstaller build)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / rel)

FILTERS = ["All Downloads", "Unfinished", "Finished", "Video", "Music",
           "Documents", "Archives", "Programs", "Other"]
COLUMNS = ["File name", "Size", "Progress", "Speed", "Time left", "Status", "Added"]
STATUS_TEXT = {"queued": "Queued", "downloading": "Downloading", "paused": "Paused",
               "done": "Completed", "error": "Error"}

QSS = """
QWidget{background:#14161c;color:#e6e8ee;font-size:13px}
QToolBar{background:#1b1e26;border:0;border-bottom:1px solid #2a2e3a;spacing:6px;padding:6px}
QToolButton{background:transparent;border:1px solid transparent;border-radius:8px;padding:7px 12px;color:#e6e8ee}
QToolButton:hover{background:#262a36;border-color:#343a4a}
QListWidget{background:#171a21;border:0;border-right:1px solid #2a2e3a;outline:0;padding:6px}
QListWidget::item{padding:9px 12px;border-radius:8px;margin:1px 0}
QListWidget::item:selected{background:#6c5ce7;color:white}
QListWidget::item:hover:!selected{background:#222633}
QTableWidget{background:#14161c;alternate-background:#171a21;border:0;gridline-color:transparent;
  selection-background-color:#2b2f4a;selection-color:#fff;outline:0}
QHeaderView::section{background:#1b1e26;color:#9aa3b5;border:0;border-bottom:1px solid #2a2e3a;padding:8px;font-weight:600}
QProgressBar{background:#232733;border:0;border-radius:7px;text-align:center;color:#fff;font-size:11px}
QProgressBar::chunk{background:#6c5ce7;border-radius:7px}
QLineEdit,QPlainTextEdit,QSpinBox{background:#1b1e26;border:1px solid #2f3445;border-radius:8px;padding:6px}
QPushButton{background:#6c5ce7;border:0;border-radius:8px;padding:8px 18px;color:#fff;font-weight:600}
QPushButton:hover{background:#7d6ff0}
QMenu{background:#1b1e26;border:1px solid #2f3445;padding:6px}
QMenu::item{padding:7px 24px;border-radius:6px}
QMenu::item:selected{background:#6c5ce7}
QStatusBar{background:#1b1e26;color:#9aa3b5}
QScrollBar:vertical{background:#14161c;width:10px}
QScrollBar::handle:vertical{background:#343a4a;border-radius:5px;min-height:30px}
QScrollBar::add-line,QScrollBar::sub-line{height:0}
"""


# ---------------------------------------------------------------- local API
class Bridge(QObject):
    incoming = Signal(str)


def start_api(mgr, bridge):
    """Local API used by the browser extension: POST /add with X-Subzeo-Token."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b"{}"):
            self.send_response(code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Subzeo-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            self._send(200, b'{"app":"subzeo"}')

        def do_POST(self):
            if self.path != "/add" or self.headers.get("X-Subzeo-Token") != mgr.s["token"]:
                return self._send(403, b'{"ok":false}')
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(min(n, 1_000_000)).decode("utf-8")
                json.loads(data)
            except (ValueError, OSError):
                return self._send(400, b'{"ok":false}')
            bridge.incoming.emit(data)
            self._send(200, b'{"ok":true}')

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler)
    except OSError:
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ----------------------------------------------------------------- dialogs
class AddDialog(QDialog):
    def __init__(self, parent, mgr, text=""):
        super().__init__(parent)
        self.setWindowTitle("Add downloads")
        self.resize(580, 340)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Paste one or more links (one per line):"))
        self.urls = QPlainTextEdit(text)
        lay.addWidget(self.urls, 1)

        row = QHBoxLayout()
        self.folder = QLineEdit(mgr.s["folder"])
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(QLabel("Save to"))
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.conn = QSpinBox()
        self.conn.setRange(1, 32)
        self.conn.setValue(int(mgr.s["connections"]))
        self.start_now = QCheckBox("Start immediately")
        self.start_now.setChecked(True)
        row2.addWidget(QLabel("Connections"))
        row2.addWidget(self.conn)
        row2.addStretch(1)
        row2.addWidget(self.start_now)
        lay.addLayout(row2)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Download")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def _browse(self):
        p = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder.text())
        if p:
            self.folder.setText(p)

    def links(self):
        return [l.strip() for l in self.urls.toPlainText().splitlines()
                if re.match(r"^https?://\S+$", l.strip(), re.I)]


class VideoDialog(QDialog):
    def __init__(self, parent, mgr, text=""):
        super().__init__(parent)
        self.setWindowTitle("Download video / audio")
        self.resize(560, 220)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Video page link (YouTube, Facebook, Vimeo, TikTok, X and 1000+ sites):"))
        self.url = QLineEdit(text)
        lay.addWidget(self.url)
        row = QHBoxLayout()
        row.addWidget(QLabel("Quality"))
        self.fmt = QComboBox()
        self.fmt.addItems(VIDEO_FORMATS)
        row.addWidget(self.fmt, 1)
        lay.addLayout(row)
        hint = QLabel("Needs yt-dlp (pip install yt-dlp). ffmpeg is needed for HD merging and MP3.")
        hint.setStyleSheet("color:#9aa3b5")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Download")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def link(self):
        u = self.url.text().strip()
        return u if re.match(r"^https?://\S+$", u, re.I) else ""


class SchedulerDialog(QDialog):
    WHEN = [("Do nothing", "none"), ("Close Subzeo", "close"),
            ("Put the PC to sleep", "sleep"), ("Shut down the PC", "shutdown")]

    def __init__(self, parent, mgr):
        super().__init__(parent)
        self.mgr = mgr
        self.setWindowTitle("Scheduler")
        self.resize(460, 280)
        sc = mgr.s["schedule"]
        form = QFormLayout(self)

        self.enabled = QCheckBox("Enable scheduler (starts paused / failed downloads at the time below)")
        self.enabled.setChecked(bool(sc.get("enabled")))
        form.addRow(self.enabled)

        self.start = QTimeEdit(QTime.fromString(sc.get("start") or "02:00", "HH:mm"))
        self.start.setDisplayFormat("HH:mm")
        form.addRow("Start at", self.start)

        self.use_stop = QCheckBox("Stop at")
        self.use_stop.setChecked(bool(sc.get("stop")))
        self.stop = QTimeEdit(QTime.fromString(sc.get("stop") or "07:00", "HH:mm"))
        self.stop.setDisplayFormat("HH:mm")
        form.addRow(self.use_stop, self.stop)

        self.when = QComboBox()
        for label, _ in self.WHEN:
            self.when.addItem(label)
        self.when.setCurrentIndex([v for _, v in self.WHEN].index(mgr.when_done))
        form.addRow("When all downloads finish\n(this session only)", self.when)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def _save(self):
        self.mgr.s["schedule"] = {
            "enabled": self.enabled.isChecked(),
            "start": self.start.time().toString("HH:mm"),
            "stop": self.stop.time().toString("HH:mm") if self.use_stop.isChecked() else "",
            "when_done": "none",
        }
        self.mgr.when_done = self.WHEN[self.when.currentIndex()][1]
        self.mgr.save_settings()
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent, mgr):
        super().__init__(parent)
        self.mgr = mgr
        self.setWindowTitle("Settings")
        self.resize(520, 360)
        form = QFormLayout(self)

        self.folder = QLineEdit(mgr.s["folder"])
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        h = QHBoxLayout()
        h.addWidget(self.folder, 1)
        h.addWidget(b)
        form.addRow("Default folder", h)

        self.conn = QSpinBox()
        self.conn.setRange(1, 32)
        self.conn.setValue(int(mgr.s["connections"]))
        form.addRow("Connections per file", self.conn)

        self.active = QSpinBox()
        self.active.setRange(1, 10)
        self.active.setValue(int(mgr.s["max_active"]))
        form.addRow("Simultaneous downloads", self.active)

        self.limit = QSpinBox()
        self.limit.setRange(0, 10_000_000)
        self.limit.setSuffix(" KB/s  (0 = unlimited)")
        self.limit.setValue(int(mgr.s["speed_limit_kb"]))
        form.addRow("Speed limit", self.limit)

        self.cat = QCheckBox("Save into category folders (Video, Music, …)")
        self.cat.setChecked(bool(mgr.s["by_category"]))
        form.addRow("", self.cat)

        self.clip = QCheckBox("Offer to download links copied to clipboard")
        self.clip.setChecked(bool(mgr.s["clipboard"]))
        form.addRow("", self.clip)

        self.tray = QCheckBox("Keep running in the system tray when the window is closed")
        self.tray.setChecked(bool(mgr.s.get("tray", True)))
        form.addRow("", self.tray)

        tok = QLineEdit(mgr.s["token"])
        tok.setReadOnly(True)
        form.addRow("Browser extension token", tok)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def _browse(self):
        p = QFileDialog.getExistingDirectory(self, "Choose folder", self.folder.text())
        if p:
            self.folder.setText(p)

    def _save(self):
        s = self.mgr.s
        s.update(folder=self.folder.text().strip() or s["folder"],
                 connections=self.conn.value(), max_active=self.active.value(),
                 speed_limit_kb=self.limit.value(), by_category=self.cat.isChecked(),
                 clipboard=self.clip.isChecked(), tray=self.tray.isChecked())
        self.mgr.apply_settings()
        self.accept()


# ------------------------------------------------------------- main window
class Main(QMainWindow):
    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr
        self.filter = FILTERS[0]
        self.row_ids = []
        self.bars = {}
        self._dlg_open = False
        self._last_clip = ""
        self._ticks = 0
        self._quitting = False
        self._pending = None
        self.sched = Scheduler(mgr)
        self.setWindowIcon(QIcon(resource("assets/icon.png")))
        self.setWindowTitle("Subzeo Download Manager")
        self.resize(1180, 700)
        self.setAcceptDrops(True)

        self._build_toolbar()

        self.side = QListWidget()
        self.side.setFixedWidth(190)
        self.side.addItems(FILTERS)
        self.side.setCurrentRow(0)
        self.side.currentTextChanged.connect(self._set_filter)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(46)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c, w in ((1, 100), (2, 190), (3, 100), (4, 90), (5, 110), (6, 140)):
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
            self.table.setColumnWidth(c, w)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(self._open_file)

        split = QSplitter()
        split.addWidget(self.side)
        split.addWidget(self.table)
        split.setStretchFactor(1, 1)
        split.setHandleWidth(1)
        self.setCentralWidget(split)

        self.speed_lbl = QLabel("")
        self.statusBar().addPermanentWidget(self.speed_lbl)
        self.statusBar().showMessage("Ready")

        self._setup_tray()
        QGuiApplication.clipboard().dataChanged.connect(self._on_clip)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    # ----- toolbar
    def _build_toolbar(self):
        tb = QToolBar()
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)

        def act(text, fn, key=None):
            a = QAction(text, self)
            a.triggered.connect(lambda *_: fn())
            if key:
                a.setShortcut(QKeySequence(key))
            tb.addAction(a)
            return a

        act("＋  Add URL", self.add_dialog, "Ctrl+N")
        act("🎬  Video", self.video_dialog, "Ctrl+Shift+V")
        tb.addSeparator()
        act("▶  Resume", self.resume_selected, "Ctrl+R")
        act("❚❚  Pause", self.pause_selected, "Ctrl+P")
        act("▶▶  Start All", self.start_all)
        act("■  Pause All", self.pause_all)
        tb.addSeparator()
        act("✕  Delete", self.delete_selected, "Delete")
        act("📂  Open Folder", self.open_folder)
        tb.addSeparator()
        act("⏰  Scheduler", self.scheduler)
        act("⚙  Settings", self.settings)

    # ----- filtering / refresh
    def _set_filter(self, text):
        if text:
            self.filter = text
            self.refresh()

    def _match(self, d):
        f = self.filter
        if f == "All Downloads":
            return True
        if f == "Unfinished":
            return d.status != "done"
        if f == "Finished":
            return d.status == "done"
        return d.category == f

    def selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        by_id = {d.id: d for d in self.mgr.items}
        return [by_id[self.row_ids[r]] for r in sorted(rows)
                if r < len(self.row_ids) and self.row_ids[r] in by_id]

    def _rebuild(self, vis):
        keep = {d.id for d in self.selected()}
        self.table.setUpdatesEnabled(False)
        self.table.clearSelection()
        self.table.setRowCount(0)
        self.table.setRowCount(len(vis))
        self.bars = {}
        for r, d in enumerate(vis):
            for c in range(len(COLUMNS)):
                it = QTableWidgetItem("")
                if c in (1, 3, 4):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, it)
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(8, 14, 8, 14)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            lay.addWidget(bar)
            self.table.setCellWidget(r, 2, box)
            self.bars[d.id] = bar
            if d.id in keep:
                self.table.setRangeSelected(
                    QTableWidgetSelectionRange(r, 0, r, len(COLUMNS) - 1), True)
        self.row_ids = [d.id for d in vis]
        self.table.setUpdatesEnabled(True)

    def refresh(self):
        for ev in self.sched.tick():
            if ev == "start":
                self.statusBar().showMessage("Scheduler: queue started", 5000)
            elif ev == "stop":
                self.statusBar().showMessage("Scheduler: queue paused", 5000)
            elif ev == "finished":
                self._when_done()
        self.mgr.pump()
        for d in self.mgr.items:
            d.tick()
        vis = [d for d in reversed(self.mgr.items) if self._match(d)]
        if [d.id for d in vis] != self.row_ids:
            self._rebuild(vis)

        for r, d in enumerate(vis):
            size = fmt_size(d.size) if d.size else "—"
            texts = [d.name or d.url, size, None,
                     fmt_speed(d.speed) if d.status == "downloading" else "",
                     fmt_eta(d.eta),
                     STATUS_TEXT[d.status] + (f": {d.error[:40]}" if d.status == "error" else ""),
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(d.added))]
            for c, t in enumerate(texts):
                if t is None:
                    continue
                it = self.table.item(r, c)
                if it is not None and it.text() != t:
                    it.setText(t)
            st = self.table.item(r, 5)
            if st is not None:
                st.setForeground(QColor({"done": "#4ade80", "error": "#f87171",
                                         "downloading": "#a5b4fc"}.get(d.status, "#9aa3b5")))
            bar = self.bars.get(d.id)
            if bar is not None:
                if d.status == "downloading" and not d.size:
                    bar.setRange(0, 0)
                else:
                    bar.setRange(0, 1000)
                    pct = 100.0 if d.status == "done" else d.percent
                    bar.setValue(int(pct * 10))
                    bar.setFormat(f"{pct:.1f}%")

        total = sum(d.speed for d in self.mgr.items if d.status == "downloading")
        active = sum(1 for d in self.mgr.items if d.status == "downloading")
        self.speed_lbl.setText(f"{active} active   ⬇ {fmt_speed(total) or '0 B/s'}   ")
        self._ticks += 1
        if self._ticks % 6 == 0:
            self.mgr.save()

    # ----- actions
    def add_dialog(self, text=""):
        if self._dlg_open:
            return
        self._dlg_open = True
        try:
            if not text:
                clip = QGuiApplication.clipboard().text().strip()
                text = clip if re.match(r"^https?://\S+$", clip) else ""
            dlg = AddDialog(self, self.mgr, text)
            if dlg.exec():
                for url in dlg.links():
                    self.mgr.add(url, dlg.folder.text().strip(), dlg.conn.value(),
                                 start=dlg.start_now.isChecked())
                self.refresh()
        finally:
            self._dlg_open = False

    def resume_selected(self):
        for d in self.selected():
            if d.status in ("paused", "error"):
                d.status, d.error = "queued", ""

    def pause_selected(self):
        for d in self.selected():
            if d.status in ("downloading", "queued"):
                d.pause()

    def start_all(self):
        self.mgr.start_all()

    def pause_all(self):
        self.mgr.pause_all()

    def video_dialog(self, text=""):
        if self._dlg_open:
            return
        self._dlg_open = True
        try:
            dlg = VideoDialog(self, self.mgr, text if isinstance(text, str) else "")
            if dlg.exec() and dlg.link():
                self.mgr.add_video(dlg.link(), dlg.fmt.currentText())
                self.refresh()
        finally:
            self._dlg_open = False

    def scheduler(self):
        if SchedulerDialog(self, self.mgr).exec():
            self.statusBar().showMessage("Scheduler saved", 3000)

    # ----- tray / quit / power
    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Subzeo Download Manager")
        self._tray_menu = QMenu()
        self._tray_menu.addAction("Open Subzeo", self._show_main)
        self._tray_menu.addAction("Add URL", lambda: (self._show_main(), self.add_dialog()))
        self._tray_menu.addSeparator()
        self._tray_menu.addAction("Quit", self.quit_app)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(
            lambda r: self._show_main()
            if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def _show_main(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._quitting = True
        self.close()
        QApplication.quit()

    def _when_done(self):
        kind, self.mgr.when_done = self.mgr.when_done, "none"
        if kind == "none":
            return
        if kind == "close":
            self.quit_app()
            return
        what = "shut down" if kind == "shutdown" else "go to sleep"
        box = QMessageBox(self)
        box.setWindowTitle("Subzeo")
        box.setText(f"All downloads are finished.\nThe PC will {what} in 60 seconds.")
        box.setStandardButtons(QMessageBox.Cancel)
        timer = QTimer(self)
        timer.setSingleShot(True)

        def go():
            box.finished.disconnect()
            box.close()
            self.mgr.shutdown()
            power_action(kind)

        timer.timeout.connect(go)
        box.finished.connect(lambda *_: timer.stop())
        self._pending = (box, timer)
        box.open()
        timer.start(60_000)

    def delete_selected(self):
        sel = self.selected()
        if not sel:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Delete")
        box.setText(f"Remove {len(sel)} download(s)?")
        a = box.addButton("Remove from list", QMessageBox.AcceptRole)
        b = box.addButton("Remove and delete file", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() not in (a, b):
            return
        for d in sel:
            self.mgr.remove(d, delete_file=box.clickedButton() is b)
        self.refresh()

    def _open_file(self, *_):
        for d in self.selected()[:1]:
            if d.status == "done" and os.path.exists(d.path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(d.path))

    def open_folder(self):
        sel = self.selected()
        folder = str(Path(sel[0].path).parent) if sel and sel[0].path else self.mgr.s["folder"]
        Path(folder).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def settings(self):
        if SettingsDialog(self, self.mgr).exec():
            self.statusBar().showMessage("Settings saved", 3000)

    def _menu(self, pos):
        sel = self.selected()
        if not sel:
            return
        m = QMenu(self)
        m.addAction("Open", self._open_file)
        m.addAction("Open folder", self.open_folder)
        m.addSeparator()
        m.addAction("Resume", self.resume_selected)
        m.addAction("Pause", self.pause_selected)
        m.addAction("Copy link", lambda: QGuiApplication.clipboard().setText(
            "\n".join(d.url for d in sel)))
        m.addSeparator()
        m.addAction("Delete…", self.delete_selected)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ----- clipboard / drag-drop / browser extension
    def _on_clip(self):
        if not self.mgr.s["clipboard"]:
            return
        t = QGuiApplication.clipboard().text().strip()
        if t == self._last_clip:
            return
        self._last_clip = t
        if re.match(r"^https?://\S+$", t) and ext_of(t) in ALL_EXT:
            self.show()
            self.raise_()
            self.activateWindow()
            self.add_dialog(t)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        links = [u.toString() for u in md.urls() if u.scheme() in ("http", "https")]
        if not links and md.hasText():
            links = md.text().split()
        self.add_dialog("\n".join(links))

    def on_extension(self, payload):
        try:
            data = json.loads(payload)
        except ValueError:
            return
        url = str(data.get("url", ""))
        if re.match(r"^https?://\S+$", url, re.I) and data.get("kind") == "video":
            self.mgr.add_video(url)
            self.statusBar().showMessage("Video added from browser", 4000)
            self.refresh()
        elif re.match(r"^https?://\S+$", url, re.I):
            self.mgr.add(url, referer=str(data.get("referer", "")),
                         cookie=str(data.get("cookie", "")), name=str(data.get("filename", "")))
            self.statusBar().showMessage("Added from browser", 4000)
            self.refresh()

    def closeEvent(self, e):
        if self.tray and self.mgr.s.get("tray", True) and not self._quitting:
            e.ignore()
            self.hide()
            self.tray.showMessage("Subzeo", "Still running in the tray - downloads continue.",
                                  QSystemTrayIcon.Information, 2500)
            return
        self.mgr.shutdown()
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Subzeo")
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    app.setWindowIcon(QIcon(resource("assets/icon.png")))
    mgr = Manager()
    win = Main(mgr)
    app.setQuitOnLastWindowClosed(win.tray is None)
    bridge = Bridge()
    bridge.incoming.connect(win.on_extension)
    start_api(mgr, bridge)
    args = sys.argv[1:]
    if not ("--minimized" in args and win.tray):
        win.show()
    for a in args:
        if a.startswith("http"):
            QTimer.singleShot(200, lambda u=a: win.add_dialog(u))
            break
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
