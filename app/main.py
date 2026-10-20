"""Game Translator: select English text on screen -> OCR -> Thai translation + word lookup.

Everything runs offline and lives in one window (meant for a second monitor).
Run with:  python -m app.main
"""
import re
import sys
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PySide6.QtCore import QLocale, QObject, QPoint, QRect, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QImage, QScreen
from PySide6.QtWidgets import (QApplication, QComboBox, QSpinBox, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QMainWindow, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from . import config, gamelang, winapi
from .dictionary import Dictionary, build as build_dictionary, contraction_note
from .glossary import Glossary
from .hotkeys import HotkeyListener
from .ocr import WindowsOcr, fix_ocr_words
from .translator import Translator, needs_translation, split_sentences
from .typhoon import TyphoonServer
from .ui import (APP_STYLE, WORD_RE, GlossaryView, ReviewView, SelectionOverlay, TermDialog,
                 TranslationView, VocabView)
from .vocab import Vocab


class Bridge(QObject):
    """Signals used to hand results from the worker thread back to the UI thread."""
    status = Signal(str)
    result = Signal(list)
    alternatives = Signal(str, list)
    error = Signal(str)
    import_progress = Signal(str)
    import_done = Signal(dict, str, dict)     # terms, source description, stats
    import_failed = Signal(str)
    refined = Signal(int, int, str, bool)     # scan id, sentence index, Typhoon text so far, done
    typhoon_status = Signal(str)


@dataclass
class Area:
    """The last selected text area."""
    screen: QScreen
    px_rect: QRect                   # inside that screen's screenshot (physical pixels)
    hwnd: int | None = None          # target window, if one was chosen
    rel_rect: QRect | None = None    # area relative to the window's top-left, so it follows the window


def join_ocr_lines(lines: list[str]) -> list[str]:
    """Merge wrapped dialogue lines into blocks; keep separate items (menus) apart."""
    blocks: list[str] = []
    for line in (l.strip() for l in lines):
        if not line:
            continue
        continues = blocks and not re.search(r"[.!?:\"”]$", blocks[-1]) and line[0].islower()
        if continues:
            # A word split at the line end ("#ars-" + "guide") is joined without a space
            sep = "" if re.search(r"[A-Za-z]-$", blocks[-1]) else " "
            blocks[-1] += sep + line
        else:
            blocks.append(line)
    return blocks


class App(QMainWindow):
    TAB_TRANSLATE, TAB_REVIEW, TAB_VOCAB = 0, 1, 2

    def __init__(self):
        super().__init__()
        self.bridge = Bridge()
        # One worker thread: OCR/translation jobs run in order and never block the UI
        self.worker = ThreadPoolExecutor(max_workers=1)
        # Imports read big jars / download ~25 MB; keep them off the translation queue
        self.importer = ThreadPoolExecutor(max_workers=1)
        # Typhoon refinement runs on its own thread so new scans are never stuck behind it
        self.typhoon = TyphoonServer(config.LLAMA_SERVERS, config.TYPHOON_MODEL)
        self.typhoon_worker = ThreadPoolExecutor(max_workers=1)
        self.scan_id = 0                 # bumped per scan; stale Typhoon results are dropped
        self.translator: Translator | None = None
        self.ocr = WindowsOcr()
        self.dictionary = Dictionary(config.DICT_DB)
        self.vocab = Vocab(config.VOCAB_DB)
        self.settings = QSettings("game-translator", "app")
        self.glossary = Glossary(config.GLOSSARY_DIR, config.GLOSSARY_IMPORTED_DIR)
        default_profile = "Minecraft" if "Minecraft" in self.glossary.profiles() else ""
        self.glossary.load(self.settings.value("glossary_profile", default_profile) or None)
        self.last_lines: list[str] | None = None   # last OCR result, re-translated when the glossary changes
        self.last_area: Area | None = None
        self.target: int | None = None   # hwnd of the chosen game window; None = whole screen
        self.overlay = None

        self._build_window()
        self.refresh_windows()
        self.bridge.status.connect(self.status.setText)
        self.bridge.result.connect(self.view.show_result)
        self.bridge.refined.connect(self.on_refined)
        self.bridge.typhoon_status.connect(self.status.setText)
        self.bridge.alternatives.connect(self.view.show_alternatives)
        self.bridge.error.connect(lambda m: self.view.show_message("เกิดข้อผิดพลาด: " + m))
        self.view.word_clicked.connect(self.on_word_clicked)
        self.view.alt_requested.connect(self.on_alt_requested)
        self.view.add_term_requested.connect(self.on_add_term)
        self.glossary_view.profile_changed.connect(self.on_glossary_profile)
        self.glossary_view.saved.connect(self.on_glossary_saved)
        self.glossary_view.import_requested.connect(self.on_import_requested)
        self.glossary_view.clear_import_requested.connect(self.on_clear_import)
        self.bridge.import_progress.connect(self.status.setText)
        self.bridge.import_done.connect(self.on_import_done)
        self.bridge.import_failed.connect(lambda m: self.status.setText(f"✖ นำเข้าไม่สำเร็จ: {m}"))
        # Plain bool copy: read from the worker thread, so do not touch the widget there
        self.auto_names_on = self.settings.value("auto_names", True, type=bool)
        self.glossary_view.auto_names.setChecked(self.auto_names_on)
        self.glossary_view.auto_names.toggled.connect(self.on_auto_names)
        self._refresh_glossary_view()

        self.hotkeys = HotkeyListener(config.HOTKEYS)
        self.hotkeys.triggered.connect(self.on_hotkey)
        self.hotkeys.failed.connect(
            lambda n: self.status.setText(f"⚠ ปุ่มลัด '{n}' ถูกโปรแกรมอื่นใช้อยู่ ใช้ปุ่มด้านบนแทน"))
        self.hotkeys.start()

        self._activate_engine(self.engine)

    def _build_window(self):
        self.setWindowTitle("Game Translator (EN → TH)")

        # Row 1: which game window to read from
        self.window_combo = QComboBox()
        self.window_combo.activated.connect(self.on_target_chosen)
        refresh_btn = QPushButton("⟳")
        refresh_btn.setToolTip("รีเฟรชรายชื่อหน้าต่าง")
        refresh_btn.clicked.connect(self.refresh_windows)
        self.pin_btn = QPushButton("📌 อยู่บนสุด")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setToolTip("ให้หน้าต่างนี้อยู่บนสุดเสมอ (ใช้ตอนมีจอเดียว)")
        self.pin_btn.toggled.connect(self.set_pinned)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("เกม:"))
        row1.addWidget(self.window_combo, 1)
        row1.addWidget(refresh_btn)
        row1.addWidget(self.pin_btn)

        # Row 2: actions + status
        select = QPushButton("✂ เลือกพื้นที่  (Ctrl+Shift+T)")
        select.clicked.connect(lambda: self.start_selection(from_button=True))
        rescan = QPushButton("⟲ สแกนซ้ำ  (Ctrl+Shift+R)")
        rescan.clicked.connect(self.rescan)
        self.model_combo = QComboBox()
        labels = ([config.ENGINE_TYPHOON] if self.typhoon.available else []) + list(config.MODELS)
        self.model_combo.addItems(labels)
        self.model_combo.setItemData(0, "Typhoon: แปลตามบริบท ทยอยแสดงทีละคำ (2-12 วินาทีต่อประโยค)"
                                     if self.typhoon.available else "", Qt.ToolTipRole)
        saved = self.settings.value("engine", labels[0])   # new key: Typhoon becomes the default once
        self.engine = saved if saved in labels and self._engine_ready(saved) else labels[0]
        self.model_combo.setCurrentText(self.engine)
        self.model_combo.activated.connect(lambda _: self.switch_model(self.model_combo.currentText()))
        row2 = QHBoxLayout()
        row2.addWidget(select)
        row2.addWidget(rescan)
        row2.addStretch(1)
        row2.addWidget(QLabel("โมเดล:"))
        row2.addWidget(self.model_combo)

        # Where Typhoon runs: graphics card (fast, needs ~3 GB free VRAM) or CPU threads
        self.typhoon.device = self.settings.value("typhoon_device", "cpu")
        if not self.typhoon.device_available(self.typhoon.device):
            self.typhoon.device = "cpu"
        self.typhoon.threads = self.settings.value("typhoon_threads", config.TYPHOON_THREADS, type=int)
        self.device_combo = QComboBox()
        self.device_combo.addItem("CPU", "cpu")
        self.device_combo.addItem("การ์ดจอ", "gpu")
        self.device_combo.setItemData(1, "รัน Typhoon บนการ์ดจอ (Vulkan) เร็วกว่ามาก ต้องมี VRAM ว่างประมาณ 3 GB",
                                      Qt.ToolTipRole)
        if not self.typhoon.device_available("gpu"):
            self.device_combo.model().item(1).setEnabled(False)
        self.device_combo.setCurrentIndex(self.device_combo.findData(self.typhoon.device))
        self.device_combo.activated.connect(lambda _: self.on_typhoon_device(self.device_combo.currentData()))
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, os.cpu_count() or 4)
        self.threads_spin.setValue(self.typhoon.threads)
        self.threads_spin.setToolTip("จำนวนเธรด CPU ที่ Typhoon ใช้ — น้อย = เกมลื่นกว่า, มาก = แปลเร็วกว่า")
        self.threads_spin.editingFinished.connect(lambda: self.on_typhoon_threads(self.threads_spin.value()))
        row2.addWidget(QLabel("รันบน:"))
        row2.addWidget(self.device_combo)
        row2.addWidget(QLabel("เธรด:"))
        row2.addWidget(self.threads_spin)

        self.status = QLabel("กำลังโหลดโมเดลแปลภาษา...")
        self.status.setObjectName("status")

        self.view = TranslationView()
        self.review_view = ReviewView(self.vocab)
        self.vocab_view = VocabView(self.vocab)
        self.glossary_view = GlossaryView()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.view, "แปล")
        self.tabs.addTab(self.review_view, "ทบทวน")
        self.tabs.addTab(self.vocab_view, "คลังคำศัพท์")
        self.tabs.addTab(self.glossary_view, "ศัพท์เกม")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        hint = QLabel("Ctrl+Shift+H = ย่อ/แสดงหน้าต่างนี้  •  เกมควรเปิดแบบ Borderless/Windowed "
                      "ถ้าใช้ 📌 บนจอเดียวกับเกม")
        hint.setObjectName("hint")

        lay = QVBoxLayout()
        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addWidget(self.status)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(hint)
        w = QWidget()
        w.setLayout(lay)
        self.setCentralWidget(w)
        self.resize(640, 780)
        if geometry := self.settings.value("geometry"):
            self.restoreGeometry(geometry)    # reopen on the same monitor/position as last time

    def showEvent(self, e):
        super().showEvent(e)
        # Never let this window end up inside a screenshot that gets OCR'd
        winapi.exclude_from_capture(int(self.winId()))

    def set_pinned(self, on: bool):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()                    # changing flags re-creates the native window

    # ---- target window -----------------------------------------------------------
    def refresh_windows(self):
        """Reload the window list, keeping the current choice (or the one used last time)."""
        saved_exe = self.settings.value("target_exe", "")
        self.window_combo.clear()
        self.window_combo.addItem("ทั้งหน้าจอ (ไม่ล็อกหน้าต่าง)", None)
        for w in winapi.list_windows():
            self.window_combo.addItem(w.label(), w.hwnd)
            self.window_combo.setItemData(self.window_combo.count() - 1, w.exe, Qt.UserRole + 1)
        index = 0
        for i in range(1, self.window_combo.count()):
            if self.window_combo.itemData(i) == self.target or (
                    self.target is None and saved_exe and self.window_combo.itemData(i, Qt.UserRole + 1) == saved_exe):
                index = i
                break
        self.window_combo.setCurrentIndex(index)
        self._set_target(self.window_combo.itemData(index))

    def on_target_chosen(self, index: int):
        self._set_target(self.window_combo.itemData(index))
        self.settings.setValue("target_exe", self.window_combo.itemData(index, Qt.UserRole + 1) or "")

    def _set_target(self, hwnd: int | None):
        if hwnd != self.target:
            self.last_area = None        # old area belongs to another window
        self.target = hwnd

    def _target_alive(self, hwnd: int | None) -> bool:
        if hwnd is None or winapi.is_valid(hwnd):
            return True
        self.status.setText("⚠ หน้าต่างเกมที่เลือกถูกปิดไปแล้ว กรุณาเลือกใหม่")
        self.target = None
        self.last_area = None
        self.refresh_windows()
        return False

    def _screen_for(self, hwnd: int | None) -> QScreen:
        if hwnd is not None:
            monitor = winapi.monitor_of(hwnd)
            for s in QGuiApplication.screens():
                if self._screen_origin(s) == monitor.topLeft():
                    return s
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    @staticmethod
    def _screen_origin(screen: QScreen) -> QPoint:
        """Top-left of the monitor in physical pixels.

        Qt on Windows scales each screen around its own top-left corner, so the
        logical origin equals the physical one even with mixed DPI monitors.
        """
        return screen.geometry().topLeft()

    # ---- worker-thread jobs --------------------------------------------------
    def _load_model(self, label: str | None = None):
        label = label or self.model_combo.currentText()
        self.translator = None          # free the old model before loading the new one (RAM)
        self.bridge.status.emit(f"กำลังโหลด {label} ...")
        try:
            self.translator = Translator(config.MODELS[label], config.SRC_LANG, config.TGT_LANG,
                                         config.TRANSLATE_THREADS)
            self.bridge.status.emit(f"✔ พร้อมใช้งาน — {label}")
        except Exception as e:
            self.bridge.status.emit(f"✖ โหลดโมเดลไม่สำเร็จ: {e}")

    def _process(self, image: QImage):
        try:
            # Grayscale removes ClearType's colored letter edges, which make OCR read "rn" as "m"
            image = image.convertToFormat(QImage.Format_Grayscale8)
            # Upscale: small UI/web text is read far more reliably at 2-3x
            scale = 3 if image.width() < config.OCR_UPSCALE_BELOW else 2
            scale = min(scale, self.ocr.max_dimension // max(1, image.width(), image.height()))
            if scale > 1:
                image = image.scaled(image.width() * scale, image.height() * scale,
                                     Qt.KeepAspectRatio, Qt.SmoothTransformation)
            image = image.convertToFormat(QImage.Format_ARGB32)       # BGRA byte order in memory
            raw = bytes(image.constBits())[: image.sizeInBytes()]
            lines = self.ocr.recognize(raw, image.width(), image.height())
            # OCR splits hyphenated words at line ends: "spaced- repetition" -> "spaced-repetition"
            lines = [re.sub(r"(?<=[a-z])- (?=[a-z])", "-", line) for line in lines]
            self.last_lines = [fix_ocr_words(line, self._is_known) for line in lines]
            self._translate_lines(self.last_lines)
        except Exception as e:
            traceback.print_exc()
            self.bridge.error.emit(str(e))

    def _translate_lines(self, lines: list[str]):
        """Translate OCR lines. Order of preference per sentence:
        no English word -> shown as-is; whole text is a game term -> glossary;
        otherwise the chosen engine: Typhoon (streamed in sentence by sentence) or NLLB
        (game terms and proper names protected with placeholders)."""
        if self.engine == config.ENGINE_TYPHOON:
            self._translate_lines_typhoon(lines)
            return
        if self.translator is None:
            self.bridge.error.emit("โมเดลแปลยังโหลดไม่เสร็จ ลองใหม่อีกครั้ง")
            return
        results: list[tuple[str, str, str] | None] = []
        pending: list[tuple[int, str]] = []
        for block in join_ocr_lines(lines):
            for s in split_sentences(block):
                if not needs_translation(s):
                    results.append((s, "", "none"))
                elif fixed := self.glossary.exact(s):
                    results.append((s, fixed, "glossary"))
                else:
                    pending.append((len(results), s))
                    results.append(None)
        if pending:
            protected = [self.glossary.protect(s, self._name_checker()) for _, s in pending]
            outs = self.translator.translate_sentences([text for text, _ in protected])
            for (i, s), (_, mapping), versions in zip(pending, protected, outs):
                results[i] = (s, self.glossary.restore(versions[0], mapping), "nllb")
        # The model translates idioms word by word; attach any idiom found in the sentence
        self.scan_id += 1
        self.bridge.result.emit([(s, th, src, self.dictionary.idioms.find(s) if src == "nllb" else [], False)
                                 for s, th, src in results])

    def _translate_lines_typhoon(self, lines: list[str]):
        rows = []
        for block in join_ocr_lines(lines):
            for s in split_sentences(block):
                if not needs_translation(s):
                    rows.append((s, "", "none", [], False))
                elif fixed := self.glossary.exact(s):
                    rows.append((s, fixed, "glossary", [], False))
                else:
                    rows.append((s, "", "typhoon", self.dictionary.idioms.find(s), True))
        self.scan_id += 1
        self.bridge.result.emit(rows)
        todo = [(i, row[0]) for i, row in enumerate(rows) if row[4]]
        if todo:
            self.typhoon_worker.submit(self._stream_job, self.scan_id, todo)

    # ---- Typhoon (second, better translation pass) ------------------------------------
    def _start_typhoon(self):
        try:
            self.bridge.typhoon_status.emit("กำลังโหลด Typhoon (ใช้ RAM ประมาณ 3 GB)...")
            self.typhoon.start()
            where = "การ์ดจอ" if self.typhoon.device == "gpu" else f"CPU {self.typhoon.threads} เธรด"
            self.bridge.typhoon_status.emit(f"✔ พร้อมใช้งาน — {config.ENGINE_TYPHOON} บน {where}")
        except Exception as e:
            self.bridge.typhoon_status.emit(f"✖ เปิด Typhoon ไม่สำเร็จ: {e}")

    def _wait_for_typhoon(self, scan_id: int, timeout: float = 120) -> bool:
        """A scan right after start-up may arrive while the model is still loading."""
        deadline = time.time() + timeout
        while not self.typhoon.running:
            if scan_id != self.scan_id or time.time() > deadline or self.engine != config.ENGINE_TYPHOON:
                return False
            time.sleep(0.3)
        return True

    def _stream_job(self, scan_id: int, todo: list[tuple[int, str]]):
        if not self._wait_for_typhoon(scan_id):
            return
        game = self.glossary.name
        for i, sentence in todo:
            text, last_emit = "", 0.0
            try:
                for text in self.typhoon.translate_stream(sentence, game, self.glossary.terms_in(sentence)):
                    if scan_id != self.scan_id:
                        return           # a newer scan replaced this one; closing the stream stops the model
                    if time.time() - last_emit > 0.12:    # redraw at most ~8 times a second
                        self.bridge.refined.emit(scan_id, i, text, False)
                        last_emit = time.time()
            except Exception as e:
                self.bridge.typhoon_status.emit(f"✖ Typhoon แปลไม่สำเร็จ: {e}")
                return
            self.bridge.refined.emit(scan_id, i, text, True)

    def on_refined(self, scan_id: int, index: int, thai: str, done: bool):
        if scan_id == self.scan_id:
            self.view.update_sentence(index, thai, "typhoon", pending=not done)

    def _is_known(self, word: str) -> bool:
        return bool(self.dictionary.lookup(word)[1])

    def _name_checker(self):
        """Word test used to spot proper names, or None when auto-keeping names is switched off."""
        if not self.auto_names_on:
            return None
        return self._is_known

    def _retranslate_job(self):
        try:
            if self.last_lines:
                self._translate_lines(self.last_lines)
        except Exception as e:
            self.bridge.error.emit(str(e))

    def _alternatives_typhoon(self, en: str):
        try:
            terms = self.glossary.terms_in(en)
            # A higher temperature gives a different wording each time
            versions = list(dict.fromkeys(
                self.typhoon.translate(en, self.glossary.name, terms, temperature=t) for t in (0.2, 0.6, 0.8)))
            self.bridge.alternatives.emit(en, versions)
        except Exception as e:
            self.bridge.error.emit(str(e))

    def _alternatives(self, en: str):
        try:
            text, mapping = self.glossary.protect(en, self._name_checker())
            versions = [self.glossary.restore(v, mapping)
                        for v in self.translator.translate_sentences([text], alternatives=4)[0]]
            self.bridge.alternatives.emit(en, versions)
        except Exception as e:
            self.bridge.error.emit(str(e))

    # ---- UI actions ------------------------------------------------------------
    def on_hotkey(self, name: str):
        if name == "select":
            self.start_selection()
        elif name == "rescan":
            self.rescan()
        elif name == "toggle":
            if self.isMinimized():
                self.showNormal()
            else:
                self.showMinimized()

    def start_selection(self, from_button: bool = False):
        if self.overlay is not None:     # a selection is already open
            return
        if not self._target_alive(self.target):
            return
        delay = 0
        if from_button and self.target is not None:
            winapi.bring_to_front(self.target)
            delay = 300                # give the game time to come to the front
        QTimer.singleShot(delay, self._open_overlay)

    def _open_overlay(self):
        screen = self._screen_for(self.target)
        pixmap = screen.grabWindow(0)
        highlight = None
        if self.target is not None:
            highlight = winapi.window_rect(self.target).translated(-self._screen_origin(screen))
        self.overlay = SelectionOverlay(screen, pixmap, highlight)
        self.overlay.selected.connect(lambda px, _geo: self._on_selected(screen, pixmap, px))
        # The overlay deletes itself on close; touching it afterwards raises
        # "Internal C++ object already deleted", so forget it as soon as it is gone
        self.overlay.destroyed.connect(self._overlay_gone)
        target = self.target
        if target is not None:
            # The overlay takes keyboard focus; hand it back to the game when it closes
            self.overlay.destroyed.connect(lambda: winapi.is_valid(target) and winapi.bring_to_front(target))
        self.overlay.show()
        self.overlay.activateWindow()
        self.overlay.raise_()

    def _overlay_gone(self):
        self.overlay = None

    def _on_selected(self, screen: QScreen, pixmap, px_rect: QRect):
        area = Area(screen, px_rect)
        if self.target is not None:
            physical = px_rect.translated(self._screen_origin(screen))
            area.hwnd = self.target
            area.rel_rect = physical.translated(-winapi.window_rect(self.target).topLeft())
        self.last_area = area
        self._run_area(pixmap.copy(px_rect).toImage())

    def rescan(self):
        if not self.last_area:
            self.status.setText("ยังไม่ได้เลือกพื้นที่ กด Ctrl+Shift+T ก่อน")
            return
        if not self._target_alive(self.last_area.hwnd):
            return
        area = self.last_area
        if area.hwnd is not None:
            # Follow the window if it was moved since the area was selected
            area.screen = self._screen_for(area.hwnd)
            physical = area.rel_rect.translated(winapi.window_rect(area.hwnd).topLeft())
            area.px_rect = physical.translated(-self._screen_origin(area.screen))
        self._run_area(area.screen.grabWindow(0).copy(area.px_rect).toImage())

    def _run_area(self, image: QImage):
        self.tabs.setCurrentIndex(self.TAB_TRANSLATE)
        self.view.show_message("กำลังอ่านและแปล...")
        self.worker.submit(self._process, image)

    def on_word_clicked(self, si: int, wi: int):
        en, th, *_ = self.view.sentences[si]
        words = self.view.words[si]
        word = words[wi]
        base, entries = self.dictionary.lookup(word)
        phrases = self.dictionary.phrases(words, wi)
        self.view.show_word(word, base, entries, phrases, contraction_note(word))
        if entries:
            meaning = ", ".join(dict.fromkeys(e["thai"] for e in entries[:4]))
            self.vocab.record(base, meaning, entries[0]["cat"], en, th)
        for phrase, p_entries in phrases:
            meaning = ", ".join(dict.fromkeys(e["thai"] for e in p_entries[:4]))
            self.vocab.record(phrase, meaning, p_entries[0]["cat"], en, th)

    def on_alt_requested(self, si: int):
        en = self.view.sentences[si][0]
        if self.engine == config.ENGINE_TYPHOON:
            if not self.typhoon.running:
                return
            self.view.detail.setHtml("<p style='color:#9fb3d1'>Typhoon กำลังแปลแบบอื่น 3 แบบ "
                                     "(ประมาณ 10-30 วินาที)...</p>")
            self.typhoon_worker.submit(self._alternatives_typhoon, en)
            return
        if self.translator is None:
            return
        self.view.detail.setHtml("<p style='color:#9fb3d1'>กำลังหาคำแปลแบบอื่น...</p>")
        self.worker.submit(self._alternatives, en)

    def _on_tab_changed(self, index: int):
        if index == self.TAB_VOCAB:
            self.vocab_view.reload()
        elif index == self.TAB_REVIEW:
            self.review_view.refresh()

    def _restart_typhoon(self):
        if self.engine == config.ENGINE_TYPHOON:
            self.scan_id += 1
            self.typhoon.stop()
            self.typhoon_worker.submit(self._start_typhoon)

    def on_typhoon_device(self, device: str):
        if device == self.typhoon.device:
            return
        self.typhoon.device = device
        self.settings.setValue("typhoon_device", device)
        self._restart_typhoon()

    def on_typhoon_threads(self, threads: int):
        if threads == self.typhoon.threads:
            return
        self.typhoon.threads = threads
        self.settings.setValue("typhoon_threads", threads)
        self._restart_typhoon()

    def _engine_ready(self, label: str) -> bool:
        if label == config.ENGINE_TYPHOON:
            return self.typhoon.available
        return (config.MODELS[label] / "model.bin").exists()

    def _activate_engine(self, label: str):
        """Load the chosen engine and free the other one (only one fits comfortably in RAM)."""
        self.engine = label
        if label == config.ENGINE_TYPHOON:
            self.translator = None                           # free NLLB (~1 GB)
            self.typhoon_worker.submit(self._start_typhoon)
        else:
            self.typhoon.stop()                              # free Typhoon (~3 GB)
            self.worker.submit(self._load_model, label)

    def switch_model(self, label: str):
        if not self._engine_ready(label):
            hint = "--typhoon" if label == config.ENGINE_TYPHOON else "--large"
            self.status.setText(f"⚠ ยังไม่มีไฟล์โมเดล {label} — รัน download_assets.py {hint} ก่อน")
            self.model_combo.setCurrentText(self.engine)
            return
        if label == self.engine:
            return
        self.settings.setValue("engine", label)
        self.scan_id += 1                                    # drop work for the old engine
        self._activate_engine(label)
        self._retranslate()

    # ---- game glossary -----------------------------------------------------------
    def _refresh_glossary_view(self):
        self.glossary_view.set_profiles(self.glossary.profiles(), self.glossary.name)
        self.glossary_view.set_terms(self.glossary.manual)
        self.glossary_view.set_imported(len(self.glossary.imported), self.glossary.imported_info)

    def _retranslate(self):
        if self.last_lines:
            self.tabs.setCurrentIndex(self.TAB_TRANSLATE)
            self.view.show_message("กำลังแปลใหม่ด้วยศัพท์เกม...")
            self.worker.submit(self._retranslate_job)

    def on_glossary_profile(self, name: str):
        self.glossary.load(name or None)
        self.settings.setValue("glossary_profile", name)
        self._refresh_glossary_view()
        self._retranslate()

    def on_glossary_saved(self, name: str, terms: dict):
        self.glossary.save(terms, name)
        self.settings.setValue("glossary_profile", self.glossary.name)
        self._refresh_glossary_view()
        self.status.setText(f"✔ บันทึกศัพท์เกม '{self.glossary.name}' แล้ว ({len(self.glossary.terms)} คำ)")
        self._retranslate()

    # ---- import names from game language files ---------------------------------------
    def on_import_requested(self, kind: str):
        if not self.glossary.name:
            self.status.setText("⚠ เลือกหรือสร้างชุดศัพท์ก่อน แล้วค่อยนำเข้า")
            return
        progress = self.bridge.import_progress.emit
        if kind == "mojang":
            version, ok = QInputDialog.getText(
                self, "ดาวน์โหลดจาก Mojang",
                "เวอร์ชัน Minecraft (เช่น 1.20.1, 1.21.1)\nเว้นว่าง = เวอร์ชันล่าสุด\n"
                "จะดาวน์โหลดประมาณ 25 MB จากเซิร์ฟเวอร์ทางการของ Mojang", text="")
            if not ok:
                return
            version = version.strip() or "latest"
            job, source = (lambda: gamelang.download_vanilla(version, progress)), f"Minecraft {version} (Mojang)"
        elif kind == "folder":
            start = Path.home() / "AppData" / "Roaming" / ".minecraft"
            folder = QFileDialog.getExistingDirectory(
                self, "เลือกโฟลเดอร์ .minecraft หรือโฟลเดอร์ instance", str(start if start.exists() else Path.home()))
            if not folder:
                return
            job, source = (lambda: gamelang.scan_game_folder(Path(folder), progress)), Path(folder).name
        else:
            files, _ = QFileDialog.getOpenFileNames(self, "เลือกไฟล์ mod", str(Path.home()), "Mod (*.jar *.zip)")
            if not files:
                return
            paths = [Path(f) for f in files]
            job, source = (lambda: gamelang.scan_files(paths, progress)), ", ".join(p.name for p in paths[:3])
        self.status.setText("กำลังนำเข้า...")
        self.importer.submit(self._import_job, job, source)

    def _import_job(self, job, source: str):
        try:
            lang = job()
            if not lang.get("en_us"):
                self.bridge.import_failed.emit("ไม่พบไฟล์ภาษาอังกฤษ (en_us) ในไฟล์ที่เลือก")
                return
            terms, stats = gamelang.build_terms(lang, lambda w: bool(self.dictionary.lookup(w)[1]))
            self.bridge.import_done.emit(terms, source, stats)
        except Exception as e:
            traceback.print_exc()
            self.bridge.import_failed.emit(str(e))

    def on_import_done(self, terms: dict, source: str, stats: dict):
        # Add to earlier imports (vanilla + several mods); an official Thai name beats a kept-English one
        merged = dict(self.glossary.imported)
        for en, target in terms.items():
            if en not in merged or (merged[en].lower() == en and target.lower() != en):
                merged[en] = target
        sources = [s for s in self.glossary.imported_info.get("source", "").split(" + ") if s and s != source]
        info = {"source": " + ".join(sources + [source]), "date": datetime.now().strftime("%Y-%m-%d")}
        self.glossary.set_imported(merged, info)
        self._refresh_glossary_view()
        self.status.setText(f"✔ นำเข้า {len(terms)} คำจาก {source} — ชื่อไทยทางการ {stats['thai']}, "
                            f"คงชื่ออังกฤษ {stats['english']}  (รวมทั้งหมด {len(merged)} คำ)")
        self._retranslate()

    def on_clear_import(self):
        if self.glossary.name and self.glossary.imported:
            self.glossary.set_imported({}, {})
            self._refresh_glossary_view()
            self.status.setText("ล้างคำที่นำเข้าจากเกมแล้ว")
            self._retranslate()

    def on_auto_names(self, on: bool):
        self.auto_names_on = on
        self.settings.setValue("auto_names", on)
        self._retranslate()

    def on_add_term(self, word: str):
        if not self.glossary.name:
            self.status.setText("⚠ เลือกหรือสร้างชุดศัพท์ในแท็บ 'ศัพท์เกม' ก่อน")
            self.tabs.setCurrentWidget(self.glossary_view)
            return
        dialog = TermDialog(self, word, self.glossary.terms.get(word.lower(), ""))
        if dialog.exec():
            en, th = dialog.values()
            if en and th:
                self.on_glossary_saved(self.glossary.name, {**self.glossary.manual, en: th})

    def closeEvent(self, e):
        self.typhoon.stop()
        self.typhoon_worker.shutdown(wait=False, cancel_futures=True)
        self.settings.setValue("geometry", self.saveGeometry())
        self.hotkeys.stop()
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.importer.shutdown(wait=False, cancel_futures=True)
        QApplication.quit()
        super().closeEvent(e)


def main():
    if not config.DICT_DB.exists():
        print("Building dictionary database (first run only)...")
        build_dictionary(config.LEXITRON_SRC, config.DICT_DB)
    # Qt formats numbers with the Windows locale; on a Lao/Thai system that means ໔ / ๔
    # in spin boxes. Use English number formatting everywhere (text stays Thai).
    QLocale.setDefault(QLocale(QLocale.English, QLocale.UnitedStates))
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)
    window = App()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
