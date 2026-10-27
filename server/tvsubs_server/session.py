"""Per-connection streaming logic: VAD segmentation, partial/final STT, translation."""
from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from .stt import Transcriber
from .translate import GeminiTranslator, NllbTranslator

Translator = GeminiTranslator | NllbTranslator
CONTEXT_LINES = 4

SR = 16_000
STEP = int(0.7 * SR)           # run a pass every ~0.7 s of new audio
END_SILENCE = int(0.7 * SR)    # this much trailing silence finalizes a segment
MAX_SEGMENT = 8 * SR           # force-finalize long unbroken speech
MIN_SPEECH = int(0.25 * SR)
DETECT_MIN = int(1.2 * SR)    # speech needed before the language is guessed (auto mode)
LANG_MIN_PROB = 0.5
KEEP_TAIL = int(0.3 * SR)      # context kept at the start of the next segment
IDLE_TRIM = 2 * SR             # silence-only buffers are trimmed beyond this

STRONG = ".?!…"            # sentence end
CLAUSE = ",;:—–"           # clause end: also a cut point once the chunk is long enough
MIN_CHUNK = 3              # words before a clause end may cut
MAX_CHUNK = 9              # words after which we cut regardless of punctuation
_STRIP = ".,!?;:…—–\"'()"

VAD = VadOptions(min_silence_duration_ms=300, speech_pad_ms=150)

# One model instance per language and a shared CPU: serialize all inference.
_POOL = ThreadPoolExecutor(max_workers=1)


def _norm(word: str) -> str:
    return word.strip(_STRIP).lower()


@dataclass
class Result:
    commits: list[tuple[str, str]]  # (text, translation) of newly stable sentences
    consumed: int = 0  # samples to drop from the head of the buffer
    tail: str = ""     # not-yet-stable text (logged only)
    timings: str = ""
    lang: str = ""


class Session:
    def __init__(self, ws: ServerConnection, stt: Transcriber, mt: Translator, src: str, dst: str):
        self.ws, self.stt, self.mt, self.src, self.dst = ws, stt, mt, src, dst
        self.buf = np.zeros(0, dtype=np.float32)
        self.fresh = 0
        self.busy = False
        self.seq = 0
        # In auto mode the language is detected once per segment and kept until it ends.
        self._lang: str | None = None if src == "auto" else src
        self._last_lang: str | None = None
        # Worker-thread state: words of the previous pass and how many of them are committed.
        self._prev: list[str] = []
        self._committed = 0
        self._history: list[str] = []  # recent source sentences, fed to the translator as context

    async def feed(self, pcm: bytes) -> None:
        chunk = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, chunk])
        self.fresh += len(chunk)
        if self.fresh < STEP or self.busy:
            return
        self.fresh = 0
        self.busy = True
        # Don't block the receive loop on inference: audio keeps accumulating meanwhile.
        asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self._step()
        except ConnectionClosed:
            pass
        finally:
            self.busy = False

    async def _step(self) -> None:
        audio = self.buf.copy()
        t0 = time.monotonic()
        res = await asyncio.get_running_loop().run_in_executor(_POOL, self._process, audio)
        print(f"[pass] audio={len(audio) / SR:.1f}s took={time.monotonic() - t0:.2f}s {res.timings} "
              f"commits={len(res.commits)} tail={res.tail!r}", flush=True)
        # Audio kept growing while we were busy; consumed counts from the head, so this is safe.
        self.buf = self.buf[res.consumed:]
        for text, translation in res.commits:
            await self.ws.send(json.dumps({
                "type": "commit", "id": self.seq, "text": text, "translation": translation,
                "src": res.lang, "dst": self.dst,
            }, ensure_ascii=False))
            self.seq += 1

    # --- runs in the worker thread -------------------------------------------------
    def _process(self, audio: np.ndarray) -> Result:
        t0 = time.monotonic()
        speech = get_speech_timestamps(audio, VAD)
        t_vad = time.monotonic() - t0
        if not speech:
            return Result([], consumed=max(0, len(audio) - KEEP_TAIL) if len(audio) > IDLE_TRIM else 0)
        start, end = speech[0]["start"], speech[-1]["end"]
        if end - start < MIN_SPEECH:
            return Result([])
        final = len(audio) - end >= END_SILENCE or len(audio) >= MAX_SEGMENT
        consumed = max(0, len(audio) - KEEP_TAIL) if final else 0

        lang = self._lang
        if lang is None:  # auto mode, first pass of a segment
            if end - start < DETECT_MIN and not final:
                return Result([])  # not enough speech to tell the language yet
            lang, prob = self.stt.detect(audio[max(0, start - KEEP_TAIL):])
            if prob < LANG_MIN_PROB:
                lang = self._last_lang or ""
            print(f"[lang] detected {lang or '?'} p={prob:.2f}", flush=True)
            if not lang:
                if final:
                    self._end_segment()
                return Result([], consumed)
            self._lang = self._last_lang = lang
        if lang == self.dst:  # already in the target language: nothing to translate
            if final:
                self._end_segment()
            return Result([], consumed)

        t1 = time.monotonic()
        text = self.stt.transcribe(audio[max(0, start - KEEP_TAIL):], lang, final)
        t_stt = time.monotonic() - t1
        words = text.split()

        # Local agreement: words are stable once two consecutive passes produced them. Stable words
        # are cut into short chunks at punctuation (or after MAX_CHUNK words); at a segment end
        # everything left is flushed.
        if final:
            stable = len(words)
        else:
            stable = 0
            while (stable < min(len(words), len(self._prev))
                   and _norm(words[stable]) == _norm(self._prev[stable])):
                stable += 1

        chunks: list[str] = []
        cur: list[str] = []
        for w in words[self._committed:stable]:
            cur.append(w)
            if (w[-1] in STRONG and len(cur) >= 2) or (w[-1] in CLAUSE and len(cur) >= MIN_CHUNK) \
                    or len(cur) >= MAX_CHUNK:
                chunks.append(" ".join(cur))
                cur = []
        if final and cur:
            chunks.append(" ".join(cur))
            cur = []
        committed = stable - len(cur)

        t2 = time.monotonic()
        commits = []
        for chunk in chunks:
            ctx = tuple(self._history[-CONTEXT_LINES:])
            try:
                commits.append((chunk, self.mt.translate(chunk, lang, self.dst, ctx)))
            except ValueError as e:  # language the fallback translator doesn't know
                print(f"[mt] skipped: {e}", flush=True)
            self._history.append(chunk)
        del self._history[:-CONTEXT_LINES * 2]
        timings = f"vad={t_vad:.2f} stt={t_stt:.2f} mt={time.monotonic() - t2:.2f}"

        if final:
            self._end_segment()
        else:
            self._prev, self._committed = words, max(self._committed, committed)
        return Result(commits, consumed, " ".join(words[self._committed:]), timings, lang)

    def _end_segment(self) -> None:
        self._prev, self._committed = [], 0
        if self.src == "auto":
            self._lang = None
