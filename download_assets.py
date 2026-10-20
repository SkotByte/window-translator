"""One-time setup: download the translation model and dictionary, then build the dictionary DB."""
import subprocess
import sys

from huggingface_hub import snapshot_download

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

if not config.LEXITRON_SRC.exists():
    print("Downloading LEXiTRON dictionary...")
    subprocess.run(["git", "clone", "--depth", "1", "https://github.com/tlwg/dict-lexitron.git",
                    str(config.LEXITRON_SRC.parent)], check=True)

print("Building dictionary DB...")
print(build(config.LEXITRON_SRC, config.DICT_DB), "entries")
print("Done. Start the app with run.bat")
sys.exit(0)
