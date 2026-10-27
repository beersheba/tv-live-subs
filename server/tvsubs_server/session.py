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
from .translate import Translator

SR = 16_000
STEP = int(0.7 * SR)           # run a pass every ~0.7 s of new audio
END_SILENCE = int(0.7 * SR)    # this much trailing silence finalizes a segment
MAX_SEGMENT = 12 * SR          # force-finalize long unbroken speech
MIN_SPEECH = int(0.25 * SR)
KEEP_TAIL = int(0.3 * SR)      # context kept at the start of the next segment
IDLE_TRIM = 2 * SR             # silence-only buffers are trimmed beyond this

VAD = VadOptions(min_silence_duration_ms=300, speech_pad_ms=150)

# One model instance per language and a shared CPU: serialize all inference.
_POOL = ThreadPoolExecutor(max_workers=1)


@dataclass
class Result:
    kind: str  # "partial" | "final" | "none"
    text: str = ""
    translation: str = ""
    consumed: int = 0  # samples to drop from the head of the buffer
    timings: str = ""


class Session:
    def __init__(self, ws: ServerConnection, stt: Transcriber, mt: Translator, src: str, dst: str):
        self.ws, self.stt, self.mt, self.src, self.dst = ws, stt, mt, src, dst
        self.buf = np.zeros(0, dtype=np.float32)
        self.fresh = 0
        self.busy = False
        self.seg_id = 0
        self.last_partial = ""

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
        print(f"[pass] audio={len(audio) / SR:.1f}s took={time.monotonic() - t0:.2f}s {res.timings} -> {res.kind}", flush=True)
        # Audio kept growing while we were busy; consumed counts from the head, so this is safe.
        self.buf = self.buf[res.consumed:]
        if res.kind == "none":
            return
        if res.kind == "partial":
            if res.text == self.last_partial:
                return
            self.last_partial = res.text
        else:
            self.last_partial = ""
        await self.ws.send(json.dumps({
            "type": res.kind, "id": self.seg_id, "text": res.text, "translation": res.translation,
            "src": self.src, "dst": self.dst,
        }, ensure_ascii=False))
        if res.kind == "final":
            self.seg_id += 1

    # --- runs in the worker thread -------------------------------------------------
    def _process(self, audio: np.ndarray) -> Result:
        t0 = time.monotonic()
        speech = get_speech_timestamps(audio, VAD)
        t_vad = time.monotonic() - t0
        if not speech:
            return Result("none", consumed=max(0, len(audio) - KEEP_TAIL) if len(audio) > IDLE_TRIM else 0)
        start, end = speech[0]["start"], speech[-1]["end"]
        if end - start < MIN_SPEECH:
            return Result("none")
        final = len(audio) - end >= END_SILENCE or len(audio) >= MAX_SEGMENT
        consumed = max(0, len(audio) - KEEP_TAIL) if final else 0
        t1 = time.monotonic()
        text = self.stt.transcribe(audio[max(0, start - KEEP_TAIL):], self.src, final)
        t_stt = time.monotonic() - t1
        if not text:
            return Result("none", consumed=consumed)
        t2 = time.monotonic()
        translation = self.mt.translate(text, self.src, self.dst)
        timings = f"vad={t_vad:.2f} stt={t_stt:.2f} mt={time.monotonic() - t2:.2f}"
        return Result("final" if final else "partial", text, translation, consumed, timings)
