"""English -> Thai word lookup backed by the LEXiTRON dictionary (NECTEC).

The raw etlex file is converted once into SQLite (see build()) so lookups are instant.
"""
import re
import sqlite3
from pathlib import Path

CAT_TH = {
    "N": "คำนาม", "V": "คำกริยา", "VT": "คำกริยา (มีกรรม)", "VI": "คำกริยา (ไม่มีกรรม)",
    "ADJ": "คำคุณศัพท์", "ADV": "คำกริยาวิเศษณ์", "PREP": "คำบุพบท", "CONJ": "คำสันธาน",
    "PRON": "คำสรรพนาม", "DET": "คำนำหน้านาม", "ART": "คำนำหน้านาม", "AUX": "กริยาช่วย",
    "INT": "คำอุทาน", "ABBR": "คำย่อ", "PHRV": "กริยาวลี (phrasal verb)", "IDM": "สำนวน",
    "SL": "คำสแลง", "PRF": "คำนำหน้า (prefix)", "SUF": "คำต่อท้าย (suffix)",
}

# Common irregular forms -> base form (dictionary only stores base forms)
IRREGULAR = {
    "am": "be", "is": "be", "are": "be", "was": "be", "were": "be", "been": "be", "being": "be",
    "has": "have", "had": "have", "does": "do", "did": "do", "done": "do",
    "went": "go", "gone": "go", "made": "make", "took": "take", "taken": "take",
    "came": "come", "saw": "see", "seen": "see", "got": "get", "gotten": "get",
    "gave": "give", "given": "give", "found": "find", "knew": "know", "known": "know",
    "thought": "think", "told": "tell", "said": "say", "left": "leave", "felt": "feel",
    "brought": "bring", "bought": "buy", "fought": "fight", "caught": "catch",
    "taught": "teach", "held": "hold", "kept": "keep", "slept": "sleep", "ran": "run",
    "began": "begin", "begun": "begin", "wrote": "write", "written": "write",
    "spoke": "speak", "spoken": "speak", "stole": "steal", "stolen": "steal",
    "fell": "fall", "fallen": "fall", "rose": "rise", "risen": "rise", "lost": "lose",
    "won": "win", "met": "meet", "sent": "send", "spent": "spend", "built": "build",
    "led": "lead", "fled": "flee", "hid": "hide", "hidden": "hide", "broke": "break",
    "broken": "break", "chose": "choose", "chosen": "choose", "drew": "draw", "drawn": "draw",
    "drove": "drive", "driven": "drive", "ate": "eat", "eaten": "eat", "flew": "fly",
    "flown": "fly", "forgot": "forget", "forgotten": "forget", "froze": "freeze",
    "frozen": "freeze", "grew": "grow", "grown": "grow", "rode": "ride", "ridden": "ride",
    "shook": "shake", "shaken": "shake", "shot": "shoot", "struck": "strike", "swore": "swear",
    "threw": "throw", "thrown": "throw", "woke": "wake", "wore": "wear", "worn": "wear",
    "men": "man", "women": "woman", "children": "child", "feet": "foot", "teeth": "tooth",
    "mice": "mouse", "people": "person", "better": "good", "best": "good",
    "worse": "bad", "worst": "bad", "dying": "die", "lying": "lie",
}

_DOC_RE = re.compile(r"<Doc>(.*?)</Doc>", re.S)
_FIELD_RE = re.compile(r"<(esearch|eentry|tentry|ecat|ethai|esyn|eant)>(.*?)</\1>", re.S)


def build(src: Path, db: Path) -> int:
    """Convert LEXiTRON etlex file into an indexed SQLite DB. Returns entry count."""
    text = src.read_text(encoding="utf-8", errors="replace")
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE entries (
        search TEXT NOT NULL, entry TEXT, thai TEXT, cat TEXT, ethai TEXT, syn TEXT, ant TEXT)""")
    rows = []
    for doc in _DOC_RE.finditer(text):
        f = {}
        for name, value in _FIELD_RE.findall(doc.group(1)):
            f.setdefault(name, value.strip())
        if not f.get("eentry") or not f.get("tentry"):
            continue
        # Clean up stray spaces and Thai characters that leaked into category codes
        cat = re.sub(r"[^A-Z,]", "", f.get("ecat", "").upper()).split(",")[0]
        search = f.get("esearch") or f["eentry"]
        rows.append((search.lower(), f["eentry"], f["tentry"], cat,
                     f.get("ethai"), f.get("esyn"), f.get("eant")))
    conn.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?,?)", rows)
    conn.execute("CREATE INDEX idx_search ON entries(search)")
    conn.commit()
    conn.close()
    return len(rows)


def base_forms(word: str) -> list[str]:
    """Guess dictionary forms of an inflected English word (simple rule-based lemmatizer)."""
    w = word.lower().strip("'’")
    w = re.sub(r"(['’]s|['’]ll|['’]re|['’]ve|['’]d|n['’]t)$", "", w) or w
    out = [w]
    if w in IRREGULAR:
        out.append(IRREGULAR[w])

    def add(c):
        if len(c) >= 2 and c not in out:
            out.append(c)

    if w.endswith("ies"):
        add(w[:-3] + "y")
    if w.endswith("es"):
        add(w[:-2])
    if w.endswith("s") and not w.endswith("ss"):
        add(w[:-1])
    for suffix in ("ed", "ing", "er", "est"):
        if w.endswith(suffix):
            stem = w[: -len(suffix)]
            add(stem)
            add(stem + "e")                       # loved -> love, making -> make
            if len(stem) > 2 and stem[-1] == stem[-2]:
                add(stem[:-1])                    # stopped -> stop, running -> run
            if stem.endswith("i"):
                add(stem[:-1] + "y")              # tried -> try, happier -> happy
    if w.endswith("ly"):
        add(w[:-2])
        add(w[:-3] + "y" if w.endswith("ily") else w[:-2])
    return out


class Dictionary:
    def __init__(self, db_path: Path):
        # Read-only use from several threads; sqlite3 handles this for SELECTs
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)

    def _find(self, term: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT entry, thai, cat, ethai, syn, ant FROM entries WHERE search = ? LIMIT 30",
            (term.lower(),))
        return [dict(zip(("entry", "thai", "cat", "ethai", "syn", "ant"), r)) for r in cur]

    def lookup(self, word: str) -> tuple[str, list[dict]]:
        """Return (matched base form, entries). Entries empty if nothing found."""
        for form in base_forms(word):
            entries = self._find(form)
            if entries:
                return form, entries
        return word.lower(), []

    def phrases(self, words: list[str], start: int, max_len: int = 4) -> list[tuple[str, list[dict]]]:
        """Find phrasal verbs / idioms starting at words[start], e.g. 'pick' + 'up'."""
        found = []
        firsts = base_forms(words[start])
        for n in range(2, max_len + 1):
            tail = words[start + 1:start + n]
            if len(tail) < n - 1:
                break
            tail = [t.lower() for t in tail]
            for first in firsts:
                phrase = " ".join([first] + tail)
                entries = self._find(phrase)
                if entries:
                    found.append((phrase, entries))
                    break
        return found
