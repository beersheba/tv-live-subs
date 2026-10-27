"""Per-connection streaming logic: VAD segmentation, STT, then translation in its own queue."""
from __future__ import annotations

import asyncio
import json
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from .stt import Transcriber
from .terms import TermStore

CONTEXT_LINES = 4
LEARN_EVERY = 8          # commits between glossary-learning attempts
RECENT_LINES = 10        # lines the learner looks at

SR = 16_000
STEP = int(0.7 * SR)           # run a pass every ~0.7 s of new audio
END_SILENCE = int(0.7 * SR)    # this much trailing silence finalizes a segment
MAX_SEGMENT = 8 * SR           # force-finalize long unbroken speech
MIN_SPEECH = int(0.25 * SR)
DETECT_MIN = int(1.2 * SR)     # speech needed before the language is guessed (auto mode)
LANG_MIN_PROB = 0.5            # to accept a language when none is known yet
LANG_SWITCH_PROB = 0.85        # to move away from the language of the previous segment
KEEP_TAIL = int(0.3 * SR)      # context kept at the start of the next segment
IDLE_TRIM = 2 * SR             # silence-only buffers are trimmed beyond this

STRONG = ".?!…"            # sentence end
CLAUSE = ",;:—–"           # clause end: also a cut point once the chunk is long enough
MIN_CHUNK = 3              # words before a clause end may cut
MAX_CHUNK = 9              # words after which we cut regardless of punctuation
_STRIP = ".,!?;:…—–\"'()"

VAD = VadOptions(min_silence_duration_ms=300, speech_pad_ms=150)

# Whisper+VAD and translation each get one worker, so a slow translation never delays the next STT pass.
_STT_POOL = ThreadPoolExecutor(max_workers=1)
_MT_POOL = ThreadPoolExecutor(max_workers=1)


def _norm(word: str) -> str:
    return word.strip(_STRIP).lower()


@dataclass
class Result:
    chunks: list[str]   # newly stable text, ready to translate
    consumed: int = 0   # samples to drop from the head of the buffer
    tail: str = ""      # not-yet-stable text (logged only)
    timings: str = ""
    lang: str = ""


class Session:
    def __init__(self, ws: ServerConnection, stt: Transcriber, mt, terms: TermStore, src: str, dst: str,
                 record_dir: str | None = None, languages: frozenset[str] | None = None,
                 stt_prompt: bool = False):
        self.ws, self.stt, self.mt, self.terms, self.src, self.dst = ws, stt, mt, terms, src, dst
        self._languages = languages
        self._stt_prompt = stt_prompt
        self._wav: wave.Wave_write | None = None
        if record_dir:
            Path(record_dir).mkdir(parents=True, exist_ok=True)
            self._wav = wave.open(str(Path(record_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}.wav"), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(SR)
        self.buf = np.zeros(0, dtype=np.float32)
        self.fresh = 0
        self.busy = False
        self.seq = 0
        # In auto mode the language is detected once per segment and kept until it ends.
        self._lang: str | None = None if src == "auto" else src
        self._last_lang: str | None = None
        # STT-thread state: words of the previous pass and how many of them are committed.
        self._prev: list[str] = []
        self._committed = 0
        # Translation side (event-loop state)
        self._history: list[str] = []                    # recent source chunks, fed as context
        self._recent: list[tuple[str, str]] = []         # (source, translation) for the term learner
        self._since_learn = 0
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._translator = asyncio.create_task(self._translate_loop())

    def close(self) -> None:
        self._translator.cancel()
        self.terms.save()
        if self._wav:
            self._wav.close()
            self._wav = None

    async def feed(self, pcm: bytes) -> None:
        if self._wav:
            self._wav.writeframes(pcm[: len(pcm) // 2 * 2])
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
        res = await asyncio.get_running_loop().run_in_executor(_STT_POOL, self._process, audio)
        print(f"[pass] audio={len(audio) / SR:.1f}s took={time.monotonic() - t0:.2f}s {res.timings} "
              f"chunks={len(res.chunks)} tail={res.tail!r}", flush=True)
        # Audio kept growing while we were busy; consumed counts from the head, so this is safe.
        self.buf = self.buf[res.consumed:]
        for chunk in res.chunks:
            self._queue.put_nowait((chunk, res.lang))

    # --- translation queue (event loop) --------------------------------------------
    async def _translate_loop(self) -> None:
        try:
            while True:
                text, lang = await self._queue.get()
                try:
                    await self._translate_one(text, lang)
                except ConnectionClosed:
                    raise
                except Exception:  # noqa: BLE001 - one bad chunk must not end the session's translation
                    import traceback
                    print(f"[mt] chunk failed:\n{traceback.format_exc()}", flush=True)
        except (ConnectionClosed, asyncio.CancelledError):
            pass

    async def _translate_one(self, text: str, lang: str) -> None:
        loop = asyncio.get_running_loop()
        ctx = tuple(self._history[-CONTEXT_LINES:])
        t0 = time.monotonic()
        try:
            translation = await loop.run_in_executor(_MT_POOL, self.mt.translate, text, lang, self.dst, ctx)
        except ValueError as e:  # language the fallback translator doesn't know
            print(f"[mt] skipped: {e}", flush=True)
            translation = None
        self._history.append(text)
        del self._history[:-CONTEXT_LINES * 2]
        if translation is None:
            return
        print(f"[mt] {time.monotonic() - t0:.2f}s queue={self._queue.qsize()}", flush=True)
        await self.ws.send(json.dumps({
            "type": "commit", "id": self.seq, "text": text, "translation": translation,
            "src": lang, "dst": self.dst,
        }, ensure_ascii=False))
        self.seq += 1
        self._recent.append((text, translation))
        del self._recent[:-RECENT_LINES]
        self._since_learn += 1
        if self._since_learn >= LEARN_EVERY and self._queue.empty():
            await self._learn(lang)

    async def _learn(self, lang: str) -> None:
        self._since_learn = 0
        extract = getattr(self.mt, "extract_terms", None)  # only LLM translators can learn
        if extract is None:
            return
        sources, translations = (list(x) for x in zip(*self._recent))
        pairs = await asyncio.get_running_loop().run_in_executor(
            _MT_POOL, extract, sources, translations, lang, self.dst, lambda: self._queue.qsize() >= 2)
        if pairs:
            self.terms.add(lang, pairs)

    # --- runs in the STT worker thread ---------------------------------------------
    def _pick_language(self, audio: np.ndarray) -> str:
        lang, prob = self.stt.detect(audio)
        last = self._last_lang
        if self._languages and lang not in self._languages:
            chosen = last or ""
        elif last and lang != last and prob < LANG_SWITCH_PROB:
            chosen = last  # not sure enough to leave the language we were just in
        elif not last and prob < LANG_MIN_PROB:
            chosen = ""
        else:
            chosen = lang
        print(f"[lang] heard {lang} p={prob:.2f} -> {chosen or '?'}", flush=True)
        return chosen

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
            lang = self._pick_language(audio[max(0, start - KEEP_TAIL):])
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
        prompt = self.terms.whisper_prompt(lang) if self._stt_prompt else ""
        text = self.stt.transcribe(audio[max(0, start - KEEP_TAIL):], lang, final, prompt)
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
        timings = f"vad={t_vad:.2f} stt={t_stt:.2f}"

        if final:
            self._end_segment()
        else:
            self._prev, self._committed = words, max(self._committed, committed)
        return Result(chunks, consumed, " ".join(words[self._committed:]), timings, lang)

    def _end_segment(self) -> None:
        self._prev, self._committed = [], 0
        if self.src == "auto":
            self._lang = None
