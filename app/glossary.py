"""Per-game glossaries: fixed translations for game terms (mob names, items, UI words).

Each profile is a JSON file in glossary/:  {"name": "Minecraft", "terms": {"creeper": "Creeper", ...}}
A value in English (e.g. a monster name) keeps the term untranslated; a Thai value forces that wording.
Terms are swapped for placeholders before translation and replaced with the fixed wording after.
"""
import json
import re
from pathlib import Path
from typing import Callable

_TOKEN = re.compile(r"ZZ\d+|[A-Za-z][A-Za-z'’\-]*")
_CONNECTORS = {"of", "the", "et", "de", "du", "la", "le", "von", "van", "and"}
_AUTHOR = re.compile(r"^\s*by\s+(.+?)[\s:.]*$", re.IGNORECASE)
# Capitalized at the start of a sentence but never names (LEXiTRON lacks some pronouns)
_COMMON = {"you", "he", "she", "it", "we", "they", "this", "that", "these", "those", "my", "your",
           "his", "her", "our", "their", "there", "here", "what", "when", "where", "why", "how",
           "who", "if", "but", "and", "or", "so", "then", "now", "yes", "no", "please", "a", "an", "the"}
# Placeholders as the model may return them; the 1.3B model sometimes writes ZZ as Thai "ซีซี"
_PLACEHOLDER_OUT = re.compile(r"(?:ZZ|ซีซี|ซซ)\s*(\d+)")


def _is_cap(word: str) -> bool:
    return word[0].isupper() and word != "I" and not re.fullmatch(r"ZZ\d+", word)


def find_names(text: str, is_known: Callable[[str], bool]) -> list[tuple[int, int]]:
    """Spans of likely proper names (people, items, places) that should stay in English.

    Rules:
    - "By <name>" lines (author credits): the whole name.
    - Runs of Capitalized words in normal sentences, e.g. "Ancient Debris", "Jar of Voiding".
    - A single capitalized word at the start of a sentence only if it is not a dictionary word
      ("Testing in the..." is just a normal first word; "Lyrellion said" is a name).
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
            if _is_cap(nxt):
                j += 1
            elif nxt.lower() in _CONNECTORS and j + 2 < len(words) and _is_cap(words[j + 2].group()):
                j += 2
            else:
                break
        at_start = words[i].start() == len(text) - len(text.lstrip(" \"'“‘([-"))
        if not (at_start and i == j and known(words[i].group())):
            spans.append((words[i].start(), words[j].end()))
        i = j + 1
    return spans


class Glossary:
    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.name: str | None = None
        self.terms: dict[str, str] = {}
        self._pattern: re.Pattern | None = None

    def profiles(self) -> list[str]:
        return sorted(p.stem for p in self.folder.glob("*.json"))

    def _path(self, name: str) -> Path:
        # Profile names come from the UI; keep them to safe file names
        safe = re.sub(r"[^\w\- ]", "", name).strip() or "game"
        return self.folder / f"{safe}.json"

    def load(self, name: str | None):
        self.name, self.terms = name, {}
        if name and self._path(name).exists():
            data = json.loads(self._path(name).read_text(encoding="utf-8"))
            self.terms = {k.lower().strip(): v.strip() for k, v in data.get("terms", {}).items() if k.strip()}
        self._compile()

    def save(self, terms: dict[str, str], name: str | None = None):
        self.name = name or self.name
        self.terms = {k.lower().strip(): v.strip() for k, v in terms.items() if k.strip() and v.strip()}
        self._path(self.name).write_text(
            json.dumps({"name": self.name, "terms": dict(sorted(self.terms.items()))}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        self._compile()

    def _compile(self):
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

        if self._pattern is not None:
            text = self._pattern.sub(lambda m: put(self.terms[m.group(1).lower()]), text)
        if is_known is not None:
            # Replace from the end so earlier spans keep their positions
            for start, end in reversed(find_names(text, is_known)):
                text = text[:start] + put(text[start:end]) + text[end:]
        return text, mapping

    @staticmethod
    def restore(text: str, mapping: dict[str, str]) -> str:
        """Put the fixed translations back where the placeholders ended up."""
        if not mapping:
            return text
        return _PLACEHOLDER_OUT.sub(lambda m: mapping.get(f"ZZ{m.group(1)}", m.group()), text)

    def exact(self, text: str) -> str | None:
        """Fixed translation if the whole text is one glossary term (e.g. a menu label)."""
        key = text.lower().strip(" .:!?")
        return self.terms.get(key) or self.terms.get(re.sub(r"e?s$", "", key))
