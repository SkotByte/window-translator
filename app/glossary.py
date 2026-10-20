"""Per-game glossaries: fixed translations for game terms (mob names, items, UI words).

Each profile is a JSON file in glossary/:  {"name": "Minecraft", "terms": {"creeper": "Creeper", ...}}
A value in English (e.g. a monster name) keeps the term untranslated; a Thai value forces that wording.
Terms are swapped for placeholders before translation and replaced with the fixed wording after.
"""
import json
import re
from pathlib import Path
from typing import Callable

from .translator import THAI_DIGITS

_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:ZZ\d+|[A-Za-z][A-Za-z0-9]*(?:[-'’][A-Za-z0-9]+)*)")
_CONNECTORS = {"of", "the", "et", "de", "du", "la", "le", "von", "van", "and"}
_AUTHOR = re.compile(r"^\s*by\s+(.+?)[\s:.]*$", re.IGNORECASE)
# Capitalized at the start of a sentence but never names (LEXiTRON lacks some pronouns)
_COMMON = {"you", "he", "she", "it", "we", "they", "this", "that", "these", "those", "my", "your",
           "his", "her", "our", "their", "there", "here", "what", "when", "where", "why", "how",
           "who", "if", "but", "and", "or", "so", "then", "now", "yes", "no", "please", "a", "an", "the"}
# Placeholders as the model may return them; the 1.3B model sometimes writes ZZ as Thai "ซีซี"
_PLACEHOLDER_OUT = re.compile(r"( ?)(?:ZZ|\u0e0b\u0e35\u0e0b\u0e35|\u0e0b\u0e0b)\s*(\d+)( ?)")

# Brand / site names that are ordinary words in lowercase ("discord" = ความแตกแยก).
# Lowest priority: imported and hand-made terms override them.
DEFAULT_TERMS = {
    "discord": "Discord", "youtube": "YouTube", "twitch": "Twitch", "reddit": "Reddit",
    "twitter": "Twitter", "github": "GitHub", "patreon": "Patreon", "curseforge": "CurseForge",
    "modrinth": "Modrinth", "wiki": "Wiki", "minecraft": "Minecraft",
}


def _is_thai(ch: str) -> bool:
    return bool(ch) and "\u0e00" <= ch <= "\u0e7f"


def _is_cap(word: str) -> bool:
    return word[0].isupper() and word != "I" and not re.fullmatch(r"ZZ\d+", word)


def _only_space(text: str, a: re.Match, b: re.Match) -> bool:
    return text[a.end():b.start()].isspace()


def find_names(text: str, is_known: Callable[[str], bool]) -> list[tuple[int, int]]:
    """Spans of likely proper names (people, items, places) that should stay in English.

    Rules:
    - "By <name>" lines (author credits): the whole name.
    - Runs of Capitalized words in normal sentences, e.g. "Ancient Debris", "Jar of Voiding".
    - The first word of a sentence is only part of a name if it is not a dictionary word
      ("Testing in the..." is just a normal first word; "Lyrellion said" is a name).
    - A lone capitalized dictionary word ("English", "Thai") is translated normally; known
      game words like "Nether" are covered by the glossary instead.
    - Titles / menu labels where most words are capitalized ("Copy Spell to Clipboard") are
      left to the translator, except words the dictionary does not know.
    """
    author = _AUTHOR.match(text)
    if author and len(author.group(1).split()) <= 5:
        return [author.span(1)]

    words = [m for m in _TOKEN.finditer(text) if not re.fullmatch(r"ZZ\d+", m.group())]
    if not words:
        return []
    def known(word: str) -> bool:
        return word.lower() in _COMMON or is_known(word)

    # A short line with no sentence punctuation and mostly Capitalized words is a title/label
    long_words = [w.group() for w in words if len(w.group()) > 3]
    title_mode = (len(words) <= 8 and not re.search(r"[.!?,;]", text) and bool(long_words)
                  and sum(w[0].isupper() for w in long_words) / len(long_words) >= 0.6)
    if title_mode:
        return [w.span() for w in words if _is_cap(w.group()) and not known(w.group())]

    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(words):
        if not _is_cap(words[i].group()):
            i += 1
            continue
        j = i
        # Extend over "Word Word" and "Word of Word"
        while j + 1 < len(words):
            nxt = words[j + 1].group()
            if not _only_space(text, words[j], words[j + 1]):
                break                      # "Translate, Review" are separate items, not one name
            if _is_cap(nxt):
                j += 1
            elif (nxt.lower() in _CONNECTORS and j + 2 < len(words) and _is_cap(words[j + 2].group())
                  and _only_space(text, words[j + 1], words[j + 2])):
                j += 2
            else:
                break
        first = i
        at_start = words[i].start() == len(text) - len(text.lstrip(" \"'“‘([-"))
        if at_start and known(words[i].group()):
            first += 1                 # "Select English ..." -> the first word is just capitalized
        if first == j and known(words[j].group()):
            pass                       # lone everyday word: "English", "Thai", "Monday"
        elif first <= j:
            spans.append((words[first].start(), words[j].end()))
        i = j + 1
    return spans


class Glossary:
    def __init__(self, folder: Path, imported_folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        # Terms imported from game files live apart from hand-made ones: they are large,
        # regenerated on every import, and are the game's text (kept out of the git repo)
        self.imported_folder = imported_folder
        self.imported_folder.mkdir(parents=True, exist_ok=True)
        self.name: str | None = None
        self.manual: dict[str, str] = {}
        self.imported: dict[str, str] = {}
        self.imported_info: dict = {}
        self.terms: dict[str, str] = {}      # combined; hand-made terms win over imported ones
        self._pattern: re.Pattern | None = None

    def profiles(self) -> list[str]:
        return sorted(p.stem for p in self.folder.glob("*.json"))

    @staticmethod
    def _safe(name: str) -> str:
        # Profile names come from the UI; keep them to safe file names
        return re.sub(r"[^\w\- ]", "", name).strip() or "game"

    def _path(self, name: str) -> Path:
        return self.folder / f"{self._safe(name)}.json"

    def _imported_path(self, name: str) -> Path:
        return self.imported_folder / f"{self._safe(name)}.json"

    @staticmethod
    def _clean(terms: dict[str, str]) -> dict[str, str]:
        return {k.lower().strip(): v.strip().translate(THAI_DIGITS)
                for k, v in terms.items() if k.strip() and v.strip()}

    def load(self, name: str | None):
        self.name, self.manual, self.imported, self.imported_info = name, {}, {}, {}
        if name and self._path(name).exists():
            data = json.loads(self._path(name).read_text(encoding="utf-8"))
            self.manual = self._clean(data.get("terms", {}))
        if name and self._imported_path(name).exists():
            data = json.loads(self._imported_path(name).read_text(encoding="utf-8"))
            self.imported = self._clean(data.get("terms", {}))
            self.imported_info = data.get("info", {})
        self._compile()

    def save(self, terms: dict[str, str], name: str | None = None):
        """Save the hand-made terms of the current (or a new) profile."""
        if name and name != self.name:
            self.load(name)                       # switching/creating: pick up its imports
        self.manual = self._clean(terms)
        self._path(self.name).write_text(
            json.dumps({"name": self.name, "terms": dict(sorted(self.manual.items()))}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        self._compile()

    def set_imported(self, terms: dict[str, str], info: dict):
        """Replace the imported terms of the current profile (empty dict clears them)."""
        self.imported, self.imported_info = self._clean(terms), info
        path = self._imported_path(self.name)
        if self.imported:
            path.write_text(json.dumps({"info": info, "terms": self.imported}, ensure_ascii=False),
                            encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        self._compile()

    def _compile(self):
        self.terms = {**DEFAULT_TERMS, **self.imported, **self.manual}
        if not self.terms:
            self._pattern = None
            return
        # Longest terms first so "charged creeper" wins over "creeper"; allow plural -s/-es
        alternatives = "|".join(re.escape(t) for t in sorted(self.terms, key=len, reverse=True))
        self._pattern = re.compile(rf"\b({alternatives})(?:e?s)?\b", re.IGNORECASE)

    def protect(self, text: str, is_known: Callable[[str], bool] | None = None) -> tuple[str, dict[str, str]]:
        """Swap game terms (and, if is_known is given, proper names) for placeholders
        (ZZ1, ZZ2...) that the model copies unchanged.

        Feeding the Thai term straight to the model is unreliable: it may "complete" it
        into another word (ต้นกล้า -> ต้นกล้ามเนื้อ). Placeholders survive intact.
        """
        mapping: dict[str, str] = {}

        def put(value: str) -> str:
            key = f"ZZ{len(mapping) + 1}"
            mapping[key] = value
            return key

        # Names first, so a name is never split by a glossary word inside it:
        # "Spell Compendium" stays whole instead of becoming "เวท Compendium".
        # A name that is itself a glossary term ("Charged Creeper") takes the glossary value.
        if is_known is not None:
            # Replace from the end so earlier spans keep their positions
            for start, end in reversed(find_names(text, is_known)):
                name = text[start:end]
                text = text[:start] + put(self.exact(name) or name) + text[end:]
        if self._pattern is not None:
            text = self._pattern.sub(lambda m: put(self.terms[m.group(1).lower()]), text)
        return text, mapping

    @staticmethod
    def restore(text: str, mapping: dict[str, str]) -> str:
        """Put the fixed translations back where the placeholders ended up."""
        if not mapping:
            return text

        def put_back(m: re.Match) -> str:
            value = mapping.get(f"ZZ{m.group(2)}")
            if value is None:
                return m.group()
            # Thai has no spaces between words: drop the spaces the model put around a
            # Thai term when its neighbour is Thai too ("ของ เวท ที่" -> "ของเวทที่")
            before, after = m.group(1), m.group(3)
            prev_char = text[m.start() - 1] if m.start() > 0 else ""
            next_char = text[m.end()] if m.end() < len(text) else ""
            if _is_thai(value[:1]) and _is_thai(prev_char):
                before = ""
            if _is_thai(value[-1:]) and _is_thai(next_char):
                after = ""
            return before + value + after

        return _PLACEHOLDER_OUT.sub(put_back, text)

    def terms_in(self, text: str) -> list[tuple[str, str]]:
        """(term as written, fixed translation) for every glossary term in the text."""
        if self._pattern is None:
            return []
        found: dict[str, str] = {}
        for m in self._pattern.finditer(text):
            found.setdefault(m.group(1), self.terms[m.group(1).lower()])
        return list(found.items())

    def exact(self, text: str) -> str | None:
        """Fixed translation if the whole text is one glossary term (e.g. a menu label)."""
        key = text.lower().strip(" .:!?")
        return self.terms.get(key) or self.terms.get(re.sub(r"e?s$", "", key))
