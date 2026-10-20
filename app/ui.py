"""Widgets of the app: area selector overlay, translation view, vocabulary view."""
import html
import re
from datetime import datetime

from PySide6.QtCore import QRect, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
                               QWidget)

from .dictionary import CAT_TH
from .vocab import AGAIN, EASY, GOOD, HARD, CardState, describe_delay, schedule

UI_FONT = "Leelawadee UI"     # ships with Windows, renders Thai well
WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")

APP_STYLE = f"""
    QWidget {{ background: #12141c; color: #e8edf6; font: 11pt '{UI_FONT}'; }}
    QLabel#hint {{ color: #6f86ad; font-size: 9pt; }}
    QLabel#status {{ color: #9fb3d1; }}
    QPushButton {{ background: #2a3550; border: none; border-radius: 5px; padding: 5px 12px; }}
    QPushButton:hover {{ background: #3b4a70; }}
    QPushButton:checked {{ background: #4b6fb0; }}
    QComboBox {{ background: #1e2333; border: 1px solid #3a4a66; border-radius: 4px; padding: 4px 8px; }}
    QComboBox QAbstractItemView {{ background: #1e2333; selection-background-color: #3b4a70; }}
    QTabWidget::pane {{ border: 1px solid #2a3550; border-radius: 6px; }}
    QTabBar::tab {{ background: #1a1f2c; padding: 6px 16px; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
    QTabBar::tab:selected {{ background: #2a3550; }}
    QTextBrowser {{ background: #161a25; border: none; border-radius: 6px; padding: 6px; font-size: 13pt; }}
    QTableWidget {{ background: #161a25; gridline-color: #2a3550; }}
    QHeaderView::section {{ background: #1e2333; color: #9fb3d1; border: none; padding: 4px; }}
    QSplitter::handle {{ background: #2a3550; height: 3px; }}
"""


class SelectionOverlay(QWidget):
    """Full-screen frozen screenshot; drag a rectangle around the text to translate."""
    selected = Signal(QRect, QRect)    # (rect in screenshot pixels, rect in global screen coords)

    def __init__(self, screen, pixmap: QPixmap, highlight: QRect | None = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.pixmap = pixmap
        self.highlight = highlight      # target window outline, in screenshot pixels
        self.origin = None
        self.current = QRect()
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CrossCursor)
        self.setAttribute(Qt.WA_DeleteOnClose)

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.pixmap)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self.highlight is not None:
            p.setPen(QPen(QColor(255, 200, 60), 2, Qt.DashLine))
            p.drawRect(self._to_widget(self.highlight))
        if not self.current.isNull():
            # Show the selected part undimmed
            src = self._to_pixels(self.current)
            p.drawPixmap(self.current, self.pixmap, src)
            p.setPen(QPen(QColor(80, 200, 255), 2))
            p.drawRect(self.current)
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont(UI_FONT, 12))
        p.drawText(20, 34, "ลากเมาส์ครอบข้อความที่ต้องการแปล  •  Esc = ยกเลิก")

    def _to_pixels(self, r: QRect) -> QRect:
        # Screenshot is in physical pixels, widget in logical (DPI-scaled) units
        sx = self.pixmap.width() / max(1, self.width())
        sy = self.pixmap.height() / max(1, self.height())
        return QRect(round(r.x() * sx), round(r.y() * sy), round(r.width() * sx), round(r.height() * sy))

    def _to_widget(self, r: QRect) -> QRect:
        sx = self.width() / max(1, self.pixmap.width())
        sy = self.height() / max(1, self.pixmap.height())
        return QRect(round(r.x() * sx), round(r.y() * sy), round(r.width() * sx), round(r.height() * sy))

    def mousePressEvent(self, e):
        self.origin = e.position().toPoint()
        self.current = QRect(self.origin, self.origin)
        self.update()

    def mouseMoveEvent(self, e):
        if self.origin is not None:
            self.current = QRect(self.origin, e.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, _):
        r = self.current
        self.close()
        if r.width() > 8 and r.height() > 8:
            self.selected.emit(self._to_pixels(r), r.translated(self.geometry().topLeft()))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()


