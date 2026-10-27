"""WebSocket server.

Protocol
  client -> server  text   {"type":"config","src":"he","dst":"ru"}   (first message)
  client -> server  binary PCM 16-bit little-endian, 16 kHz, mono
  server -> client  text   {"type":"ready"}
  server -> client  text   {"type":"commit","id":N,"text":..,"translation":..,"src":..,"dst":..}  (immutable subtitle line)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from websockets.asyncio.server import ServerConnection, serve

from .langs import nllb
from .session import Session
from .stt import DEFAULT_MODELS, FALLBACK_MODEL, Transcriber
from .translate import CT2_REPO, GeminiTranslator, NllbTranslator, load_env, load_glossary


async def handle(ws: ServerConnection, stt: Transcriber, mt: Translator) -> None:
    try:
        cfg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        src, dst = cfg["src"], cfg["dst"]
        if src != "auto":
            nllb(src)
        nllb(dst)  # validate early
    except Exception as e:  # noqa: BLE001
        await ws.close(code=1008, reason=f"bad config: {e}"[:120])
        return
    print(f"[ws] {ws.remote_address} {src}->{dst}", flush=True)
    session = Session(ws, stt, mt, src, dst)
    await ws.send(json.dumps({"type": "ready"}))
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                await session.feed(msg)
    finally:
        print(f"[ws] {ws.remote_address} closed", flush=True)


async def run(args: argparse.Namespace) -> None:
    models = dict(DEFAULT_MODELS)
    models["he"] = args.he_model
    stt = Transcriber(models, args.model, args.threads)
    nllb_mt = NllbTranslator(args.mt_model, args.compute_type, args.mt_threads)
    key = os.environ.get("GEMINI_API_KEY")
    if args.translator == "gemini" and not key:
        raise SystemExit("--translator gemini needs GEMINI_API_KEY (environment or server/.env)")
    if args.translator == "auto":
        args.translator = "gemini" if key else "nllb"
    if args.translator == "gemini":
        mt = GeminiTranslator(key, args.gemini_model, nllb_mt, load_glossary(args.glossary), args.gemini_thinking)
        print(f"[mt] gemini {args.gemini_model} (thinking={args.gemini_thinking}), NLLB fallback", flush=True)
    else:
        mt = nllb_mt
    for lang in args.preload:
        stt.preload_detector() if lang == "auto" else stt.preload(lang)
    async with serve(lambda ws: handle(ws, stt, mt), args.host, args.port, max_size=2**20):
        print(f"[ws] listening on {args.host}:{args.port}", flush=True)
        await asyncio.Future()


def main() -> None:
    load_env()
    p = argparse.ArgumentParser(prog="tvsubs-server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--model", default=FALLBACK_MODEL, help="ggml path or <repo>::<file> for languages without a dedicated model")
    p.add_argument("--he-model", default=DEFAULT_MODELS["he"], help="ggml path or <repo>::<file>")
    p.add_argument("--mt-model", default=CT2_REPO, help="CTranslate2 NLLB repo or path")
    p.add_argument("--compute-type", default="int8", help="CTranslate2 compute type for translation")
    p.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    p.add_argument("--mt-threads", type=int, default=4, help="CPU threads for translation (more is slower here)")
    p.add_argument("--translator", choices=["auto", "gemini", "nllb"], default="auto")
    p.add_argument("--gemini-model", default=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    p.add_argument("--gemini-thinking", default="default", help="off | default | thinkingLevel (minimal/low/...)")
    p.add_argument("--glossary", default="glossary.txt")
    p.add_argument("--preload", metavar="LANG", nargs="+", default=[], help="load these STT models at startup (a language code, or 'auto' for the detector)")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
