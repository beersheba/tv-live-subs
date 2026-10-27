"""NLLB-200 translation through CTranslate2."""
from __future__ import annotations

import re
import threading

import ctranslate2
from huggingface_hub import snapshot_download
from transformers import AutoTokenizer

from .langs import nllb

_SENT_END = re.compile(r"(?<=[.!?…])\s+")
CACHE_MAX = 512

TOKENIZER = "facebook/nllb-200-distilled-600M"
CT2_REPO = "JustFrederik/nllb-200-distilled-600M-ct2-int8"


class Translator:
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

    def translate(self, text: str, src: str, dst: str) -> str:
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