class TranslationView(QWidget):
    """Translated sentences on top, details of the clicked word below."""
    word_clicked = Signal(int, int)      # sentence index, word index
    alt_requested = Signal(int)          # sentence index
    add_term_requested = Signal(str)     # english word to put in the game glossary

    DETAIL_HINT = "<p style='color:#6f86ad'>คลิกคำภาษาอังกฤษด้านบนเพื่อดูความหมาย</p>"

    def __init__(self):
        super().__init__()
        self.sentences: list[tuple[str, str]] = []
        self.words: list[list[str]] = []

        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.anchorClicked.connect(self._on_link)
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        self.detail.anchorClicked.connect(self._on_link)
        self.detail.setHtml(self.DETAIL_HINT)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.text)
        splitter.addWidget(self.detail)
        splitter.setSizes([300, 400])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(splitter)
        self.show_message("กด Ctrl+Shift+T แล้วลากครอบข้อความในเกม")

    def show_message(self, msg: str):
        self.sentences, self.words = [], []
        self.text.setHtml(f"<p style='color:#9fb3d1'>{html.escape(msg)}</p>")

    SOURCE_TAG = {"glossary": "ศัพท์เกม", "dict": "พจนานุกรม"}

    def show_result(self, pairs: list[tuple[str, str, str]]):
        """pairs: (english, thai, source) where source is nllb / glossary / dict / none."""
        self.sentences = pairs
        self.words = [WORD_RE.findall(en) for en, _, _ in pairs]
        if not pairs:
            self.show_message("ไม่พบข้อความภาษาอังกฤษในพื้นที่ที่เลือก")
            return
        parts = []
        for si, (en, th, source) in enumerate(pairs):
            if source == "none":      # numbers, versions... shown as-is
                parts.append(f"<p style='margin:0 0 12px 0; color:#6f86ad'>{html.escape(en)}</p>")
                continue
            tag = (f" <span style='color:#6f86ad; font-size:9pt'>({self.SOURCE_TAG[source]})</span>"
                   if source in self.SOURCE_TAG else "")
            parts.append(f"<p style='margin:0 0 2px 0'>{self._linkify(si, en)}</p>"
                         f"<p style='margin:0 0 12px 0; color:#ffd36b; font-size:14pt'>{html.escape(th)}{tag}"
                         f" <a href='alt:{si}' style='color:#6f86ad; font-size:9pt'>[แปลแบบอื่น]</a></p>")
        self.text.setHtml("".join(parts))
        self.detail.setHtml(self.DETAIL_HINT)

    def _linkify(self, si: int, sentence: str) -> str:
        """Turn every English word into a link while keeping punctuation/spaces as-is."""
        out, last, wi = [], 0, 0
        for m in WORD_RE.finditer(sentence):
            out.append(html.escape(sentence[last:m.start()]))
            out.append(f"<a href='w:{si}:{wi}' style='color:#e8edf6; text-decoration:none'>"
                       f"{html.escape(m.group())}</a>")
            last, wi = m.end(), wi + 1
        out.append(html.escape(sentence[last:]))
        return "".join(out)

    def show_word(self, word: str, base: str, entries: list[dict], phrases: list):
        h = [f"<p style='margin:0'><b style='font-size:17pt; color:#7fd4ff'>{html.escape(word)}</b>"]
        if base.lower() != word.lower():
            h.append(f" <span style='color:#9fb3d1'>(รูปพื้นฐาน: {html.escape(base)})</span>")
        h.append(f" &nbsp;<a href='add:{html.escape(base)}' style='color:#b9f57f; font-size:10pt'>"
                 "[+ เพิ่มลงศัพท์เกม]</a></p>")
        if not entries:
            h.append("<p style='color:#9fb3d1'>ไม่พบคำนี้ในพจนานุกรม (อาจเป็นชื่อเฉพาะในเกม)</p>")
        h.append(self._entries_html(entries))
        for phrase, p_entries in phrases:
            h.append(f"<p style='margin:12px 0 0 0'><b style='color:#b9f57f'>วลี: {html.escape(phrase)}</b></p>")
            h.append(self._entries_html(p_entries))
        self.detail.setHtml("".join(h))

    @staticmethod
    def _entries_html(entries: list[dict]) -> str:
        by_cat: dict[str, list[dict]] = {}
        for e in entries:
            by_cat.setdefault(e["cat"], []).append(e)
        h = []
        for cat, items in by_cat.items():
            h.append(f"<p style='margin:6px 0 0 0; color:#9fb3d1'>{html.escape(CAT_TH.get(cat, cat))}</p><ol style='margin:0'>")
            for e in items:
                extra = f" <span style='color:#8a97ad'>— {html.escape(e['ethai'])}</span>" if e.get("ethai") else ""
                syn = f"<br><span style='color:#6f86ad; font-size:10pt'>คำเหมือน: {html.escape(e['syn'])}</span>" if e.get("syn") else ""
                h.append(f"<li>{html.escape(e['thai'])}{extra}{syn}</li>")
            h.append("</ol>")
        return "".join(h)

    def show_alternatives(self, en: str, versions: list[str]):
        items = "".join(f"<li>{html.escape(v)}</li>" for v in versions)
        self.detail.setHtml(
            f"<p style='margin:0; color:#9fb3d1'>คำแปลแบบอื่นของ:</p><p style='margin:0'>{html.escape(en)}</p>"
            f"<ol style='color:#ffd36b'>{items}</ol>"
            "<p style='color:#6f86ad; font-size:9pt'>* เป็นการเรียบเรียงคำแปลแบบอื่นจากโมเดล "
            "ไม่ใช่การวิเคราะห์ความหมายแฝง</p>")

    def _on_link(self, url):
        kind, _, rest = url.toString(QUrl.PrettyDecoded).partition(":")
        if kind == "w":
            si, wi = rest.split(":")
            self.word_clicked.emit(int(si), int(wi))
        elif kind == "alt":
            self.alt_requested.emit(int(rest))
        elif kind == "add":
            self.add_term_requested.emit(rest)


