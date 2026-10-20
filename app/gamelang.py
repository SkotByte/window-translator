"""Import game-term names from Minecraft (and mod) language files.

Minecraft keeps every displayed name in per-language files:
  - vanilla English:  client .jar  -> assets/minecraft/lang/en_us.json
  - vanilla Thai:     downloaded asset objects (assets/indexes/*.json -> objects/<hash>)
  - mods:             mods/*.jar   -> assets/<modid>/lang/en_us.json (sometimes th_th.json)
  - 1.12 and older:   same places, but "key=value" .lang files

Names that have an official Thai version become "English -> Thai" glossary terms.
Names without Thai (most mods) are kept in English when they look like proper names.

Security: archives are only read in memory (no extraction), only lang files are opened,
and each file is size-capped. Downloads use Mojang's official HTTPS endpoints only.
"""
import json
import re
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Callable

MAX_LANG_BYTES = 20 * 1024 * 1024      # refuse absurdly large "lang" files
LANG_RE = re.compile(r"^assets/([^/]+)/lang/(en_us|th_th)\.(json|lang)$", re.IGNORECASE)
NAME_KEY_RE = re.compile(
    r"^(block|item|entity|biome|effect|enchantment|tile|fluid|painting|instrument)\.", re.IGNORECASE)
SKIP_KEY_RE = re.compile(r"(desc|tooltip|lore|info|message|subtitle|jei|guide|book|page|chat)", re.IGNORECASE)
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES_URL = "https://resources.download.minecraft.net/{}/{}"
ALLOWED_HOSTS = ("piston-meta.mojang.com", "piston-data.mojang.com", "resources.download.minecraft.net",
                 "launchermeta.mojang.com", "launcher.mojang.com")

Lang = dict[str, dict[str, str]]          # {"en_us": {key: text}, "th_th": {key: text}}


def _parse(data: bytes, fmt: str) -> dict[str, str]:
    text = data.decode("utf-8-sig", errors="replace")
    if fmt.lower() == "json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return {k: v for k, v in obj.items() if isinstance(k, str) and isinstance(v, str)}
    out = {}
    for line in text.splitlines():           # legacy "key=value" format
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _merge(into: Lang, other: Lang):
    for lang, entries in other.items():
        into.setdefault(lang, {}).update(entries)


def read_archive(source) -> Lang:
    """Read en_us/th_th lang files from a .jar/.zip (path or bytes)."""
    result: Lang = {}
    try:
        zf = zipfile.ZipFile(BytesIO(source) if isinstance(source, bytes) else source)
    except (zipfile.BadZipFile, OSError):
        return result
    with zf:
        for info in zf.infolist():
            m = LANG_RE.match(info.filename)
            if not m or info.file_size > MAX_LANG_BYTES:
                continue
            _merge(result, {m.group(2).lower(): _parse(zf.read(info), m.group(3))})
    return result


def _read_asset_thai(game_dir: Path) -> dict[str, str]:
    """Vanilla Thai lives in hashed asset objects; find it through the asset indexes."""
    thai: dict[str, str] = {}
    for index in sorted((game_dir / "assets" / "indexes").glob("*.json")):
        try:
            obj = json.loads(index.read_text(encoding="utf-8")).get("objects", {})
        except (OSError, json.JSONDecodeError):
            continue
        for name, fmt in (("minecraft/lang/th_th.json", "json"), ("minecraft/lang/th_th.lang", "lang")):
            entry = obj.get(name)
            if not entry:
                continue
            h = entry["hash"]
            f = game_dir / "assets" / "objects" / h[:2] / h
            if f.exists() and f.stat().st_size <= MAX_LANG_BYTES:
                thai.update(_parse(f.read_bytes(), fmt))
    return thai


