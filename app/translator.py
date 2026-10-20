"""Offline English -> Thai sentence translation with NLLB-200 (CTranslate2, int8, CPU)."""
import re
from pathlib import Path

import ctranslate2
import sentencepiece as spm

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])")


# Thai digits and Lao digits (the model sometimes writes those too) -> 0-9
THAI_DIGITS = str.maketrans("\u0e50\u0e51\u0e52\u0e53\u0e54\u0e55\u0e56\u0e57\u0e58\u0e59"
                            "\u0ed0\u0ed1\u0ed2\u0ed3\u0ed4\u0ed5\u0ed6\u0ed7\u0ed8\u0ed9", "0123456789" * 2)


def normalize_thai(text: str) -> str:
    """NLLB writes sara am as nikhahit + sara aa; join them into the real sara am.
    Also drops "\u2047", which sentencepiece prints for characters the model does not know,
    and turns Thai digits into normal ones."""
    text = text.replace("\u0e4d\u0e32", "\u0e33").translate(THAI_DIGITS)
    return re.sub(r"\s*\u2047\s*", " ", text).strip()


def normalize_source(text: str) -> str:
    """Clean English before translation: the model has no token for em/en dashes."""
    return re.sub(r"\s*[—–]\s*", " - ", text)


_REPEAT = re.compile(r"(.{1,8}?)\1{4,}")


def is_degenerate(text: str) -> bool:
    """True if the output is stuck in a loop, e.g. the same syllable 5+ times in a row."""
    return _REPEAT.search(text) is not None


# Scripts of neighbouring languages the model sometimes slips into (Lao, Myanmar, Khmer)
_FOREIGN = re.compile("[\u0e80-\u0eff\u1000-\u109f\u1780-\u17ff]")


def has_foreign_script(text: str) -> bool:
    return _FOREIGN.search(text) is not None


def needs_translation(text: str) -> bool:
    """False for numbers, version strings, symbols, etc. that contain no English word."""
    return re.search(r"[A-Za-z]{2,}", text) is not None


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


class Translator:
    def __init__(self, model_dir: Path, src_lang: str, tgt_lang: str, threads: int = 4):
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.model = ctranslate2.Translator(
            str(model_dir), device="cpu", compute_type="int8", intra_threads=threads)
        self.sp = spm.SentencePieceProcessor(model_file=str(model_dir / "sentencepiece.bpe.model"))
        self._cache: dict[str, str] = {}

    def translate_sentences(self, sentences: list[str], alternatives: int = 1) -> list[list[str]]:
        """Translate each sentence; returns up to `alternatives` distinct versions per sentence."""
        todo = [s for s in sentences if alternatives > 1 or s not in self._cache]
        results = {}
        if todo:
            results = self._run(todo, alternatives)
            # Rarely the model gets stuck repeating one word (ขี้ขี้ขี้...). Re-run only those
            # sentences with a repetition penalty; it is not used always because it makes
            # the model drop parts of long sentences.
            stuck = [s for s in todo if is_degenerate(results[s][0])]
            if stuck:
                results.update(self._run(stuck, alternatives, repetition_penalty=1.2,
                                         no_repeat_ngram_size=4))
            # Sometimes a word comes out in Burmese/Lao script; pick another candidate instead
            for s in [s for s in todo if has_foreign_script(results[s][0])]:
                candidates = self._run([s], 6)[s]
                clean = [c for c in candidates if not has_foreign_script(c)]
                if clean:
                    results[s] = clean[:max(1, alternatives)]
            for s in todo:
                self._cache[s] = results[s][0]
        return [results.get(s) or [self._cache[s]] for s in sentences]

    def _run(self, sentences: list[str], alternatives: int, **options) -> dict[str, list[str]]:
        tokens = [[self.src_lang] + self.sp.encode(normalize_source(s), out_type=str) + ["</s>"]
                  for s in sentences]
        out = self.model.translate_batch(
            tokens,
            target_prefix=[[self.tgt_lang]] * len(sentences),
            beam_size=max(4, alternatives),
            num_hypotheses=alternatives,
            max_decoding_length=256,
            # A mild penalty helps the model finish long sentences instead of trailing off
            **{"repetition_penalty": 1.1, **options},
        )
        results = {}
        for s, r in zip(sentences, out):
            versions = []
            for hyp in r.hypotheses:
                t = normalize_thai(self.sp.decode(hyp[1:]))   # drop the target language token
                if t not in versions:
                    versions.append(t)
            results[s] = versions
        return results

    def translate(self, text: str) -> list[tuple[str, str]]:
        """Translate a block of text; returns [(english sentence, thai sentence), ...]."""
        sentences = split_sentences(text)
        return [(s, t[0]) for s, t in zip(sentences, self.translate_sentences(sentences))]
