# window-translator

Offline English → Thai screen translator for games, with built-in vocabulary learning.
Select English text in any window, get a Thai translation, click words to see dictionary
meanings, and review the words you looked up with spaced-repetition flashcards.

Everything runs locally on the CPU — no internet connection or API key is needed after setup.

## Features
- **One window** (best placed on a second monitor) with tabs: Translate, Review, Vocabulary, Game glossary
- **Target window picker**: lock onto the game window; the selected area follows the window when it moves
- **OCR** with the engine built into Windows (`Windows.Media.Ocr`)
- **Offline translation** with NLLB-200 (600M fast, or 1.3B more fluent), via CTranslate2 int8
- **Typhoon Translate 1.5** (default engine, [model card](https://huggingface.co/scb10x/typhoon-translate1.5-4b)): Thai-specialised 4B model run by llama.cpp on 127.0.0.1, streamed word by word; game terms are sent to it as rules. Runs on the CPU (thread count adjustable, below-normal priority so the game comes first) or on the graphics card via Vulkan (~3 GB free VRAM). NLLB stays available as a fast mode
- **Click any word** for meanings, part of speech, synonyms and phrasal verbs (LEXiTRON dictionary)
- **Game glossary** per game: fixed translations for game terms (`chest = หีบ`, `creeper = Creeper`)
- **Proper names kept in English automatically** (e.g. `Ancient Debris`, `Pan Et Rosa`)
- **Flashcards** (simplified SM-2) built from every word you looked up, with the sentence you met it in
- The app window is excluded from screen capture, so it never ends up in its own OCR

## Requirements
- Windows 10 (2004+) or 11, with the English language pack (for OCR)
- Python 3.11+
- ~2 GB disk (NLLB 600M only); ~4.7 GB with Typhoon; 4 GB+ free RAM (Typhoon on CPU needs ~3 GB more)

## Setup (once)
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python download_assets.py            # add --large (NLLB 1.3B) and/or --typhoon (Typhoon + llama.cpp, ~2.6 GB)
```
`download_assets.py` downloads the NLLB model from Hugging Face and the LEXiTRON dictionary
from [tlwg/dict-lexitron](https://github.com/tlwg/dict-lexitron), then builds the dictionary database.

## Use
Run `run.bat`. If the translator shares a monitor with the game, run the game in
**Borderless** or **Windowed** mode and use 📌 to keep the translator on top.

| Hotkey | Action |
|---|---|
| Ctrl+Shift+T | Drag to select a text area |
| Ctrl+Shift+R | Scan the last area again (dialogue boxes) |
| Ctrl+Shift+H | Minimize / restore the translator window |

Review shortcuts: `Space` reveal, `1` again, `2` hard, `3` good, `4` easy.

## Project layout
```
app/main.py         main window, OCR -> translate pipeline
app/ui.py           widgets: selection overlay, translate / review / vocabulary / glossary tabs
app/translator.py   NLLB via CTranslate2 (+ Thai normalization, loop detection)
app/typhoon.py      Typhoon Translate via a local llama-server process
app/gamelang.py     import item/mob names from Minecraft and mod language files
app/glossary.py     per-game glossary and automatic proper-name protection
app/dictionary.py   LEXiTRON lookup and simple lemmatizer
app/vocab.py        vocabulary store and spaced-repetition scheduling
app/ocr.py          Windows OCR wrapper
app/winapi.py       window list, window/monitor geometry, capture exclusion
app/hotkeys.py      global hotkeys (RegisterHotKey)
glossary/*.json     game glossaries (editable in the app)
```

## Licenses of downloaded data
Models and dictionary data are **not** included in this repository.
- NLLB-200 (Meta): CC-BY-NC 4.0 — non-commercial use only
- Typhoon Translate 1.5 (SCB 10X): see its model card; the card lists both Gemma and Apache-2.0, check before redistribution
- LEXiTRON (NECTEC): © NECTEC, see the license in the downloaded `data/_lexitron_src/LICENSE`
