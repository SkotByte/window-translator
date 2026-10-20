"""Text recognition using the OCR engine built into Windows 10/11 (no download needed)."""
import asyncio

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