def scan_game_folder(game_dir: Path, progress: Callable[[str], None] = print) -> Lang:
    """Scan a .minecraft folder or a launcher instance folder (CurseForge, Prism, ...)."""
    result: Lang = {}
    jars = sorted((game_dir / "mods").glob("*.jar"))
    versions = sorted((game_dir / "versions").glob("*/*.jar"))
    # Instance folders keep the vanilla jar elsewhere; only use the newest version jar found
    if versions:
        jars.insert(0, versions[-1])
    for i, jar in enumerate(jars, 1):
        progress(f"อ่านไฟล์ {i}/{len(jars)}: {jar.name}")
        _merge(result, read_archive(jar))
    thai = _read_asset_thai(game_dir)
    if thai:
        result.setdefault("th_th", {}).update(thai)
    return result


def scan_files(paths: list[Path], progress: Callable[[str], None] = print) -> Lang:
    result: Lang = {}
    for i, p in enumerate(paths, 1):
        progress(f"อ่านไฟล์ {i}/{len(paths)}: {p.name}")
        _merge(result, read_archive(p))
    return result


def _fetch(url: str) -> bytes:
    if not url.startswith("https://") or url.split("/")[2] not in ALLOWED_HOSTS:
        raise ValueError(f"Blocked download from untrusted host: {url}")
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def download_vanilla(version: str = "latest", progress: Callable[[str], None] = print) -> Lang:
    """Download official vanilla English + Thai names from Mojang (no game install needed)."""
    progress("กำลังโหลดรายชื่อเวอร์ชันจาก Mojang...")
    manifest = json.loads(_fetch(MANIFEST_URL))
    if version in ("", "latest"):
        version = manifest["latest"]["release"]
    match = next((v for v in manifest["versions"] if v["id"] == version), None)
    if match is None:
        raise ValueError(f"ไม่พบ Minecraft เวอร์ชัน {version}")
    meta = json.loads(_fetch(match["url"]))
    progress(f"กำลังโหลดไฟล์ภาษาอังกฤษของ {version} (~25 MB)...")
    result = read_archive(_fetch(meta["downloads"]["client"]["url"]))
    progress("กำลังโหลดไฟล์ภาษาไทย...")
    index = json.loads(_fetch(meta["assetIndex"]["url"]))["objects"]
    entry = index.get("minecraft/lang/th_th.json")
    if entry:
        h = entry["hash"]
        result.setdefault("th_th", {}).update(_parse(_fetch(RESOURCES_URL.format(h[:2], h)), "json"))
    return result


def build_terms(lang: Lang, is_known: Callable[[str], bool]) -> tuple[dict[str, str], dict[str, int]]:
    """Turn lang data into glossary terms {english lower: target} plus counts for the UI."""
    en, th = lang.get("en_us", {}), lang.get("th_th", {})
    terms: dict[str, str] = {}
    stats = {"names": 0, "thai": 0, "english": 0, "skipped": 0}
    for key, name in en.items():
        if not NAME_KEY_RE.match(key) or SKIP_KEY_RE.search(key):
            continue
        name = re.sub(r"§.", "", name).strip()         # strip Minecraft color codes
        words = name.split()
        if not (1 <= len(words) <= 5) or not 3 <= len(name) <= 40 or re.search(r"[%{}\[\]<>]|\d", name):
            continue
        if re.fullmatch(r"[IVXLCDM]+", name):      # enchantment levels "I", "II"... would match "is"
            continue
        stats["names"] += 1
        thai = re.sub(r"§.", "", th.get(key, "")).strip()
        if len(words) == 1 and is_known(name):
            # A single everyday word ("Target", "Stone", "Source") is also an item name, but
            # forcing the item name everywhere breaks normal sentences ("the target" ->
            # "เป้ายิง", the Target block). Leave those to the translator.
            stats["skipped"] += 1
        elif thai and thai != name:
            terms.setdefault(name.lower(), thai)
            stats["thai"] += 1
        else:
            terms.setdefault(name.lower(), name)    # no Thai name: keep the English name
            stats["english"] += 1
    return terms, stats
