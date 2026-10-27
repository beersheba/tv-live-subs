"""NLLB-200 translation through CTranslate2."""
from __future__ import annotations

import os
import re
import threading
import time

import ctranslate2
import httpx
from huggingface_hub import snapshot_download
from transformers import AutoTokenizer

from .langs import nllb

_SENT_END = re.compile(r"(?<=[.!?…])\s+")
CACHE_MAX = 512
BLOCK_SECONDS = 15.0

TOKENIZER = "facebook/nllb-200-distilled-600M"
CT2_REPO = "JustFrederik/nllb-200-distilled-600M-ct2-int8"


class NllbTranslator:
    def __init__(self, ct2_repo: str, compute_type: str, threads: int):
        print(f"[mt] loading {ct2_repo}", flush=True)
        path = snapshot_download(ct2_repo)
        self._tr = ctranslate2.Translator(
            path, device="cpu", compute_type=compute_type, inter_threads=1, intra_threads=threads,
        )
        self._tok = AutoTokenizer.from_pretrained(TOKENIZER)
        self._lock = threading.Lock()
        # Partials re-send the same leading sentences over and over: translate each only once.
        self._cache: dict[tuple[str, str, str], str] = {}

    def translate(self, text: str, src: str, dst: str, context: tuple[str, ...] = ()) -> str:
        if not text or src == dst:
            return text
        sentences = [s for s in _SENT_END.split(text.strip()) if s]
        with self._lock:
            todo = [s for s in dict.fromkeys(sentences) if (src, dst, s) not in self._cache]
            if todo:
                self._tok.src_lang = nllb(src)
                batch = [self._tok.convert_ids_to_tokens(self._tok.encode(s)) for s in todo]
                out = self._tr.translate_batch(
                    batch, target_prefix=[[nllb(dst)]] * len(batch), beam_size=1, max_decoding_length=160,
                )
                if len(self._cache) > CACHE_MAX:
                    self._cache.clear()
                for s, r in zip(todo, out):
                    ids = self._tok.convert_tokens_to_ids(r.hypotheses[0][1:])  # drop the language token
                    self._cache[(src, dst, s)] = self._tok.decode(ids, skip_special_tokens=True)
            return " ".join(self._cache[(src, dst, s)] for s in sentences)


class GeminiTranslator:
    """Gemini via REST: context-aware, glossary-aware, falls back to NLLB on any failure."""

    LANG_NAMES = {"he": "Hebrew", "ru": "Russian", "en": "English", "ar": "Arabic", "uk": "Ukrainian",
                  "fr": "French", "de": "German", "es": "Spanish", "it": "Italian",
                  "pt": "Portuguese", "tr": "Turkish", "pl": "Polish", "fa": "Persian", "am": "Amharic"}

    def __init__(self, api_key: str, model: str, fallback: NllbTranslator, glossary: dict[str, str],
                 thinking: str = "off", timeout: float = 4.0):
        self._fallback = fallback
        self._glossary = glossary
        self._model = model
        self._thinking = thinking
        self._blocked_until = 0.0  # after a failure Gemini is skipped for a while (NLLB covers)
        self._http = httpx.Client(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            headers={"x-goog-api-key": api_key}, timeout=timeout,
        )

    def _system(self, src: str, dst: str) -> str:
        s, d = self.LANG_NAMES.get(src, src), self.LANG_NAMES.get(dst, dst)
        terms = "\n".join(f"- {k} = {v}" for k, v in self._glossary.items())
        return (
            f"You are a live TV subtitle translator, {s} to {d}. The input is automatic speech "
            f"recognition output and may contain recognition errors; use the previous lines as context "
            f"to fix obvious ones. Translate ONLY the last line into natural, concise spoken {d}. "
            f"Keep proper names correct. Output the translation and nothing else.\n"
            + (f"Glossary (always use these renderings):\n{terms}" if terms else "")
        )

    def _gen_config(self) -> dict:
        cfg: dict = {"temperature": 0.2, "maxOutputTokens": 300}
        if self._thinking == "off":
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        elif self._thinking != "default":
            cfg["thinkingConfig"] = {"thinkingLevel": self._thinking}  # Gemini 3.x: minimal/low/...
        return cfg

    def translate(self, text: str, src: str, dst: str, context: tuple[str, ...] = ()) -> str:
        if not text or src == dst:
            return text
        if time.monotonic() < self._blocked_until:
            return self._fallback.translate(text, src, dst, context)
        prompt = ("Previous lines (context only):\n" + "\n".join(context) + "\n\n" if context else "") \
            + f"Last line to translate:\n{text}"
        try:
            r = self._http.post(f"/models/{self._model}:generateContent", json={
                "systemInstruction": {"parts": [{"text": self._system(src, dst)}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": self._gen_config(),
            })
            r.raise_for_status()
            out = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()
            if out:
                return out
        except Exception as e:  # noqa: BLE001 - any failure must not stall subtitles
            self._blocked_until = time.monotonic() + BLOCK_SECONDS
            code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
            print(f"[mt] gemini failed ({code}); NLLB for the next {BLOCK_SECONDS:.0f}s", flush=True)
        return self._fallback.translate(text, src, dst, context)


def load_glossary(path: str | None) -> dict[str, str]:
    """`source = target` per line; '#' comments."""
    out: dict[str, str] = {}
    if path and os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                out[k] = v
    return out


def load_env(path: str = ".env") -> None:
    """Minimal .env reader (KEY=VALUE); real environment variables win."""
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