class VocabView(QWidget):
    """Table of every word you have looked up."""
    COLS = ("คำ", "ความหมาย", "ชนิดคำ", "ค้นกี่ครั้ง", "ล่าสุด", "ประโยคที่เจอ", "ทบทวนครั้งถัดไป")

    def __init__(self, vocab):
        super().__init__()
        self.vocab = vocab
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        refresh = QPushButton("รีเฟรช")
        refresh.clicked.connect(self.reload)
        delete = QPushButton("ลบคำที่เลือก")
        delete.clicked.connect(self._delete)
        self.count = QLabel()
        bar = QHBoxLayout()
        bar.addWidget(self.count, 1)
        bar.addWidget(refresh)
        bar.addWidget(delete)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(bar)
        lay.addWidget(self.table)

    def reload(self):
        rows = self.vocab.all()
        self.table.setRowCount(len(rows))
        now = datetime.now().isoformat(timespec="seconds")
        for r, (word, meaning, cat, lookups, last, ctx_en, ctx_th, due) in enumerate(rows):
            next_review = "ถึงกำหนดแล้ว" if (due or "") <= now else due[:16].replace("T", " ")
            values = (word, meaning, CAT_TH.get(cat, cat or ""), str(lookups), last.replace("T", " "),
                      f"{ctx_en}\n{ctx_th}", next_review)
            for c, v in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.resizeColumnsToContents()
        self.table.resizeRowsToContents()
        self.count.setText(f"ทั้งหมด {len(rows)} คำ")

    def _delete(self):
        for idx in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.vocab.delete(self.table.item(idx, 0).text())
        self.reload()


