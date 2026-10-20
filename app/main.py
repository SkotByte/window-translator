"""Game Translator: select English text on screen -> OCR -> Thai translation + word lookup.

Everything runs offline and lives in one window (meant for a second monitor).
Run with:  python -m app.main
"""
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QImage, QScreen
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QTabWidget, QVBoxLayout, QWidget)

from . import config, winapi
from .dictionary import Dictionary, build as build_dictionary
from .glossary import Glossary
from .hotkeys import HotkeyListener
from .ocr import WindowsOcr
from .translator import Translator, needs_translation, split_sentences
from .ui import (APP_STYLE, WORD_RE, GlossaryView, ReviewView, SelectionOverlay, TermDialog,
                 TranslationView, VocabView)
from .vocab import Vocab


class Bridge(QObject):
    """Signals used to hand results from the worker thread back to the UI thread."""
    status = Signal(str)
    result = Signal(list)
    alternatives = Signal(str, list)
    error = Signal(str)


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
            blocks[-1] += " " + line
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
        self.translator: Translator | None = None
        self.ocr = WindowsOcr()
        self.dictionary = Dictionary(config.DICT_DB)
        self.vocab = Vocab(config.VOCAB_DB)
        self.settings = QSettings("game-translator", "app")
        self.glossary = Glossary(config.GLOSSARY_DIR)
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
        self.bridge.alternatives.connect(self.view.show_alternatives)
        self.bridge.error.connect(lambda m: self.view.show_message("เกิดข้อผิดพลาด: " + m))
        self.view.word_clicked.connect(self.on_word_clicked)
        self.view.alt_requested.connect(self.on_alt_requested)
        self.view.add_term_requested.connect(self.on_add_term)
        self.glossary_view.profile_changed.connect(self.on_glossary_profile)
        self.glossary_view.saved.connect(self.on_glossary_saved)
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

        self.worker.submit(self._load_model)

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
        for label in config.MODELS:
            self.model_combo.addItem(label)
        self.model_combo.setCurrentText(self.settings.value("model", config.DEFAULT_MODEL))
        self.model_combo.activated.connect(lambda _: self.switch_model(self.model_combo.currentText()))
        row2 = QHBoxLayout()
        row2.addWidget(select)
        row2.addWidget(rescan)
        row2.addStretch(1)
        row2.addWidget(QLabel("โมเดล:"))
        row2.addWidget(self.model_combo)

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
            image = image.convertToFormat(QImage.Format_ARGB32)       # BGRA byte order in memory
            if image.width() < config.OCR_UPSCALE_BELOW:
                scale = min(3, config.OCR_UPSCALE_BELOW // max(1, image.width()) + 1,
                            self.ocr.max_dimension // max(1, image.width(), image.height()))
                if scale > 1:
                    image = image.scaled(image.width() * scale, image.height() * scale,
                                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
            raw = bytes(image.constBits())[: image.sizeInBytes()]
            self.last_lines = self.ocr.recognize(raw, image.width(), image.height())
            self._translate_lines(self.last_lines)
        except Exception as e:
            traceback.print_exc()
            self.bridge.error.emit(str(e))

    def _translate_lines(self, lines: list[str]):
        """Translate OCR lines. Order of preference per sentence:
        no English word -> shown as-is; whole text is a game term -> glossary;
        short label (<= 3 words) found in the dictionary -> dictionary meanings;
        otherwise NLLB, with game terms swapped in first."""
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
                elif short := self._short_meaning(s):
                    results.append((s, short, "dict"))
                else:
                    pending.append((len(results), s))
                    results.append(None)
        if pending:
            protected = [self.glossary.protect(s, self._name_checker()) for _, s in pending]
            outs = self.translator.translate_sentences([text for text, _ in protected])
            for (i, s), (_, mapping), versions in zip(pending, protected, outs):
                results[i] = (s, self.glossary.restore(versions[0], mapping), "nllb")
        self.bridge.result.emit(results)

    def _name_checker(self):
        """Word test used to spot proper names, or None when auto-keeping names is switched off."""
        if not self.auto_names_on:
            return None
        return lambda word: bool(self.dictionary.lookup(word)[1])

    def _short_meaning(self, text: str) -> str | None:
        """Dictionary meanings for short labels like 'Utility' or 'Pick up'; the model guesses these badly."""
        words = WORD_RE.findall(text)
        if not 1 <= len(words) <= 3:
            return None
        _, entries = self.dictionary.lookup(" ".join(words))
        if not entries:
            return None
        return " / ".join(list(dict.fromkeys(e["thai"] for e in entries))[:3])

    def _retranslate_job(self):
        try:
            if self.last_lines:
                self._translate_lines(self.last_lines)
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
        en, th, _ = self.view.sentences[si]
        words = self.view.words[si]
        word = words[wi]
        base, entries = self.dictionary.lookup(word)
        phrases = self.dictionary.phrases(words, wi)
        self.view.show_word(word, base, entries, phrases)
        if entries:
            meaning = ", ".join(dict.fromkeys(e["thai"] for e in entries[:4]))
            self.vocab.record(base, meaning, entries[0]["cat"], en, th)
        for phrase, p_entries in phrases:
            meaning = ", ".join(dict.fromkeys(e["thai"] for e in p_entries[:4]))
            self.vocab.record(phrase, meaning, p_entries[0]["cat"], en, th)

    def on_alt_requested(self, si: int):
        if self.translator is None:
            return
        self.view.detail.setHtml("<p style='color:#9fb3d1'>กำลังหาคำแปลแบบอื่น...</p>")
        self.worker.submit(self._alternatives, self.view.sentences[si][0])

    def _on_tab_changed(self, index: int):
        if index == self.TAB_VOCAB:
            self.vocab_view.reload()
        elif index == self.TAB_REVIEW:
            self.review_view.refresh()

    def switch_model(self, label: str):
        if not (config.MODELS[label] / "model.bin").exists():
            self.status.setText(f"⚠ ยังไม่มีไฟล์โมเดล {label} — รัน download_assets.py --large ก่อน")
            self.model_combo.setCurrentText(self.settings.value("model", config.DEFAULT_MODEL))
            return
        self.settings.setValue("model", label)
        self.worker.submit(self._load_model, label)
        self._retranslate()

    # ---- game glossary -----------------------------------------------------------
    def _refresh_glossary_view(self):
        self.glossary_view.set_profiles(self.glossary.profiles(), self.glossary.name)
        self.glossary_view.set_terms(self.glossary.terms)

    def _retranslate(self):
        if self.last_lines:
            self.tabs.setCurrentIndex(self.TAB_TRANSLATE)
            self.view.show_message("กำลังแปลใหม่ด้วยศัพท์เกม...")
            self.worker.submit(self._retranslate_job)

    def on_glossary_profile(self, name: str):
        self.glossary.load(name or None)
        self.settings.setValue("glossary_profile", name)
        self.glossary_view.set_terms(self.glossary.terms)
        self._retranslate()

    def on_glossary_saved(self, name: str, terms: dict):
        self.glossary.save(terms, name)
        self.settings.setValue("glossary_profile", self.glossary.name)
        self._refresh_glossary_view()
        self.status.setText(f"✔ บันทึกศัพท์เกม '{self.glossary.name}' แล้ว ({len(self.glossary.terms)} คำ)")
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
                self.on_glossary_saved(self.glossary.name, {**self.glossary.terms, en: th})

    def closeEvent(self, e):
        self.settings.setValue("geometry", self.saveGeometry())
        self.hotkeys.stop()
        self.worker.shutdown(wait=False, cancel_futures=True)
        QApplication.quit()
        super().closeEvent(e)


def main():
    if not config.DICT_DB.exists():
        print("Building dictionary database (first run only)...")
        build_dictionary(config.LEXITRON_SRC, config.DICT_DB)
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)
    window = App()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
