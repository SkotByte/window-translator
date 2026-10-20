"""One-time setup: download the translation model and dictionary, then build the dictionary DB."""
import io
import subprocess
import sys
import urllib.request
import zipfile

from huggingface_hub import hf_hub_download, snapshot_download

from app import config
from app.dictionary import build

FILES = ["config.json", "model.bin", "sentencepiece.bpe.model", "shared_vocabulary.txt"]
# Model folder -> Hugging Face repo. The 1.3B model is optional: pass --large to get it.
DOWNLOADS = [(config.MODELS["NLLB 600M (เร็ว)"], "JustFrederik/nllb-200-distilled-600M-ct2-int8", "~600 MB")]
if "--large" in sys.argv:
    DOWNLOADS.append((config.MODELS["NLLB 1.3B (แม่นกว่า ช้ากว่า)"],
                      "JustFrederik/nllb-200-distilled-1.3B-ct2-int8", "~1.4 GB"))

for folder, repo, size in DOWNLOADS:
    if not (folder / "model.bin").exists():
        print(f"Downloading {repo} ({size})...")
        snapshot_download(repo, local_dir=folder, allow_patterns=FILES)

# Optional: Typhoon Translate (better quality, ~2.5 GB) + llama.cpp runtime. Pass --typhoon.
LLAMA_CPP_BUILD = "b11433"
if "--typhoon" in sys.argv:
    if not config.TYPHOON_MODEL.exists():
        print("Downloading Typhoon Translate 1.5 4B (~2.5 GB)...")
        hf_hub_download("scb10x/typhoon-translate1.5-4b-gguf", config.TYPHOON_MODEL.name,
                        local_dir=config.TYPHOON_MODEL.parent)
    # CPU build always; Vulkan build lets Typhoon run on the graphics card
    for flavour, exe in (("cpu", config.LLAMA_SERVERS["cpu"]), ("vulkan", config.LLAMA_SERVERS["gpu"])):
        if exe.exists():
            continue
        name = f"llama-{LLAMA_CPP_BUILD}-bin-win-{flavour}-x64.zip"
        print(f"Downloading llama.cpp {LLAMA_CPP_BUILD} ({name})...")
        url = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_BUILD}/{name}"
        with urllib.request.urlopen(url, timeout=300) as r:
            archive = zipfile.ZipFile(io.BytesIO(r.read()))
        target = exe.parent.resolve()
        for member in archive.namelist():
            # Refuse entries that would land outside the target folder (zip slip)
            if not (target / member).resolve().is_relative_to(target):
                raise SystemExit(f"Unsafe path in archive: {member}")
        archive.extractall(target)

if not config.LEXITRON_SRC.exists():
    print("Downloading LEXiTRON dictionary...")
    subprocess.run(["git", "clone", "--depth", "1", "https://github.com/tlwg/dict-lexitron.git",
                    str(config.LEXITRON_SRC.parent)], check=True)

print("Building dictionary DB...")
print(build(config.LEXITRON_SRC, config.DICT_DB), "entries")
print("Done. Start the app with run.bat")
sys.exit(0)