class TermDialog(QDialog):
    """Small form: English term + the translation it should always get."""

    def __init__(self, parent, english: str = "", thai: str = ""):
        super().__init__(parent)
        self.setWindowTitle("เพิ่มศัพท์เกม")
        self.english = QLineEdit(english)
        self.thai = QLineEdit(thai)
        self.thai.setPlaceholderText("คำแปลภาษาไทย หรือพิมพ์ชื่อภาษาอังกฤษเพื่อไม่ให้แปล")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("คำ / วลีภาษาอังกฤษ:", self.english)
        form.addRow("ให้แปลเป็น:", self.thai)
        form.addRow(buttons)
        self.resize(460, 0)
        self.thai.setFocus()

    def values(self) -> tuple[str, str]:
        return self.english.text().strip(), self.thai.text().strip()


class GlossaryView(QWidget):
    """Edit the per-game glossary (terms that must always be translated a fixed way)."""
    profile_changed = Signal(str)        # profile name, "" = no glossary
    saved = Signal(str, dict)            # profile name, terms
    COLS = ("คำ / วลีภาษาอังกฤษ", "ให้แปลเป็น (ไทย หรือคงชื่ออังกฤษ)")

    def __init__(self):
        super().__init__()
        self.profile = QComboBox()
        self.profile.activated.connect(lambda _: self.profile_changed.emit(self.profile.currentData() or ""))
        new_btn = QPushButton("+ สร้างชุดศัพท์ใหม่")
        new_btn.clicked.connect(self._new_profile)
        top = QHBoxLayout()
        top.addWidget(QLabel("ชุดศัพท์:"))
        top.addWidget(self.profile, 1)
        top.addWidget(new_btn)
        self.auto_names = QCheckBox("คงชื่อเฉพาะเป็นภาษาอังกฤษอัตโนมัติ (คำขึ้นต้นตัวใหญ่ เช่น Ancient Debris, Pan Et Rosa)")

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(False)

        add = QPushButton("เพิ่มแถว")
        add.clicked.connect(self._add_row)
        delete = QPushButton("ลบแถวที่เลือก")
        delete.clicked.connect(self._delete_rows)
        save = QPushButton("💾 บันทึก แล้วแปลใหม่")
        save.clicked.connect(self._save)
        bottom = QHBoxLayout()
        bottom.addWidget(add)
        bottom.addWidget(delete)
        bottom.addStretch(1)
        bottom.addWidget(save)

        hint = QLabel("คำในตารางจะถูกแทนก่อนส่งไปแปลเสมอ เช่น creeper → Creeper (ไม่แปลชื่อ), "
                      "chest → หีบ  •  รองรับรูปพหูพจน์ -s/-es อัตโนมัติ")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(top)
        lay.addWidget(self.auto_names)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)
        lay.addWidget(hint)

    def set_profiles(self, names: list[str], current: str | None):
        self.profile.clear()
        self.profile.addItem("— ไม่ใช้ศัพท์เกม —", "")
        for n in names:
            self.profile.addItem(n, n)
        i = self.profile.findData(current or "")
        self.profile.setCurrentIndex(max(0, i))

    def set_terms(self, terms: dict[str, str]):
        self.table.setRowCount(0)
        for en, th in sorted(terms.items()):
            self._add_row(en, th)

    def _add_row(self, en: str = "", th: str = ""):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(en))
        self.table.setItem(r, 1, QTableWidgetItem(th))
        if not en:
            self.table.scrollToBottom()
            self.table.editItem(self.table.item(r, 0))

    def _delete_rows(self):
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def _terms(self) -> dict[str, str]:
        terms = {}
        for r in range(self.table.rowCount()):
            en, th = (self.table.item(r, c).text() if self.table.item(r, c) else "" for c in (0, 1))
            if en.strip() and th.strip():
                terms[en.strip()] = th.strip()
        return terms

    def _save(self):
        name = self.profile.currentData()
        if not name:
            name = self._ask_name()
            if not name:
                return
        self.saved.emit(name, self._terms())

    def _new_profile(self):
        name = self._ask_name()
        if name:
            self.table.setRowCount(0)
            self.saved.emit(name, {})

    def _ask_name(self) -> str:
        name, ok = QInputDialog.getText(self, "ชุดศัพท์ใหม่", "ชื่อเกม:")
        return name.strip() if ok else ""


