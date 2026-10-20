"""Text recognition using the OCR engine built into Windows 10/11 (no download needed)."""
import asyncio
import re
from typing import Callable

from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter


class OcrError(RuntimeError):
    pass


class WindowsOcr:
    def __init__(self, lang: str = "en-US"):
        self.engine = OcrEngine.try_create_from_language(Language(lang))
        if self.engine is None:
            raise OcrError(f"Windows OCR language '{lang}' is not installed "
                           "(Settings > Time & language > Language > add English)")

    @property
    def max_dimension(self) -> int:
        return OcrEngine.max_image_dimension

    def recognize(self, bgra: bytes, width: int, height: int) -> list[str]:
        """Run OCR on a raw BGRA8 image; returns recognized lines of text."""
        writer = DataWriter()
        writer.write_bytes(bgra)
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.BGRA8, width, height)
        # WinRT calls are async; each call runs in its own short-lived event loop
        result = asyncio.run(self._recognize_async(bitmap))
        return [line.text for line in result.lines]

    async def _recognize_async(self, bitmap):
        return await self.engine.recognize_async(bitmap)


# Letter shapes OCR typically confuses; each pair is tried in both directions
_CONFUSIONS = (("rn", "m"), ("cl", "d"), ("vv", "w"), ("li", "h"), ("l", "i"), ("I", "l"),
               ("0", "o"), ("1", "l"), ("5", "s"))
_WORD = re.compile(r"[A-Za-z0-9]{4,}")


def _variants(word: str):
    for a, b in _CONFUSIONS:
        for src, dst in ((a, b), (b, a)):
            start = word.find(src)
            while start != -1:
                yield word[:start] + dst + word[start + len(src):]
                start = word.find(src, start + 1)


def fix_ocr_words(line: str, is_known: Callable[[str], bool]) -> str:
    """Repair typical OCR slips ("leaming" -> "learning") using the dictionary.

    Only lowercase words (names are left alone) that are not dictionary words are
    touched, and only when a single confusable-letter swap turns them into one.
    """
    def fix(m):
        word = m.group()
        if word[0].isupper() or not word[1:].islower() or is_known(word):
            return word
        for candidate in _variants(word):
            if candidate.isalpha() and is_known(candidate):
                return candidate
        return word

    return _WORD.sub(fix, line)
