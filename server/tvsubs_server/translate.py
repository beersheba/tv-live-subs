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
from .prompts import system_prompt, user_prompt
from .terms import TermStore

_SENT_END = re.compile(r"(?<=[.!?…])\s+")
CACHE_MAX = 512
# How long Gemini is skipped (NLLB covers) after a failure, by cause.
BLOCK_TIMEOUT_S = 4.0
BLOCK_QUOTA_S = 30.0
BLOCK_OTHER_S = 8.0
MIN_WORDS_FOR_LLM = 3  # shorter fragments aren't worth a request

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

    def __init__(self, api_key: str, model: str, fallback: NllbTranslator, terms: TermStore,
                 thinking: str = "off", timeout: float = 3.5, rpm: float = 14.0):
        self._fallback = fallback
        self._terms = terms
        self._model = model
        self._thinking = thinking
        # Token bucket: spreads a per-minute quota evenly instead of burning it and then falling back
        # for half a minute. rpm <= 0 disables the limit (paid tier).
        self._rpm = rpm
        self._tokens = min(4.0, rpm)
        self._refilled = time.monotonic()
        self._blocked_until = 0.0  # after a failure Gemini is skipped for a while (NLLB covers)
        self._http = httpx.Client(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            headers={"x-goog-api-key": api_key}, timeout=timeout,
        )

    def _take(self) -> bool:
        if self._rpm <= 0:
            return True
        now = time.monotonic()
        self._tokens = min(4.0, self._tokens + (now - self._refilled) * self._rpm / 60)
        self._refilled = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

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
        if time.monotonic() < self._blocked_until or len(text.split()) < MIN_WORDS_FOR_LLM or not self._take():
            return self._fallback.translate(text, src, dst, context)
        prompt = user_prompt(text, context, self._terms.learned_for(src))
        try:
            r = self._http.post(f"/models/{self._model}:generateContent", json={
                "systemInstruction": {"parts": [{"text": system_prompt(src, dst, self._terms.static_for(src))}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": self._gen_config(),
            })
            r.raise_for_status()
            out = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()
            if out:
                return out
        except Exception as e:  # noqa: BLE001 - any failure must not stall subtitles
            code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
            block = BLOCK_QUOTA_S if code == 429 else BLOCK_TIMEOUT_S if isinstance(e, httpx.TimeoutException) \
                else BLOCK_OTHER_S
            self._blocked_until = time.monotonic() + block
            print(f"[mt] gemini failed ({code}); NLLB for the next {block:.0f}s", flush=True)
        return self._fallback.translate(text, src, dst, context)


def load_env(path: str = ".env") -> None:
    """Minimal .env reader (KEY=VALUE); real environment variables win."""
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