class ReviewView(QWidget):
    """Flashcard review: see the word, try to recall, reveal, then rate how well you remembered."""
    GRADES = ((AGAIN, "จำไม่ได้", "#b04b4b"), (HARD, "ยาก", "#a07a32"),
              (GOOD, "จำได้", "#3f8a4f"), (EASY, "ง่ายมาก", "#3b6fb0"))

    def __init__(self, vocab):
        super().__init__()
        self.vocab = vocab
        self.queue: list[dict] = []
        self.card: dict | None = None
        self.revealed = False
        self.done_count = 0
        self.setFocusPolicy(Qt.StrongFocus)

        self.stats = QLabel()
        self.reverse = QCheckBox("ถามจากภาษาไทย → ตอบอังกฤษ")
        start = QPushButton("▶ เริ่มทบทวน")
        start.clicked.connect(self.start)
        top = QHBoxLayout()
        top.addWidget(self.stats, 1)
        top.addWidget(self.reverse)
        top.addWidget(start)

        self.front = QLabel()
        self.front.setAlignment(Qt.AlignCenter)
        self.front.setWordWrap(True)
        self.front.setStyleSheet("font-size: 26pt; color: #7fd4ff; padding: 18px;")
        self.front_context = QLabel()
        self.front_context.setAlignment(Qt.AlignCenter)
        self.front_context.setWordWrap(True)
        self.front_context.setStyleSheet("color: #9fb3d1; font-size: 12pt;")
        self.back = QLabel()
        self.back.setAlignment(Qt.AlignCenter)
        self.back.setWordWrap(True)
        self.back.setTextFormat(Qt.RichText)
        self.back.setStyleSheet("font-size: 14pt; padding: 12px;")

        self.reveal_btn = QPushButton("เฉลย  (Space)")
        self.reveal_btn.clicked.connect(self.reveal)
        self.grade_btns = []
        grades = QHBoxLayout()
        for key, (grade, label, color) in enumerate(self.GRADES, start=1):
            b = QPushButton()
            b.setStyleSheet(f"QPushButton {{ background: {color}; padding: 8px; }}")
            b.clicked.connect(lambda _=False, g=grade: self.rate(g))
            b.setProperty("label", f"{label}  ({key})")
            grades.addWidget(b)
            self.grade_btns.append(b)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addLayout(top)
        lay.addStretch(1)
        lay.addWidget(self.front)
        lay.addWidget(self.front_context)
        lay.addWidget(self.back)
        lay.addStretch(1)
        lay.addWidget(self.reveal_btn)
        lay.addLayout(grades)
        hint = QLabel("คีย์ลัด: Space = เฉลย  •  1 จำไม่ได้  2 ยาก  3 จำได้  4 ง่ายมาก  "
                      "•  ตอบตามจริงจะได้ผลดีที่สุด")
        hint.setObjectName("hint")
        lay.addWidget(hint)
        self.refresh()

    def refresh(self):
        """Update the counters and show the idle screen when no session is running."""
        s = self.vocab.stats()
        self.stats.setText(f"ถึงกำหนดทบทวน <b>{s['due']}</b> คำ  •  คำใหม่ {s['new']}  •  "
                           f"จำได้ดีแล้ว (≥21 วัน) {s['learned']}/{s['total']}")
        if self.card is None:
            self._show_idle()

    def _show_idle(self):
        due = self.vocab.stats()["due"]
        if self.done_count:
            self.front.setText("🎉 ทบทวนเสร็จแล้ว")
            self.front_context.setText(f"วันนี้ทบทวนไป {self.done_count} ครั้ง")
        elif due:
            self.front.setText(f"มี {due} คำรอทบทวน")
            self.front_context.setText("กด ▶ เริ่มทบทวน")
        else:
            self.front.setText("ยังไม่มีคำที่ต้องทบทวน")
            self.front_context.setText("คลิกคำในแท็บ 'แปล' เพื่อเก็บคำศัพท์ แล้วคำจะมาให้ทบทวนที่นี่")
        self.back.clear()
        self.reveal_btn.hide()
        for b in self.grade_btns:
            b.hide()

    def start(self):
        self.queue = self.vocab.due_cards()
        self.done_count = 0
        self._next()
        self.setFocus()

    def _next(self):
        self.card = self.queue.pop(0) if self.queue else None
        self.revealed = False
        if self.card is None:
            self.refresh()
            return
        c = self.card
        if self.reverse.isChecked():
            self.front.setText(c["meaning"] or "")
            self.front_context.setText(c["context_th"] or "")
        else:
            self.front.setText(c["word"])
            self.front_context.setText(self._highlight(c["context_en"] or "", c["word"]))
        self.back.clear()
        self.reveal_btn.show()
        for b in self.grade_btns:
            b.hide()
        self.stats.setText(f"เหลืออีก {len(self.queue) + 1} คำในรอบนี้")

    @staticmethod
    def _highlight(sentence: str, word: str) -> str:
        """Bold the studied word (any inflection starting with it) inside the context sentence."""
        stem = re.escape(word.split()[0][:max(3, len(word) - 2)])
        parts = re.split(rf"(\b{stem}\w*)", sentence, flags=re.IGNORECASE)
        return "".join(f"<b style='color:#ffd36b'>{html.escape(p)}</b>" if i % 2 else html.escape(p)
                       for i, p in enumerate(parts))

    def reveal(self):
        if self.card is None or self.revealed:
            return
        self.revealed = True
        c = self.card
        cat = html.escape(CAT_TH.get(c["cat"], c["cat"] or ""))
        if self.reverse.isChecked():
            self.back.setText(f"<span style='font-size:24pt; color:#7fd4ff'>{html.escape(c['word'])}</span>"
                              f"<br><span style='color:#9fb3d1'>{cat}</span>"
                              f"<br><br>{self._highlight(c['context_en'] or '', c['word'])}")
        else:
            self.back.setText(f"<span style='color:#ffd36b'>{html.escape(c['meaning'] or '')}</span>"
                              f"<br><span style='color:#9fb3d1; font-size:11pt'>{cat}</span>"
                              f"<br><br><span style='font-size:12pt'>{html.escape(c['context_th'] or '')}</span>")
        state = CardState(c["interval"], c["ease"], c["reps"], c["lapses"])
        for b, (grade, _, _) in zip(self.grade_btns, self.GRADES):
            _, delay = schedule(state, grade)
            b.setText(f"{b.property('label')}\n{describe_delay(delay)}")
            b.show()
        self.reveal_btn.hide()

    def rate(self, grade: int):
        if self.card is None or not self.revealed:
            return
        c = self.card
        new_state, _ = schedule(CardState(c["interval"], c["ease"], c["reps"], c["lapses"]), grade)
        self.vocab.review(c["word"], grade)
        self.done_count += 1
        if grade == AGAIN:
            # Forgotten words come back at the end of this session too
            c.update(interval=new_state.interval, ease=new_state.ease, reps=new_state.reps,
                     lapses=new_state.lapses)
            self.queue.append(c)
        self._next()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space:
            self.reveal()
        elif Qt.Key_1 <= e.key() <= Qt.Key_4:
            self.rate(self.GRADES[e.key() - Qt.Key_1][0])
        else:
            super().keyPressEvent(e)
