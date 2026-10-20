"""Paths and settings shared by the whole app."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
# Translation models (CTranslate2 int8). Label shown in the UI -> folder
MODELS = {
    "NLLB 600M (เร็ว)": ROOT / "models" / "nllb-600m-int8",
    "NLLB 1.3B (แม่นกว่า ช้ากว่า)": ROOT / "models" / "nllb-1.3b-int8",
}
DEFAULT_MODEL = "NLLB 600M (เร็ว)"
MODEL_DIR = MODELS[DEFAULT_MODEL]

LEXITRON_SRC = DATA_DIR / "_lexitron_src" / "etlex.utf-8"
DICT_DB = DATA_DIR / "lexitron.db"
VOCAB_DB = DATA_DIR / "vocab.db"
GLOSSARY_DIR = ROOT / "glossary"

# NLLB language codes
SRC_LANG = "eng_Latn"
TGT_LANG = "tha_Thai"

# CPU threads for translation; keep low so the game keeps most of the CPU
TRANSLATE_THREADS = 4

# Small game text is hard for OCR; upscale crops narrower than this (pixels)
OCR_UPSCALE_BELOW = 1200

# Global hotkeys: (modifiers, key). Modifiers: "ctrl", "shift", "alt", "win"
HOTKEYS = {
    "select": (("ctrl", "shift"), "T"),   # drag to select a text area
    "rescan": (("ctrl", "shift"), "R"),   # scan the last area again
    "toggle": (("ctrl", "shift"), "H"),   # hide/show the result popup
}
