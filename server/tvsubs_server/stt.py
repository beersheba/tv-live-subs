"""Whisper via whisper.cpp's `whisper-server` (Metal on Apple Silicon).

One server process per model, spawned lazily on a free localhost port and kept resident.
A model spec is either a path to a ggml file or ``<hf repo>::<file>`` (downloaded on demand).
"""
from __future__ import annotations

import atexit
import io
import os
import shutil
import socket
import subprocess
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from huggingface_hub import hf_hub_download

CACHE = Path(os.environ.get("TVSUBS_CACHE", Path.home() / ".cache" / "tvsubs"))

# Hebrew gets the ivrit-ai fine-tune, everything else the stock multilingual model.
DEFAULT_MODELS = {"he": "ivrit-ai/whisper-large-v3-turbo-ggml::ggml-model.bin"}
FALLBACK_MODEL = "ggerganov/whisper.cpp::ggml-large-v3-turbo.bin"
# Fine-tunes like ivrit-ai call everything Hebrew, so language ID needs a stock model; small is enough.
DETECT_MODEL = "ggerganov/whisper.cpp::ggml-small.bin"


def resolve_model(spec: str) -> Path:
    if os.path.isfile(spec):
        return Path(spec)
    repo, _, filename = spec.partition("::")
    if not filename:
        raise ValueError(f"model spec must be a file path or '<repo>::<file>': {spec!r}")
    target = CACHE / repo.replace("/", "__")
    if not (target / filename).is_file():
        print(f"[stt] downloading {spec}", flush=True)
        hf_hub_download(repo, filename, local_dir=target)
    return target / filename


def _wav(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class _Server:
    def __init__(self, model: Path, threads: int, binary: str):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        CACHE.mkdir(parents=True, exist_ok=True)
        self._log = open(CACHE / f"whisper-server-{self.port}.log", "wb")
        self.proc = subprocess.Popen(
            [binary, "-m", str(model), "--host", "127.0.0.1", "--port", str(self.port),
             "-t", str(threads), "-bs", "1", "-nf", "-nt", "-sns"],
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        atexit.register(self.close)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"whisper-server exited with {self.proc.returncode}; see {self._log.name}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.3)
        else:
            raise RuntimeError("whisper-server did not start in time")
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=60)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()


class Transcriber:
    def __init__(self, models: dict[str, str], fallback: str, threads: int,
                 binary: str = "whisper-server", detect_model: str = DETECT_MODEL):
        if shutil.which(binary) is None:
            raise RuntimeError("whisper-server not found; `brew install whisper-cpp`")
        self._models = models
        self._fallback = fallback
        self._threads = threads
        self._binary = binary
        self._detect_model = detect_model
        self._servers: dict[str, _Server] = {}
        self._lock = threading.Lock()

    def preload(self, lang: str) -> None:
        self._get(lang)

    def _get(self, lang: str) -> _Server:
        return self._server(self._models.get(lang, self._fallback))

    def _server(self, spec: str) -> _Server:
        with self._lock:
            if spec not in self._servers:
                print(f"[stt] starting whisper-server for {spec}", flush=True)
                self._servers[spec] = _Server(resolve_model(spec), self._threads, self._binary)
            return self._servers[spec]

    def preload_detector(self) -> None:
        self._server(self._detect_model)

    def detect(self, audio: np.ndarray) -> tuple[str, float]:
        """Most likely spoken language (ISO 639-1) and its probability."""
        r = self._server(self._detect_model).client.post(
            "/inference",
            files={"file": ("a.wav", _wav(audio), "audio/wav")},
            data={"language": "auto", "response_format": "verbose_json", "temperature": "0.0"},
        )
        r.raise_for_status()
        probs = r.json().get("language_probabilities") or {}
        if not probs:
            return "", 0.0
        lang = max(probs, key=probs.get)
        return lang, float(probs[lang])

    def transcribe(self, audio: np.ndarray, lang: str, final: bool, prompt: str = "") -> str:
        r = self._get(lang).client.post(
            "/inference",
            files={"file": ("a.wav", _wav(audio), "audio/wav")},
            data={"language": lang, "response_format": "json", "temperature": "0.0",
                  **({"prompt": prompt} if prompt else {})},
        )
        r.raise_for_status()
        return r.json().get("text", "").strip()
