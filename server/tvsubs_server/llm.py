"""Local LLM translator on Apple Silicon (mlx-lm). Needs the `local` extra: `uv sync --extra local`."""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

from .prompts import extract_system, extract_user, system_prompt, user_prompt
from .terms import TermStore, parse_terms

# E4B (7.5 GB, ~1 s) keeps up with live speech; 12B (9 GB) and 26B-A4B (22 GB) are only ~0.05-0.1 better on our blind test.
DEFAULT_MODEL = "mlx-community/gemma-4-e4b-it-qat-OptiQ-4bit"
MAX_TOKENS = 90


def _clean(text: str) -> str:
    text = text.strip().split("\n")[0].strip()
    text = re.sub(r"^(translation|перевод)\s*:\s*", "", text, flags=re.I)
    return text.strip(" `\"'")


class LocalLlmTranslator:
    def __init__(self, repo: str, fallback, terms: TermStore):
        from mlx_lm import load
        from mlx_lm.models.cache import can_trim_prompt_cache, make_prompt_cache
        from mlx_lm.sample_utils import make_sampler

        print(f"[mt] loading {repo}", flush=True)
        self._model, self._tok = load(repo)
        self._sampler = make_sampler(temp=0.2)
        self._fallback = fallback
        self._terms = terms
        self._lock = threading.Lock()
        self._waiting = 0  # translations queued behind the lock; background work aborts when > 0
        self._make_cache = make_prompt_cache
        self._can_trim = can_trim_prompt_cache
        # The static system prompt is prefilled once per language pair and reused for every request.
        self._prefix: dict[tuple[str, str], tuple[list, list[int]] | None] = {}

    # --- prompt plumbing ---------------------------------------------------------
    def _template(self, system: str, user: str | None) -> str:
        msgs = [{"role": "system", "content": system}] + ([{"role": "user", "content": user}] if user else [])
        try:
            return self._tok.apply_chat_template(msgs, add_generation_prompt=user is not None,
                                                 tokenize=False, enable_thinking=False)
        except TypeError:
            return self._tok.apply_chat_template(msgs, add_generation_prompt=user is not None, tokenize=False)

    def _encode(self, text: str) -> list[int]:
        bos = self._tok.bos_token
        return self._tok.encode(text, add_special_tokens=bos is None or not text.startswith(bos))

    def _prefix_for(self, src: str, dst: str, system: str):
        key = (src, dst)
        if key not in self._prefix:
            from mlx_lm import generate
            ids = self._encode(self._template(system, None))
            cache = self._make_cache(self._model)
            if not self._can_trim(cache):
                self._prefix[key] = None
            else:
                generate(self._model, self._tok, prompt=ids, max_tokens=1, prompt_cache=cache)
                from mlx_lm.models.cache import trim_prompt_cache
                trim_prompt_cache(cache, cache[0].offset - len(ids))  # drop the one generated token
                self._prefix[key] = (cache, ids)
        return self._prefix[key]

    def _generate(self, system: str, user: str, src: str, dst: str, max_tokens: int,
                  abort: Callable[[], bool] | None = None, use_prefix: bool = True) -> str | None:
        from mlx_lm import stream_generate
        from mlx_lm.models.cache import trim_prompt_cache

        ids = self._encode(self._template(system, user))
        cache, pre = None, 0
        pref = self._prefix_for(src, dst, system) if use_prefix else None
        if pref and ids[: len(pref[1])] == pref[1]:
            cache, pre = pref[0], len(pref[1])
            ids = ids[pre:]
        parts: list[str] = []
        try:
            for r in stream_generate(self._model, self._tok, prompt=ids, max_tokens=max_tokens,
                                     sampler=self._sampler, prompt_cache=cache):
                parts.append(r.text)
                if abort and abort():
                    return None
        finally:
            if cache is not None:
                trim_prompt_cache(cache, cache[0].offset - pre)  # back to the bare prefix
        return "".join(parts)

    # --- public API --------------------------------------------------------------
    def warm(self, src: str, dst: str) -> None:
        self.translate("שלום", src, dst)

    def translate(self, text: str, src: str, dst: str, context: tuple[str, ...] = ()) -> str:
        if not text or src == dst:
            return text
        system = system_prompt(src, dst, self._terms.static_for(src))
        user = user_prompt(text, context, self._terms.learned_for(src))
        self._waiting += 1
        try:
            with self._lock:
                self._waiting -= 1
                out = _clean(self._generate(system, user, src, dst, MAX_TOKENS) or "")
        except Exception as e:  # noqa: BLE001 - never stall subtitles on a model error
            self._waiting = max(0, self._waiting - 1)
            print(f"[mt] local LLM failed ({type(e).__name__}: {e}); NLLB", flush=True)
            out = ""
        if not out or len(out) > 4 * len(text) + 60:  # empty or runaway output
            return self._fallback.translate(text, src, dst, context)
        return out

    def extract_terms(self, sources: list[str], translations: list[str], src: str, dst: str,
                      abort: Callable[[], bool]) -> list[tuple[str, str]]:
        """Names and slang worth remembering. Low priority: gives up the moment a translation waits."""
        if self._waiting or not self._lock.acquire(blocking=False):
            return []
        try:
            t0 = time.monotonic()
            out = self._generate(extract_system(src, dst), extract_user(sources, translations, src, dst),
                                 src, dst, 60, abort=lambda: self._waiting > 0 or abort(), use_prefix=False)
            if out is None:
                return []
            pairs = parse_terms(out, "\n".join(sources))
            print(f"[terms] {len(pairs)} candidates in {time.monotonic() - t0:.1f}s: {pairs}", flush=True)
            return pairs
        finally:
            self._lock.release()
