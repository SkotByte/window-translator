"""Higher-quality translation with Typhoon Translate 1.5 (SCB 10X), run locally by llama.cpp.

NLLB answers in ~1 s but translates word by word; Typhoon understands context and game
terms given as rules, but needs ~2-12 s per sentence on CPU. The app shows NLLB first and
replaces each sentence with Typhoon's version when it is ready.

The model runs in a llama-server child process that listens on 127.0.0.1 only.
"""
import atexit
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from .translator import normalize_thai

CREATE_NO_WINDOW = 0x08000000   # do not flash a console window on Windows
# Windows gives the game the CPU first; translation only uses what is left over
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
MAX_RULES = 30


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_prompt(text: str, game: str | None, terms: list[tuple[str, str]]) -> str:
    """Prompt in the format from the model card: instruction, numbered rules, source text."""
    context = f"the video game {game}" if game else "a video game"
    rules = [f"This text is from {context}. Use natural, casual Thai that a gamer would use.",
             "Keep names of spells, items, mobs, places, players and brands in English.",
             "Use Arabic digits (0-9), never Thai digits."]
    for en, target in terms[:MAX_RULES]:
        if target.lower() == en.lower():
            rules.append(f'Keep "{target}" in English.')
        else:
            rules.append(f'Translate "{en}" as "{target}".')
    numbered = "\n".join(f"{i}.  {r}" for i, r in enumerate(rules, 1))
    return ("Translate the following English text into Thai, strictly following the rules below "
            f"and return only the translated text.\nRules:\n{numbered}\nSource Text:\n{text}")


class TyphoonServer:
    def __init__(self, servers: dict[str, Path], model: Path, threads: int = 4, device: str = "cpu"):
        """servers: {"cpu": llama-server.exe, "gpu": Vulkan llama-server.exe}."""
        self.servers, self.model, self.threads, self.device = servers, model, threads, device
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.ready = False           # True once the model is loaded and answering
        atexit.register(self.stop)

    @property
    def available(self) -> bool:
        return self.servers["cpu"].exists() and self.model.exists()

    def device_available(self, device: str) -> bool:
        return self.servers.get(device, Path()).exists()

    @property
    def running(self) -> bool:
        return self.ready and self.process is not None and self.process.poll() is None

    def start(self, timeout: float = 180):
        """Start llama-server and wait until the model is loaded (blocking; call off the UI thread)."""
        if self.running:
            return
        self.stop()
        self.port = _free_port()
        args = [str(self.servers[self.device]), "-m", str(self.model), "--host", "127.0.0.1",
                "--port", str(self.port), "-c", "4096", "-t", str(self.threads)]
        if self.device == "gpu":
            args += ["-ngl", "99"]            # whole model on the graphics card (~3 GB VRAM)
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                if self.device == "gpu":
                    raise RuntimeError("รันบนการ์ดจอไม่ได้ (VRAM ไม่พอ ต้องว่างประมาณ 3 GB) — เปลี่ยน 'รันบน' เป็น CPU")
                raise RuntimeError("llama-server หยุดทำงาน (RAM ไม่พอหรือไฟล์โมเดลเสีย) — เลือกโมเดล NLLB แทนได้")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2)
                self.ready = True
                return
            except OSError:
                time.sleep(0.5)
        self.stop()
        raise RuntimeError("โหลด Typhoon ไม่ทันเวลา")

    def stop(self):
        self.ready = False
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def _request(self, text: str, game: str | None, terms: list[tuple[str, str]],
                 temperature: float, stream: bool) -> urllib.request.Request:
        body = json.dumps({
            "messages": [{"role": "user", "content": build_prompt(text, game, terms)}],
            "temperature": temperature,
            "max_tokens": 512,
            "stream": stream,
        }).encode()
        return urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", body,
                                      {"Content-Type": "application/json"})

    def translate(self, text: str, game: str | None, terms: list[tuple[str, str]],
                  temperature: float = 0.2) -> str:
        """Whole translation at once. The model card recommends a low temperature;
        a higher one gives different wordings (used for "other translations")."""
        with urllib.request.urlopen(self._request(text, game, terms, temperature, False), timeout=120) as r:
            data = json.loads(r.read())
        return normalize_thai(data["choices"][0]["message"]["content"])

    def translate_stream(self, text: str, game: str | None, terms: list[tuple[str, str]]):
        """Yield the translation so far, growing as the model writes it (server-sent events).
        Stopping the iteration closes the connection, which makes the server stop generating."""
        parts: list[str] = []
        with urllib.request.urlopen(self._request(text, game, terms, 0.2, True), timeout=120) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                delta = json.loads(payload)["choices"][0].get("delta", {}).get("content")
                if delta:
                    parts.append(delta)
                    yield normalize_thai("".join(parts))
